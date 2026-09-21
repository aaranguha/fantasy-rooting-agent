"""Orchestrator: gather -> decide -> recommend -> notify.

Recommend-only, permanently - see app/league_manager/actions.py for why.
This is what `fantasy-agent manage-league` and the league-manager.yml
GitHub Actions workflow both call.
"""

from __future__ import annotations

from .actions import describe_decision
from .decide import decide
from .notify import send_update
from .state import gather_state


def run(league_id: str, sleeper_username: str) -> dict:
    """Returns a summary dict for CLI/log output."""
    state = gather_state(league_id, sleeper_username)
    decision = decide(state)
    recommendations = describe_decision(decision)
    ok, detail = send_update(state.league_name, decision)

    return {
        "league": state.league_name,
        "week": state.week,
        "lineup_changes": len(decision.lineup_changes),
        "waiver_claims": len(decision.waiver_claims),
        "trade_proposals": len(decision.trade_proposals),
        "recommendations": recommendations,
        "telegram_sent": ok,
        "telegram_detail": detail,
    }
