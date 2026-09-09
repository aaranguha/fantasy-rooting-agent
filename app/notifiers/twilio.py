"""OPTIONAL Twilio SMS.  Nothing in this app requires it, it is never the
default, and the package is not installed unless you ask for it:

    pip install 'fantasy-rooting-agent[twilio]'
"""

from __future__ import annotations

import os
from typing import Optional

from .base import Notifier, NotificationError


class TwilioNotifier(Notifier):
    name = "twilio"

    def __init__(self, sid: Optional[str] = None, token: Optional[str] = None,
                 from_: Optional[str] = None, to: Optional[str] = None, **kw) -> None:
        super().__init__(**kw)
        self.sid = sid or os.getenv("TWILIO_ACCOUNT_SID", "")
        self.token = token or os.getenv("TWILIO_AUTH_TOKEN", "")
        self.from_ = from_ or os.getenv("TWILIO_FROM", "")
        self.to = to or os.getenv("TWILIO_TO", "")

    def available(self) -> tuple[bool, str]:
        missing = [n for n, v in (("TWILIO_ACCOUNT_SID", self.sid),
                                  ("TWILIO_AUTH_TOKEN", self.token),
                                  ("TWILIO_FROM", self.from_),
                                  ("TWILIO_TO", self.to)) if not v]
        if missing:
            return False, "missing " + ", ".join(missing)
        try:
            import twilio  # noqa: F401
        except ImportError:
            return False, "the optional twilio package is not installed"
        return True, ""

    def _send(self, title: str, body: str) -> str:
        from twilio.rest import Client  # imported lazily; optional dependency

        client = Client(self.sid, self.token)
        msg = client.messages.create(body=f"{title}\n\n{body}", from_=self.from_, to=self.to)
        if not getattr(msg, "sid", None):
            raise NotificationError("Twilio did not return a message sid")
        return f"twilio sid {msg.sid}"
