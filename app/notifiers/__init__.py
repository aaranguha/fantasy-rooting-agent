"""Notifier registry.  Priority order is deliberate (spec section 18):
iMessage -> ntfy -> Telegram -> console -> optional Twilio.
"""

from __future__ import annotations

from typing import Optional

from .base import Notifier, NotificationError, NotificationResult
from .console import ConsoleNotifier
from .imessage import IMessageNotifier
from .ntfy import NtfyNotifier
from .telegram import TelegramNotifier
from .twilio import TwilioNotifier

PRIORITY = ["imessage", "ntfy", "telegram", "console", "twilio"]

_REGISTRY = {
    "imessage": IMessageNotifier,
    "ntfy": NtfyNotifier,
    "telegram": TelegramNotifier,
    "console": ConsoleNotifier,
    "twilio": TwilioNotifier,
}


def build(name: str, *, dry_run: bool = False, **kw) -> Notifier:
    cls = _REGISTRY.get((name or "console").lower())
    if cls is None:
        raise NotificationError(
            f"Unknown notifier {name!r}. Choose one of: {', '.join(PRIORITY)}"
        )
    return cls(dry_run=dry_run, **kw)


def available_notifiers() -> list[tuple[str, bool, str]]:
    """(name, configured, reason) for every provider, in priority order."""
    out = []
    for name in PRIORITY:
        try:
            ok, why = build(name).available()
        except Exception as exc:  # noqa: BLE001
            ok, why = False, str(exc)
        out.append((name, ok, why))
    return out


def autoselect(preferred: Optional[str] = None, *, dry_run: bool = False) -> Notifier:
    """Use the preferred provider if it is configured, else fall down the list."""
    if preferred:
        n = build(preferred, dry_run=dry_run)
        if n.available()[0]:
            return n
    for name in PRIORITY:
        if name == "twilio":
            continue  # never auto-select a paid provider
        n = build(name, dry_run=dry_run)
        if n.available()[0]:
            return n
    return ConsoleNotifier(dry_run=dry_run)


__all__ = ["Notifier", "NotificationError", "NotificationResult", "ConsoleNotifier",
           "IMessageNotifier", "NtfyNotifier", "TelegramNotifier", "TwilioNotifier",
           "build", "available_notifiers", "autoselect", "PRIORITY"]
