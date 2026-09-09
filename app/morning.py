"""Gameday-morning preview: the same push you'd get at kickoff, just sent hours
earlier so there's still time to fix a lineup.

`Scheduler.morning_tick` (in scheduler.py) builds each preview with the exact
same `phone_title`/`phone_message` calls the T-minus-kickoff push uses - this
module only supplies the timing: which local time to fire at, and which games
fall on "today". When the real kickoff push later lands, it deletes this one
(where the provider supports it - Telegram does, most others don't), so you end
up with one message per game rather than a stale preview plus a fresh one.
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from .models import NFLGame

#: How late we'll still fire if the machine was asleep exactly at the target time.
LATE_GRACE_MINUTES = 180


def parse_hhmm(value: str) -> dtime:
    try:
        h, m = value.strip().split(":")
        return dtime(hour=int(h), minute=int(m))
    except (ValueError, TypeError):
        return dtime(hour=9, minute=0)


def games_today(games: list[NFLGame], tz: ZoneInfo, *, on: Optional[date] = None) -> list[NFLGame]:
    """Primetime games whose kickoff falls on the given local date (default: today)."""
    day = on or datetime.now(tz).date()
    return sorted((g for g in games if g.kickoff.astimezone(tz).date() == day),
                 key=lambda g: g.kickoff)


def is_due(target: dtime, tz: ZoneInfo, *, now: Optional[datetime] = None,
          grace_minutes: int = LATE_GRACE_MINUTES) -> bool:
    """True during the window [target, target + grace] in local time."""
    now = (now or datetime.now(tz)).astimezone(tz)
    fire_at = now.replace(hour=target.hour, minute=target.minute, second=0, microsecond=0)
    return fire_at <= now <= fire_at + timedelta(minutes=grace_minutes)
