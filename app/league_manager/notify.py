"""Telegram updates for the league manager - deliberately a separate bot
from the primetime rooting notifier (LEAGUE_MANAGER_TELEGRAM_BOT_TOKEN /
_CHAT_ID) so roster-management chatter doesn't mix into the rooting feed.
"""

from __future__ import annotations

import os

from ..notifiers.telegram import TelegramNotifier
from .decide import Decision


def _bot() -> TelegramNotifier:
    return TelegramNotifier(
        token=os.getenv("LEAGUE_MANAGER_TELEGRAM_BOT_TOKEN"),
        chat_id=os.getenv("LEAGUE_MANAGER_TELEGRAM_CHAT_ID"),
    )


def format_message(league_name: str, decision: Decision) -> tuple[str, str]:
    """One line per action, reason folded in - built to read at a glance on
    a phone lock screen, not as a report."""
    title = f"🏈 {league_name}"

    if not decision.has_actions:
        body = decision.summary.strip() or "No moves — lineup's right."
        if decision.notes:
            body += f"\n📝 {decision.notes}"
        return title, body

    lines = [decision.summary.strip()]
    for c in decision.lineup_changes:
        lines.append(f"📋 {c['slot']}: {c['bench_player_name']} → {c['start_player_name']} "
                     f"({c['reasoning']})")
    for c in decision.waiver_claims:
        drop = f", drop {c['drop_player_name']}" if c.get("drop_player_name") else ""
        bid = f" ${c['faab_bid']}FAAB" if c.get("faab_bid") else ""
        lines.append(f"🔄 +{c['add_player_name']}{drop}{bid} ({c['reasoning']})")
    for t in decision.trade_proposals:
        lines.append(f"🤝 {t['target_team_name']}: give {', '.join(t['give'])} for "
                     f"{', '.join(t['get'])} ({t['reasoning']})")
    if decision.notes:
        lines.append(f"📝 {decision.notes}")

    return title, "\n".join(lines)


def send_update(league_name: str, decision: Decision) -> tuple[bool, str]:
    bot = _bot()
    ok, why = bot.available()
    if not ok:
        return False, f"Telegram not configured: {why}"
    title, body = format_message(league_name, decision)
    result = bot.send(title, body)
    return result.ok, result.detail
