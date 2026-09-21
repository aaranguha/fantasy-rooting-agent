"""Dispatches a Decision's moves to Sleeper - or, in dry-run mode, just
describes what would have happened. This is the only place that calls the
Playwright automation in ``app.providers.sleeper_write``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..providers import sleeper_write as sw
from .decide import Decision
from .state import LeagueManagerState

log = logging.getLogger(__name__)


def default_session_path() -> Path:
    env = os.getenv("SLEEPER_SESSION_PATH")
    return Path(env).expanduser() if env else Path.home() / ".fantasy-agent" / "sleeper_session.json"


def apply_decision(state: LeagueManagerState, decision: Decision, *,
                    dry_run: bool = True) -> tuple[list[str], list[str]]:
    """Returns (executed, failed) human-readable descriptions.

    In dry-run mode nothing touches Sleeper - everything is reported as
    "would do X" so the Telegram message always reflects reality.
    """
    executed: list[str] = []
    failed: list[str] = []
    state_path = default_session_path()

    if dry_run:
        for c in decision.lineup_changes:
            executed.append(f"[dry-run] would start {c['start_player_name']} "
                            f"over {c['bench_player_name']} at {c['slot']}")
        for c in decision.waiver_claims:
            executed.append(f"[dry-run] would claim {c['add_player_name']}"
                            + (f" (drop {c['drop_player_name']})" if c.get("drop_player_name") else "")
                            + (f" for ${c['faab_bid']} FAAB" if c.get("faab_bid") else ""))
        for t in decision.trade_proposals:
            executed.append(f"[dry-run] would propose to {t['target_team_name']}: "
                            f"give {t['give']}, get {t['get']}")
        return executed, failed

    if decision.lineup_changes:
        result = sw.set_lineup(state_path, state.league_id, state.my_roster.roster_id,
                               decision.lineup_changes)
        (executed if result.ok else failed).append(result.detail)

    for c in decision.waiver_claims:
        result = sw.submit_waiver_claim(
            state_path, state.league_id,
            add_player_name=c["add_player_name"],
            drop_player_name=c.get("drop_player_name", ""),
            faab_bid=int(c.get("faab_bid") or 0),
        )
        (executed if result.ok else failed).append(result.detail)

    for t in decision.trade_proposals:
        result = sw.propose_trade(
            state_path, state.league_id,
            target_roster_id=t["target_roster_id"], give=t["give"], get=t["get"],
        )
        (executed if result.ok else failed).append(result.detail)

    return executed, failed
