"""Orchestrator: gather -> decide -> (dry-run log | execute) -> notify.

This is what `fantasy-agent manage-league` and the league-manager.yml
GitHub Actions workflow both call.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .actions import apply_decision, default_session_path
from .decide import decide
from .notify import send_update
from .state import gather_state

log = logging.getLogger(__name__)


def run(league_id: str, sleeper_username: str, *, live: bool = False) -> dict:
    """Returns a summary dict for CLI/log output. live=False (dry-run) is the
    default - a caller must ask for --live explicitly, and even then this
    silently falls back to dry-run if no Sleeper session has been captured
    yet, so a misconfigured secret can never look like a successful trade."""
    state = gather_state(league_id, sleeper_username)

    effective_live = live
    if live and not default_session_path().exists():
        log.warning("league_manager: --live requested but no Sleeper session at %s - "
                    "falling back to dry-run", default_session_path())
        effective_live = False

    decision = decide(state)
    executed, failed = apply_decision(state, decision, dry_run=not effective_live)
    ok, detail = send_update(state.league_name, decision, dry_run=not effective_live,
                             executed=executed, failed=failed)

    return {
        "league": state.league_name,
        "week": state.week,
        "live": effective_live,
        "lineup_changes": len(decision.lineup_changes),
        "waiver_claims": len(decision.waiver_claims),
        "trade_proposals": len(decision.trade_proposals),
        "executed": executed,
        "failed": failed,
        "telegram_sent": ok,
        "telegram_detail": detail,
    }
