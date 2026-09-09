"""macOS launchd integration: keep the daemon alive across logins and reboots."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .config import agent_home

LABEL = "com.fantasyagent.rooting"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def build_plist(python: str | None = None, workdir: str | None = None) -> dict:
    python = python or sys.executable
    workdir = workdir or str(Path(__file__).resolve().parent.parent)
    logs = agent_home()
    return {
        "Label": LABEL,
        "ProgramArguments": [python, "-m", "app.cli", "daemon", "--poll", "30"],
        "WorkingDirectory": workdir,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False, "Crashed": True},
        "ThrottleInterval": 30,
        "StandardOutPath": str(logs / "daemon.out.log"),
        "StandardErrorPath": str(logs / "daemon.err.log"),
        "ProcessType": "Background",
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
            "PYTHONPATH": workdir,
            "PYTHONUNBUFFERED": "1",
            "FANTASY_AGENT_HOME": str(agent_home()),
            "TZ": "America/Los_Angeles",
        },
    }


def install(python: str | None = None, workdir: str | None = None) -> str:
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(build_plist(python, workdir)))

    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    boot = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)],
                          capture_output=True, text=True)
    if boot.returncode != 0:
        legacy = subprocess.run(["launchctl", "load", "-w", str(path)],
                                capture_output=True, text=True)
        if legacy.returncode != 0:
            return (f"Wrote {path} but launchctl refused to load it:\n"
                    f"  {boot.stderr.strip()}\n  {legacy.stderr.strip()}\n"
                    f"Load it manually with:  launchctl bootstrap gui/{uid} {path}")
    subprocess.run(["launchctl", "enable", f"gui/{uid}/{LABEL}"], capture_output=True)
    return (f"Installed and started {LABEL}\n"
            f"  plist: {path}\n"
            f"  logs:  {agent_home()}/daemon.out.log\n"
            f"Check it with:  launchctl print gui/{uid}/{LABEL} | head -20")


def uninstall() -> str:
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    subprocess.run(["launchctl", "unload", str(plist_path())], capture_output=True)
    if plist_path().exists():
        plist_path().unlink()
        return f"Removed {plist_path()} and stopped the daemon."
    return "Nothing installed."
