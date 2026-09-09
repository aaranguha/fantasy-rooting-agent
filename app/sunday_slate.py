"""Sunday early/late slate coverage.

Sundays are different from the other primetime nights: there isn't just one
game, there are a full early window (~1:00 ET) and late window (~4:05/4:25 ET)
of regular games before SNF even kicks off. A per-game "morning preview" the
way TNF/MNF get one doesn't make sense here - nobody wants ten separate
messages. Instead:

  1. One condensed digest in the morning, covering BOTH windows at once,
     showing only the strong signals (🚀 🟢 🟥 🔴/☠️) - not the full per-player
     breakdown, and not the wishy-washy middle (🟡 🟧).
  2. A second condensed digest 15 minutes before the LATE window kicks off,
     scoped to just that window's players, recomputed on real early-window
     results now that they're mostly known.
  3. SNF itself is untouched - it still gets the normal, full T-15 push
     (spec.scheduler.tick / notify_game), exactly like TNF and MNF.

`Scheduler.morning_tick` is told to skip SNF specifically on Sundays so this
digest replaces it rather than stacking alongside it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from .analysis import GameGuide
from .formatting import display_name
from .models import NFLGame, SlotType
from .rooting import PlayerRooting

EASTERN = ZoneInfo("America/New_York")
#: ET hour that splits the early window from the late window. Real NFL windows
#: are ~1:00 and ~4:05/4:25 ET, so anything at or past 3pm ET is "late."
LATE_WINDOW_ET_HOUR = 15

#: Only these carry a strong enough signal to earn a line in a QUICK digest.
#: The toss-up (🟡) and mild-lean (🟧) colors are deliberately left out here -
#: full nuance still lives in `fantasy-agent game`/`player`/the T-15 push.
_ROCKET, _GREEN, _RED_SQUARE, _RED_CIRCLE, _SKULL = (
    "\U0001f680", "\U0001f7e2", "\U0001f7e5", "\U0001f534", "☠️",
)
BUCKET_ORDER = [
    ("GO OFF", _ROCKET, {_ROCKET}),
    ("LEAN OURS", _GREEN, {_GREEN}),
    ("LEAN AGAINST", _RED_SQUARE, {_RED_SQUARE}),
    ("FADE", _RED_CIRCLE, {_RED_CIRCLE, _SKULL}),
]

MAX_PER_BUCKET = 10


def is_sunday(tz: ZoneInfo, *, now: Optional[datetime] = None) -> bool:
    return (now or datetime.now(tz)).astimezone(tz).weekday() == 6  # Mon=0 ... Sun=6


def slate_window(game: NFLGame) -> Optional[str]:
    """'early' or 'late' for a Sunday REGULAR game; None for anything else
    (primetime games, or a non-Sunday game) - those are out of scope here."""
    if game.slot != SlotType.REGULAR:
        return None
    et = game.kickoff.astimezone(EASTERN)
    if et.weekday() != 6:
        return None
    return "late" if et.hour >= LATE_WINDOW_ET_HOUR else "early"


def slate_games(games: list[NFLGame], *, window: Optional[str] = None,
                on: Optional[date] = None, tz: Optional[ZoneInfo] = None) -> list[NFLGame]:
    """Sunday REGULAR games, optionally narrowed to one window and/or one
    local calendar date."""
    tz = tz or EASTERN
    out = []
    for g in games:
        w = slate_window(g)
        if w is None:
            continue
        if window and w != window:
            continue
        if on and g.kickoff.astimezone(tz).date() != on:
            continue
        out.append(g)
    return sorted(out, key=lambda g: g.kickoff)


def earliest_kickoff(games: list[NFLGame]) -> Optional[datetime]:
    return min((g.kickoff for g in games), default=None)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _bucket(players: list[PlayerRooting]) -> dict[str, list[PlayerRooting]]:
    buckets: dict[str, list[PlayerRooting]] = {label: [] for label, _, _ in BUCKET_ORDER}
    for p in players:
        for label, _, emojis in BUCKET_ORDER:
            if p.emoji in emojis:
                buckets[label].append(p)
                break  # a player's own .emoji is always exactly one of these
    for label in buckets:
        buckets[label].sort(key=lambda p: -p.dollar_swing)
    return buckets


def _name(p: PlayerRooting) -> str:
    return (f"{p.emoji} {display_name(p.player)}" if p.emoji == "☠️"
            else display_name(p.player))


def condensed_digest(guides: list[GameGuide]) -> str:
    """The whole point of this module: names only, strong signals only."""
    seen: dict[str, PlayerRooting] = {}
    for g in guides:
        for p in g.relevant:
            seen.setdefault(p.player.key, p)  # a player is in exactly one game

    buckets = _bucket(list(seen.values()))
    lines = []
    for label, emoji, _ in BUCKET_ORDER:
        players = buckets[label]
        if not players:
            continue
        names = [_name(p) for p in players[:MAX_PER_BUCKET]]
        extra = len(players) - len(names)
        tail = f" (+{extra} more)" if extra > 0 else ""
        lines.append(f"{emoji} {label}\n" + ", ".join(names) + tail)

    if not lines:
        return "Nothing with a strong signal either way - a quiet slate for us."
    return "\n\n".join(lines)


def sunday_morning_title(tz: ZoneInfo, *, now: Optional[datetime] = None) -> str:
    today = (now or datetime.now(tz)).astimezone(tz)
    return f"\U0001f3c8 Sunday Slate — {today:%b %-d}"


def second_slate_title(now: Optional[datetime] = None,
                       tz: Optional[ZoneInfo] = None) -> str:
    return "\U0001f3c8 Second Slate in 15 — updated for early-window results"
