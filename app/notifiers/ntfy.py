"""ntfy push notifications - free, no account needed, works on iPhone.

Setup (README has the walkthrough):
  1. Install "ntfy" from the App Store.
  2. Subscribe to a LONG RANDOM topic, e.g. ff-root-7f3a91c2e05b4d76.
  3. Put it in .env as NTFY_TOPIC.

The topic is a shared secret: on the public ntfy.sh server anyone who knows the
topic name can read *and* publish to it.  Treat it like a password, which is why
setup generates a 128-bit random one rather than letting you pick 'football'.
"""

from __future__ import annotations

import os
from typing import Optional

import requests

from .base import Notifier, NotificationError


class NtfyNotifier(Notifier):
    name = "ntfy"

    def __init__(self, server: Optional[str] = None, topic: Optional[str] = None,
                 token: Optional[str] = None, user: Optional[str] = None,
                 password: Optional[str] = None, **kw) -> None:
        super().__init__(**kw)
        self.server = (server or os.getenv("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
        self.topic = topic or os.getenv("NTFY_TOPIC", "")
        self.token = token or os.getenv("NTFY_TOKEN", "")
        self.user = user or os.getenv("NTFY_USER", "")
        self.password = password or os.getenv("NTFY_PASSWORD", "")

    def available(self) -> tuple[bool, str]:
        if not self.topic:
            return False, "NTFY_TOPIC is not set in .env"
        return True, ""

    def _send(self, title: str, body: str) -> str:
        headers = {
            # ntfy headers must be latin-1 safe, so strip emoji from the title
            # and let the (UTF-8) body carry them.
            "Title": title.encode("ascii", "ignore").decode().strip() or "Fantasy Rooting Agent",
            "Priority": "high",
            "Tags": "football",
            "Markdown": "no",
        }
        auth = None
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.user:
            auth = (self.user, self.password)

        r = requests.post(f"{self.server}/{self.topic}", data=body.encode("utf-8"),
                          headers=headers, auth=auth, timeout=15)
        if r.status_code >= 400:
            raise NotificationError(f"ntfy returned {r.status_code}: {r.text[:200]}")
        return f"published to {self.server}/<topic>"
