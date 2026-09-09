"""Notifier interface + shared retry policy."""

from __future__ import annotations

import logging
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


class NotificationError(RuntimeError):
    pass


@dataclass
class NotificationResult:
    ok: bool
    provider: str
    detail: str = ""
    attempts: int = 1
    dry_run: bool = False
    #: Provider-native id of the sent message, when the provider hands one back
    #: (Telegram does). Lets a later call ask the provider to delete it.
    message_id: Optional[str] = None


class Notifier(ABC):
    name: str = "base"
    #: True when the provider only works on the machine sitting on your desk.
    requires_local_mac: bool = False
    #: True when this provider can recall a message it already sent.
    supports_delete: bool = False

    def __init__(self, *, dry_run: bool = False, retries: int = 3) -> None:
        self.dry_run = dry_run
        self.retries = retries

    @abstractmethod
    def _send(self, title: str, body: str) -> str | tuple[str, Optional[str]]:
        """Do the actual send.  Raise NotificationError on failure.

        Return either a plain detail string, or (detail, message_id) for a
        provider that can hand back an id `delete()` can later use.
        """

    def available(self) -> tuple[bool, str]:
        """(configured?, why not)"""
        return True, ""

    def delete(self, message_id: str) -> bool:
        """Best-effort: ask the provider to recall a message it sent earlier.

        The base implementation is a no-op - most channels (ntfy, iMessage,
        Twilio) have no way to unsend, and that's fine: it just means an earlier
        preview stays visible alongside the update instead of disappearing.
        """
        return False

    def send(self, title: str, body: str) -> NotificationResult:
        ok, why = self.available()
        if not ok:
            return NotificationResult(False, self.name, f"not configured: {why}")
        if self.dry_run:
            log.info("[dry-run] %s would send:\n%s\n%s", self.name, title, body)
            return NotificationResult(True, self.name, "dry run - nothing sent", dry_run=True)

        last = ""
        for attempt in range(1, self.retries + 1):
            try:
                result = self._send(title, body)
                detail, message_id = result if isinstance(result, tuple) else (result, None)
                return NotificationResult(True, self.name, detail, attempts=attempt,
                                          message_id=message_id)
            except Exception as exc:  # noqa: BLE001 - report, back off, retry
                last = f"{type(exc).__name__}: {exc}"
                log.warning("%s send attempt %d/%d failed: %s",
                            self.name, attempt, self.retries, last)
                if attempt < self.retries:
                    time.sleep((2 ** (attempt - 1)) * 1.0 + random.uniform(0, 0.5))
        return NotificationResult(False, self.name, last, attempts=self.retries)
