"""Executes roster moves on sleeper.com by driving the real web app.

Sleeper's public API (``app.providers.sleeper``) is read-only - there is no
supported way to add/drop, submit a waiver claim, set a lineup or propose a
trade programmatically. This module is the workaround: Playwright drives an
actual logged-in browser session and clicks through the same UI a human
would.

STATUS: best-effort. The selectors below were written from Sleeper's general
UI patterns, not verified against a live logged-in session (this module was
built without one). Before the first `--live` run, use `verify()` against a
real captured session and fix any selector that doesn't match - see
scripts/capture_sleeper_session.py and README.md "League Manager" section.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

log = logging.getLogger(__name__)

BASE = "https://sleeper.com"


class SleeperWriteError(RuntimeError):
    """Automation failed - selector mismatch, session expired, network error, etc."""


@dataclass
class ActionResult:
    ok: bool
    detail: str
    screenshot_path: Optional[str] = None


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment issue, not a code bug
        raise SleeperWriteError(
            "playwright is not installed. Run: pip install playwright && playwright install chromium"
        ) from exc
    return sync_playwright


@contextmanager
def session(state_path: Path, *, headless: bool = True) -> Iterator["object"]:
    """Yields a Playwright page loaded with a previously captured login session.

    Raises SleeperWriteError if the session file is missing or Sleeper no
    longer considers it logged in (session expired - re-run the capture script).
    """
    if not state_path.exists():
        raise SleeperWriteError(
            f"No Sleeper session at {state_path}. Run scripts/capture_sleeper_session.py first.")

    sync_playwright = _require_playwright()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(storage_state=str(state_path))
        page = context.new_page()
        page.goto(f"{BASE}/dashboard", wait_until="networkidle", timeout=30000)
        if "login" in page.url:
            browser.close()
            raise SleeperWriteError(
                "Sleeper session expired (redirected to login). "
                "Re-run scripts/capture_sleeper_session.py to capture a fresh one.")
        try:
            yield page
        finally:
            browser.close()


def verify(state_path: Path, *, screenshot_to: Optional[Path] = None) -> ActionResult:
    """Smoke test: confirms the stored session is actually logged in.

    Run this after capturing a session, and again any time a --live run
    starts failing, before assuming the selectors below are wrong.
    """
    try:
        with session(state_path) as page:
            title = page.title()
            if screenshot_to:
                page.screenshot(path=str(screenshot_to))
            return ActionResult(ok=True, detail=f"Logged in - page title: {title!r}",
                                 screenshot_path=str(screenshot_to) if screenshot_to else None)
    except SleeperWriteError as exc:
        return ActionResult(ok=False, detail=str(exc))


def set_lineup(state_path: Path, league_id: str, roster_id: str,
               moves: list[dict], *, headless: bool = True) -> ActionResult:
    """moves: [{"slot": "RB", "start_player_name": ..., "bench_player_name": ...}, ...]

    Drives the matchup/roster page: opens the lineup editor, drags/clicks the
    bench player into the named slot for each move, then saves.
    """
    with session(state_path, headless=headless) as page:
        page.goto(f"{BASE}/leagues/{league_id}/team/{roster_id}", wait_until="networkidle",
                   timeout=30000)
        for m in moves:
            try:
                # Sleeper's roster page lists starters and bench as rows; clicking a
                # bench player's row opens a "move to <slot>" action sheet.
                page.get_by_text(m["bench_player_name"], exact=False).first.click(timeout=10000)
                page.get_by_text(f"Start", exact=False).first.click(timeout=5000)
            except Exception as exc:  # noqa: BLE001 - best-effort UI automation
                return ActionResult(
                    ok=False,
                    detail=f"Failed to start {m.get('start_player_name')} over "
                           f"{m.get('bench_player_name')}: {exc}")
        return ActionResult(ok=True, detail=f"Applied {len(moves)} lineup change(s)")


def submit_waiver_claim(state_path: Path, league_id: str, *, add_player_name: str,
                         drop_player_name: str = "", faab_bid: int = 0,
                         headless: bool = True) -> ActionResult:
    with session(state_path, headless=headless) as page:
        page.goto(f"{BASE}/leagues/{league_id}/players", wait_until="networkidle", timeout=30000)
        try:
            page.get_by_placeholder("Search Players").fill(add_player_name)
            page.get_by_text(add_player_name, exact=False).first.click(timeout=10000)
            page.get_by_text("Add", exact=True).first.click(timeout=5000)
            if drop_player_name:
                page.get_by_text(drop_player_name, exact=False).first.click(timeout=10000)
            if faab_bid:
                page.get_by_placeholder("$").fill(str(faab_bid))
            page.get_by_text("Submit Waiver Claim", exact=False).first.click(timeout=5000)
        except Exception as exc:  # noqa: BLE001
            return ActionResult(ok=False, detail=f"Failed to claim {add_player_name}: {exc}")
        return ActionResult(
            ok=True,
            detail=f"Waiver claim submitted: +{add_player_name}"
                   + (f" -{drop_player_name}" if drop_player_name else "")
                   + (f" (${faab_bid} FAAB)" if faab_bid else ""))


def propose_trade(state_path: Path, league_id: str, *, target_roster_id: str,
                   give: list[str], get: list[str], headless: bool = True) -> ActionResult:
    with session(state_path, headless=headless) as page:
        page.goto(f"{BASE}/leagues/{league_id}/trade/{target_roster_id}", wait_until="networkidle",
                   timeout=30000)
        try:
            for name in give:
                page.get_by_text(name, exact=False).first.click(timeout=10000)
            for name in get:
                page.get_by_text(name, exact=False).first.click(timeout=10000)
            page.get_by_text("Propose Trade", exact=False).first.click(timeout=5000)
        except Exception as exc:  # noqa: BLE001
            return ActionResult(ok=False, detail=f"Failed to propose trade: {exc}")
        return ActionResult(
            ok=True,
            detail=f"Trade proposed to roster {target_roster_id}: give {give}, get {get}")
