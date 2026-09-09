"""Native iMessage via the macOS Messages app (free, no account, no service).

HOW IT WORKS
    We drive Messages.app with AppleScript through `osascript`.  Two dialects are
    tried, because which one works varies by macOS release and by whether the
    recipient is on iMessage or SMS:

      1. `participant "<handle>" of (1st account whose service type = iMessage)`
      2. `buddy "<handle>" of service "SMS"`   (fallback, needs Text Message
         Forwarding enabled on your iPhone)

REQUIREMENTS - all of these must be true at send time:
    * The Mac is powered on, awake (or has "Prevent sleeping" set) and logged in
      to the same user account that runs the agent.
    * Messages.app is signed in to your Apple ID and has iMessage enabled.
    * The process running the agent has Automation permission for Messages
      (System Settings -> Privacy & Security -> Automation), and Full Disk Access
      is required for some terminal apps.

If your Mac may be asleep at kickoff, use ntfy or Telegram instead - those reach
your phone with the laptop closed.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
from typing import Optional

from .base import Notifier, NotificationError

log = logging.getLogger(__name__)

# Escape for insertion into an AppleScript string literal.
def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


SCRIPT_IMESSAGE = '''
on run
    set target to "{handle}"
    set msg to "{body}"
    tell application "Messages"
        set svc to 1st account whose service type = iMessage
        set who to participant target of svc
        send msg to who
    end tell
    return "sent via iMessage"
end run
'''

SCRIPT_SMS = '''
on run
    set target to "{handle}"
    set msg to "{body}"
    tell application "Messages"
        set svc to service "SMS"
        send msg to buddy target of svc
    end tell
    return "sent via SMS relay"
end run
'''


class IMessageNotifier(Notifier):
    name = "imessage"
    requires_local_mac = True

    def __init__(self, to: Optional[str] = None, timeout: float = 30.0, **kw) -> None:
        super().__init__(**kw)
        self.to = (to or os.getenv("IMESSAGE_TO", "")).strip()
        self.timeout = timeout

    # -- preflight ----------------------------------------------------------
    def available(self) -> tuple[bool, str]:
        if platform.system() != "Darwin":
            return False, "iMessage only works on macOS"
        if not shutil.which("osascript"):
            return False, "osascript not found"
        if not self.to:
            return False, "IMESSAGE_TO is not set in .env (your phone number or Apple ID email)"
        return True, ""

    def messages_running(self) -> bool:
        try:
            out = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to (name of processes) contains "Messages"'],
                capture_output=True, text=True, timeout=10,
            )
            return out.stdout.strip() == "true"
        except Exception:  # noqa: BLE001
            return False

    def ensure_messages_running(self) -> None:
        """Launch Messages.app (hidden) if it isn't already up."""
        if self.messages_running():
            return
        try:
            subprocess.run(["open", "-g", "-a", "Messages"], capture_output=True, timeout=15)
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not pre-launch Messages: %s", exc)

    # -- send ---------------------------------------------------------------
    def _run(self, script: str) -> str:
        proc = subprocess.run(["osascript", "-"], input=script, capture_output=True,
                              text=True, timeout=self.timeout)
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()
            if "-1743" in err or "not allowed assistive" in err or "Not authorized" in err:
                raise NotificationError(
                    "macOS blocked the automation. Grant your terminal/python Automation "
                    "access to Messages in System Settings > Privacy & Security > Automation, "
                    f"then retry. ({err})"
                )
            raise NotificationError(err or f"osascript exited {proc.returncode}")
        return (proc.stdout or "").strip() or "sent"

    def _send(self, title: str, body: str) -> str:
        self.ensure_messages_running()
        payload = f"{title}\n\n{body}"
        script = SCRIPT_IMESSAGE.format(handle=_esc(self.to), body=_esc(payload))
        try:
            return self._run(script)
        except NotificationError as first:
            log.info("iMessage dialect 1 failed (%s); trying the SMS-relay dialect", first)
            script = SCRIPT_SMS.format(handle=_esc(self.to), body=_esc(payload))
            try:
                return self._run(script)
            except NotificationError as second:
                raise NotificationError(f"iMessage failed: {first} | SMS relay failed: {second}") from second
