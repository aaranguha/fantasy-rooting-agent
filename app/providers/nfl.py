"""NFL schedule, live game state and primetime-slot detection.

Nothing here is hardcoded per week.  Slots are derived from ESPN's public
scoreboard feed: kickoff time in US/Eastern, the broadcast network, and how many
other games share the same window.  That means flexed games, Wednesday openers,
Black Friday, Christmas, London games and double-MNF all classify correctly
without a code change.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from ..config import cache_dir
from ..models import NFLGame, PlayerGameState, SlotType, normalize_team
from .base import HttpClient, ProviderError

log = logging.getLogger(__name__)

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
EASTERN = ZoneInfo("America/New_York")

# Networks that only ever carry standalone national windows.
NATIONAL_NETS = {
    "NBC", "ESPN", "ABC", "ESPN2", "AMAZON", "PRIME VIDEO", "PRIME",
    "NFLN", "NFL NETWORK", "NETFLIX", "PEACOCK", "ESPN+",
}
# Regional Sunday-afternoon partners.
REGIONAL_NETS = {"CBS", "FOX"}


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


class NFLScheduleProvider:
    def __init__(self, client: Optional[HttpClient] = None) -> None:
        self.http = client or HttpClient(cache_dir=cache_dir())

    # -- fetching -----------------------------------------------------------
    def current_week(self) -> tuple[int, int, int]:
        """(season, week, season_type) straight from the live scoreboard feed."""
        data = self.http.get(SCOREBOARD, cache_ttl=600)
        season = data.get("season", {})
        week = data.get("week", {}).get("number", 1)
        return int(season.get("year", 0)), int(week), int(season.get("type", 2))

    def games(self, season: int, week: int, season_type: int = 2,
              *, cache_ttl: float = 120) -> list[NFLGame]:
        """All games in a week, with slot classification and live state."""
        data = self.http.get(
            SCOREBOARD,
            params={"dates": season, "seasontype": season_type, "week": week},
            cache_ttl=cache_ttl,
        )
        games = [self._parse_event(e, season, week, season_type) for e in data.get("events", [])]
        games = [g for g in games if g]
        classify_slots(games)
        games.sort(key=lambda g: g.kickoff)
        return games

    def _parse_event(self, e: dict, season: int, week: int, stype: int) -> Optional[NFLGame]:
        try:
            comp = e["competitions"][0]
            teams = {c["homeAway"]: c for c in comp["competitors"]}
            home, away = teams["home"], teams["away"]
            status = comp.get("status", {}).get("type", {}) or e.get("status", {}).get("type", {})
            state = {
                "pre": PlayerGameState.NOT_STARTED,
                "in": PlayerGameState.IN_PROGRESS,
                "post": PlayerGameState.FINAL,
            }.get(status.get("state", "pre"), PlayerGameState.NOT_STARTED)
            nets: list[str] = []
            for b in comp.get("broadcasts") or []:
                nets.extend(b.get("names") or ([b["media"]["shortName"]] if b.get("media") else []))
            return NFLGame(
                id=str(e["id"]),
                away=normalize_team(away["team"].get("abbreviation")),
                home=normalize_team(home["team"].get("abbreviation")),
                kickoff=_parse_iso(e["date"]),
                broadcast="/".join(dict.fromkeys(nets)),
                week=week,
                season=season,
                season_type=stype,
                state=state,
                period=int(comp.get("status", {}).get("period", 0) or 0),
                clock=str(comp.get("status", {}).get("displayClock", "") or ""),
                away_score=int(away.get("score", 0) or 0),
                home_score=int(home.get("score", 0) or 0),
                name=e.get("shortName", ""),
            )
        except (KeyError, IndexError, ValueError) as exc:  # pragma: no cover
            log.warning("Skipping unparseable NFL event: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Slot classification
# ---------------------------------------------------------------------------


def classify_slots(games: list[NFLGame]) -> None:
    """Tag each game with a SlotType, in place.

    A game is 'primetime' for our purposes when it is a *standalone national
    window*: at most two games kick off within 90 minutes of it and it is not on
    a regional-only broadcast.  We then name the window by its Eastern-time day
    and hour so TNF/SNF/MNF read naturally, and fall back to PRIMETIME for the
    weird ones (Wednesday openers, Black Friday, Saturday specials).
    """
    for g in games:
        window = [o for o in games if abs((o.kickoff - g.kickoff).total_seconds()) <= 90 * 60]
        standalone = len(window) <= 2
        nets = {n.strip().upper() for n in g.broadcast.split("/") if n.strip()}
        national = bool(nets & NATIONAL_NETS) or not (nets & REGIONAL_NETS)
        et = g.kickoff.astimezone(EASTERN)
        evening = et.hour >= 17 or et.hour < 4

        if not standalone:
            g.slot = SlotType.REGULAR
            continue

        if not national and not evening:
            # A lone regional early game (rare) is not a rooting event.
            g.slot = SlotType.REGULAR
            continue

        weekday = et.weekday()  # Mon=0 ... Sun=6
        if evening and weekday == 3:
            g.slot = SlotType.TNF
        elif evening and weekday == 6:
            g.slot = SlotType.SNF
        elif evening and weekday == 0:
            g.slot = SlotType.MNF
        elif (et.month, et.day) in ((11, 27), (11, 28), (12, 25)) or (
            weekday == 3 and et.month == 11 and 22 <= et.day <= 28
        ):
            g.slot = SlotType.HOLIDAY
        elif not evening and standalone and national and et.hour < 14:
            g.slot = SlotType.INTERNATIONAL
        else:
            g.slot = SlotType.PRIMETIME

    # Late Sunday-afternoon doubleheader leftovers are never SNF.
    for g in games:
        et = g.kickoff.astimezone(EASTERN)
        if g.slot == SlotType.SNF and et.hour < 19:
            g.slot = SlotType.REGULAR


def label_slot(game: NFLGame, tz: ZoneInfo) -> str:
    """Human label including the local day, e.g. 'MNF (Mon 5:15 PM PT)'."""
    local = game.kickoff.astimezone(tz)
    return f"{game.slot.value} ({local:%a %-I:%M %p})"


def primetime_games(games: list[NFLGame], include: Optional[list[str]] = None) -> list[NFLGame]:
    allowed = set(include or [s.value for s in SlotType if s != SlotType.REGULAR])
    return [g for g in games if g.slot.value in allowed]


def team_game_index(games: list[NFLGame]) -> dict[str, NFLGame]:
    idx: dict[str, NFLGame] = {}
    for g in games:
        idx[g.away] = g
        idx[g.home] = g
    return idx


def upcoming(games: list[NFLGame], *, now: Optional[datetime] = None,
             within: timedelta = timedelta(days=8)) -> list[NFLGame]:
    now = now or datetime.now(timezone.utc)
    return [g for g in games if now <= g.kickoff <= now + within]
