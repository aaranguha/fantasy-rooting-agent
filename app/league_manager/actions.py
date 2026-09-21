"""Turns a Decision into human-readable recommendations.

Sleeper has no write API, and driving the site via browser automation ran
straight into Sleeper's own hCaptcha-based bot detection (confirmed by hand -
see git history for the full story). Rather than fight a real anti-bot
system, this stays recommend-only on purpose: it never touches your Sleeper
account, it just tells you exactly what to do and you tap it in the app
yourself.
"""

from __future__ import annotations

from .decide import Decision


def describe_decision(decision: Decision) -> list[str]:
    """Human-readable action lines for the Telegram message."""
    lines: list[str] = []
    for c in decision.lineup_changes:
        lines.append(f"Start {c['start_player_name']} over {c['bench_player_name']} at {c['slot']}")
    for c in decision.waiver_claims:
        drop = f" (drop {c['drop_player_name']})" if c.get("drop_player_name") else ""
        bid = f" for ${c['faab_bid']} FAAB" if c.get("faab_bid") else ""
        lines.append(f"Claim {c['add_player_name']}{drop}{bid}")
    for t in decision.trade_proposals:
        lines.append(f"Propose to {t['target_team_name']}: give {t['give']}, get {t['get']}")
    return lines
