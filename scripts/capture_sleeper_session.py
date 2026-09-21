#!/usr/bin/env python3
"""One-time, interactive: opens a real Chromium window so YOU log into
Sleeper normally (username/password, 2FA, whatever you use). This script
never sees your password - it only reads the resulting session cookies once
you're logged in, and saves them locally.

Run it from the project root with the venv active:

    python3 scripts/capture_sleeper_session.py

The saved file (~/.fantasy-agent/sleeper_session.json, chmod 600) is what
`fantasy-agent manage-league --live` and app/providers/sleeper_write.py use
to act on your roster. Sleeper sessions eventually expire - re-run this
whenever `manage-league --live` starts reporting "session expired".

To let the GitHub Actions workflow use it too, push its contents as a repo
secret (see README.md "League Manager" -> step 4) - never commit the file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("playwright is not installed in this environment.\n"
          "  pip install playwright\n"
          "  playwright install chromium")
    sys.exit(1)

OUT_PATH = Path.home() / ".fantasy-agent" / "sleeper_session.json"


def main() -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    print("Opening a Chromium window. Log into Sleeper normally, then come back here")
    print("and press Enter once you can see your dashboard/leagues.\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://sleeper.com/login", wait_until="networkidle")

        input("Press Enter once you're logged in and can see your leagues... ")

        if "login" in page.url:
            print(f"\nStill on {page.url} - doesn't look like login finished. "
                  "Re-run this script and try again.")
            browser.close()
            sys.exit(1)

        state = context.storage_state()
        OUT_PATH.write_text(json.dumps(state))
        OUT_PATH.chmod(0o600)
        browser.close()

    print(f"\nSaved session to {OUT_PATH} (chmod 600).")
    print("Verify it: fantasy-agent verify-sleeper-session")
    print("\nFor the GitHub Actions workflow to use this too, push it as a secret:")
    print(f"  gh secret set SLEEPER_SESSION_STATE < {OUT_PATH}")
    print("Then, since the file has your login session in it, consider deleting the local")
    print("copy if this machine isn't the one running --live checks day to day.")


if __name__ == "__main__":
    main()
