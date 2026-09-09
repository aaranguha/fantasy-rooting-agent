"""Shared HTTP plumbing: retry/backoff, timeouts, and an on-disk TTL cache."""

from __future__ import annotations

import json
import hashlib
import os
import logging
import random
import time
from pathlib import Path
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

# ESPN's site.api host 403s several browser-like UAs (and anything containing
# "fantasy"); a plain client UA is accepted by every host we talk to.
USER_AGENT = os.getenv("HTTP_USER_AGENT", "python-requests/2.32")


class ProviderError(RuntimeError):
    """Any provider-level failure the analyzer should degrade gracefully around."""


class AuthError(ProviderError):
    """Credentials missing, expired or rejected."""


class HttpClient:
    """Thin requests wrapper with exponential backoff + jitter and a file cache."""

    def __init__(
        self,
        base_url: str = "",
        *,
        cookies: Optional[dict[str, str]] = None,
        headers: Optional[dict[str, str]] = None,
        timeout: float = 20.0,
        retries: int = 3,
        cache_dir: Optional[Path] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.cache_dir = cache_dir
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        if headers:
            self.session.headers.update(headers)
        if cookies:
            self.session.cookies.update(cookies)

    # -- cache --------------------------------------------------------------
    def _cache_file(self, key: str) -> Optional[Path]:
        if not self.cache_dir:
            return None
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return self.cache_dir / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".json")

    def _cache_read(self, key: str, ttl: float) -> Optional[Any]:
        f = self._cache_file(key)
        if not f or not f.exists():
            return None
        if ttl >= 0 and (time.time() - f.stat().st_mtime) > ttl:
            return None
        try:
            return json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def _cache_write(self, key: str, value: Any) -> None:
        f = self._cache_file(key)
        if not f:
            return
        try:
            f.write_text(json.dumps(value))
        except (OSError, TypeError):  # pragma: no cover - cache is best effort
            log.debug("cache write failed for %s", key)

    # -- requests -----------------------------------------------------------
    def get(
        self,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
        cache_ttl: float = 0.0,
        allow_stale_on_error: bool = True,
    ) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        key = url + "?" + json.dumps(params or {}, sort_keys=True)

        if cache_ttl > 0:
            hit = self._cache_read(key, cache_ttl)
            if hit is not None:
                log.debug("cache hit %s", url)
                return hit

        last: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                r = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
                if r.status_code in (401, 403):
                    raise AuthError(f"{r.status_code} from {url} - credentials rejected or league is private")
                if r.status_code == 404:
                    raise ProviderError(f"404 from {url}")
                if r.status_code >= 500 or r.status_code == 429:
                    raise ProviderError(f"{r.status_code} from {url}")
                r.raise_for_status()
                data = r.json()
                if cache_ttl != 0:
                    self._cache_write(key, data)
                return data
            except AuthError:
                raise
            except Exception as exc:  # noqa: BLE001 - retry everything else
                last = exc
                if attempt < self.retries - 1:
                    delay = (2 ** attempt) * 0.75 + random.uniform(0, 0.4)
                    log.debug("GET %s failed (%s); retry in %.1fs", url, exc, delay)
                    time.sleep(delay)

        if allow_stale_on_error and cache_ttl != 0:
            stale = self._cache_read(key, ttl=-1)
            if stale is not None:
                log.warning("Using STALE cache for %s after failure: %s", url, last)
                return stale
        raise ProviderError(f"GET {url} failed after {self.retries} attempts: {last}") from last

    def post(self, url: str, *, json_body: Optional[dict] = None, data: Optional[dict] = None) -> Any:
        last: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                r = self.session.post(url, json=json_body, data=data, timeout=self.timeout)
                if r.status_code in (401, 403):
                    raise AuthError(f"{r.status_code} from {url}")
                r.raise_for_status()
                try:
                    return r.json()
                except ValueError:
                    return {"ok": True, "text": r.text}
            except AuthError:
                raise
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < self.retries - 1:
                    time.sleep((2 ** attempt) * 0.75 + random.uniform(0, 0.4))
        raise ProviderError(f"POST {url} failed after {self.retries} attempts: {last}") from last
