"""Gathers everything needed to manage one Sleeper league: rosters, the
current matchup, the free-agent pool and recent league transactions.

Read-only - Sleeper's public API has no write endpoints, and this module
only ever reads anyway: app.league_manager stays recommend-only (see
app/league_manager/actions.py for why) rather than touching Sleeper directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..config import cache_dir
from ..providers.base import HttpClient, ProviderError

API = "https://api.sleeper.app/v1"

# Sleeper's full player dump includes retired players, practice-squad-only
# names and non-fantasy positions. Keep the free-agent pool to what a
# manager could plausibly add.
FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}

#: How many unrostered players to hand the model, ranked by Sleeper's own
#: `search_rank` (lower = more relevant). Keeps the prompt bounded.
FREE_AGENT_LIMIT = 150


@dataclass
class PlayerRef:
    id: str
    name: str
    position: str
    team: Optional[str]
    injury_status: str = ""

    def line(self) -> str:
        bits = [self.name, self.position, self.team or "FA"]
        if self.injury_status:
            bits.append(self.injury_status)
        return " · ".join(bits)


@dataclass
class RosterView:
    roster_id: str
    owner_name: str
    is_me: bool
    starters: list[PlayerRef]
    bench: list[PlayerRef]
    record: str = ""


@dataclass
class LeagueManagerState:
    league_id: str
    league_name: str
    season: int
    week: int
    scoring_format: str
    waiver_type: str  # "faab" | "rolling" | "reverse_standings"
    waiver_budget_total: int
    roster_positions: list[str]
    my_roster: RosterView
    opponent: Optional[RosterView]
    other_rosters: list[RosterView]
    free_agents: list[PlayerRef]
    recent_transactions: list[str]  # human-readable one-liners, most recent first


def _http() -> HttpClient:
    return HttpClient(cache_dir=cache_dir())


def _player_dump(http: HttpClient) -> dict[str, dict]:
    return http.get(f"{API}/players/nfl", cache_ttl=1800) or {}


def _resolve(pid: str, dump: dict[str, dict]) -> PlayerRef:
    if pid == "0" or pid is None:
        return PlayerRef(id=str(pid), name="(empty)", position="", team=None)
    p = dump.get(str(pid)) or {}
    pos = p.get("position") or (p.get("fantasy_positions") or [""])[0] or ""
    name = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() \
        or f"player {pid}"
    return PlayerRef(
        id=str(pid), name=name, position=pos, team=p.get("team"),
        injury_status=p.get("injury_status") or "",
    )


def _roster_view(raw: dict, users_by_id: dict[str, dict], my_user_id: str,
                  dump: dict[str, dict]) -> RosterView:
    starter_ids = raw.get("starters") or []
    all_ids = raw.get("players") or []
    bench_ids = [p for p in all_ids if p not in starter_ids]
    user = users_by_id.get(str(raw.get("owner_id")), {})
    owner_name = user.get("display_name") or user.get("username") or f"team {raw.get('roster_id')}"
    meta = raw.get("metadata") or {}
    wins = (raw.get("settings") or {}).get("wins")
    losses = (raw.get("settings") or {}).get("losses")
    record = f"{wins}-{losses}" if wins is not None else meta.get("record", "")
    return RosterView(
        roster_id=str(raw.get("roster_id")),
        owner_name=owner_name,
        is_me=str(raw.get("owner_id")) == str(my_user_id),
        starters=[_resolve(p, dump) for p in starter_ids],
        bench=[_resolve(p, dump) for p in bench_ids],
        record=record,
    )


def _transactions(http: HttpClient, league_id: str, week: int,
                   dump: dict[str, dict], limit: int = 15) -> list[str]:
    try:
        raw = http.get(f"{API}/league/{league_id}/transactions/{week}", cache_ttl=300) or []
    except ProviderError:
        return []
    lines: list[str] = []
    for t in sorted(raw, key=lambda x: x.get("status_updated", 0), reverse=True)[:limit]:
        kind = t.get("type", "?")
        adds = ", ".join(_resolve(pid, dump).name for pid in (t.get("adds") or {}).keys())
        drops = ", ".join(_resolve(pid, dump).name for pid in (t.get("drops") or {}).keys())
        bid = (t.get("settings") or {}).get("waiver_bid")
        bid_note = f" (${bid} FAAB)" if bid is not None else ""
        bits = [b for b in (f"+{adds}" if adds else "", f"-{drops}" if drops else "") if b]
        if bits:
            lines.append(f"{kind}: {' '.join(bits)}{bid_note}")
    return lines


def gather_state(league_id: str, sleeper_username: str) -> LeagueManagerState:
    """Pull everything needed to make roster decisions for this week."""
    http = _http()
    dump = _player_dump(http)

    league = http.get(f"{API}/league/{league_id}", cache_ttl=600) or {}
    nfl_state = http.get(f"{API}/state/nfl", cache_ttl=300) or {}
    week = int(nfl_state.get("week") or 1)
    season = int(league.get("season") or nfl_state.get("season") or 0)

    users = http.get(f"{API}/league/{league_id}/users", cache_ttl=1800) or []
    users_by_id = {str(u.get("user_id")): u for u in users}

    user_info = http.get(f"{API}/user/{sleeper_username}", cache_ttl=3600) or {}
    my_user_id = str(user_info.get("user_id") or "")

    rosters_raw = http.get(f"{API}/league/{league_id}/rosters", cache_ttl=60) or []
    rosters = [_roster_view(r, users_by_id, my_user_id, dump) for r in rosters_raw]
    my_roster = next((r for r in rosters if r.is_me), None)
    if my_roster is None:
        raise ProviderError(
            f"{sleeper_username!r} does not own a roster in league {league_id}")

    matchups = http.get(f"{API}/league/{league_id}/matchups/{week}", cache_ttl=0) or []
    my_matchup_id = next(
        (m.get("matchup_id") for m in matchups
         if str(m.get("roster_id")) == my_roster.roster_id), None)
    opp_roster_id = next(
        (str(m.get("roster_id")) for m in matchups
         if m.get("matchup_id") == my_matchup_id and str(m.get("roster_id")) != my_roster.roster_id),
        None,
    ) if my_matchup_id is not None else None
    opponent = next((r for r in rosters if r.roster_id == opp_roster_id), None)

    rostered_ids = {pid for r in rosters_raw for pid in (r.get("players") or [])}
    free_agent_raw = [
        (pid, p) for pid, p in dump.items()
        if pid not in rostered_ids
        and (p.get("position") in FANTASY_POSITIONS)
        and p.get("team")
    ]
    free_agent_raw.sort(key=lambda kv: kv[1].get("search_rank") or 999999)
    free_agents = [_resolve(pid, dump) for pid, _ in free_agent_raw[:FREE_AGENT_LIMIT]]

    settings = league.get("settings") or {}
    waiver_type_code = settings.get("waiver_type", 2)
    waiver_type = {0: "rolling", 1: "reverse_standings", 2: "faab"}.get(waiver_type_code, "faab")

    return LeagueManagerState(
        league_id=league_id,
        league_name=league.get("name", league_id),
        season=season,
        week=week,
        scoring_format=("dynasty/keeper" if settings.get("type") else "redraft"),
        waiver_type=waiver_type,
        waiver_budget_total=int(settings.get("waiver_budget") or 0),
        roster_positions=league.get("roster_positions") or [],
        my_roster=my_roster,
        opponent=opponent,
        other_rosters=[r for r in rosters if not r.is_me and r.roster_id != opp_roster_id],
        free_agents=free_agents,
        recent_transactions=_transactions(http, league_id, week, dump),
    )
