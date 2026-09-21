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


def format_message(league_name: str, decision: Decision, *, dry_run: bool,
                    executed: list[str], failed: list[str]) -> tuple[str, str]:
    mode = "🧪 DRY RUN — nothing sent to Sleeper" if dry_run else "✅ LIVE"
    title = f"🏈 {league_name} — {mode}"

    lines = [decision.summary.strip(), ""]

    if decision.lineup_changes:
        lines.append("📋 Lineup:")
        for c in decision.lineup_changes:
            lines.append(f"  {c['slot']}: {c['bench_player_name']} → {c['start_player_name']}")
            lines.append(f"    ({c['reasoning']})")
    if decision.waiver_claims:
        lines.append("\n🔄 Waivers:")
        for c in decision.waiver_claims:
            drop = f" / drop {c['drop_player_name']}" if c.get("drop_player_name") else ""
            bid = f" — ${c['faab_bid']} FAAB" if c.get("faab_bid") else ""
            lines.append(f"  +{c['add_player_name']}{drop}{bid}")
            lines.append(f"    ({c['reasoning']})")
    if decision.trade_proposals:
        lines.append("\n🤝 Trade proposals:")
        for t in decision.trade_proposals:
            lines.append(f"  To {t['target_team_name']}: give {', '.join(t['give'])} "
                         f"for {', '.join(t['get'])}")
            lines.append(f"    ({t['reasoning']})")
    if not decision.has_actions:
        lines.append("No moves this check-in — lineup and roster already look right.")
    if decision.notes:
        lines.append(f"\n📝 {decision.notes}")
    if executed:
        lines.append("\n✅ Executed: " + "; ".join(executed))
    if failed:
        lines.append("\n⚠️ Failed to execute (check manually in the app): " + "; ".join(failed))
    lines.append(f"\n[{decision.searches_used} web searches this run]")

    return title, "\n".join(lines)


def send_update(league_name: str, decision: Decision, *, dry_run: bool,
                 executed: list[str], failed: list[str]) -> tuple[bool, str]:
    bot = _bot()
    ok, why = bot.available()
    if not ok:
        return False, f"Telegram not configured: {why}"
    title, body = format_message(league_name, decision, dry_run=dry_run,
                                  executed=executed, failed=failed)
    result = bot.send(title, body)
    return result.ok, result.detail
