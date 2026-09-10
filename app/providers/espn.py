"""ESPN Fantasy Football integration (private leagues supported).

Current, most reliable method as of the 2026 season:

  GET https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{year}
      /segments/0/leagues/{leagueId}
      ?view=mMatchupScore&view=mRoster&view=mTeam&view=mSettings
      &scoringPeriodId={week}

  Cookies: espn_s2, SWID   (private leagues only; public leagues need neither)

``lm-api-reads`` is the read replica ESPN's own web app uses.  The legacy
``fantasy.espn.com/apis/v3`` host still resolves but rate-limits and intermittently
302s, so we use lm-api-reads and keep the legacy host as an automatic fallback.

ESPN returns points already computed in the league's own scoring, for both actual
(statSourceId 0) and projected (statSourceId 1) - which is exactly what we want.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..config import LeagueConfig, cache_dir, espn_cookies
from ..models import (
    DataTier, FantasyPlayerExposure, League, LineupStatus, MatchupState, NFLGame,
    Platform, PlayerGameState, ScoringSettings, Side, normalize_injury,
    normalize_team,
)
from ..playerids import ESPN_POSITIONS, PlayerRegistry, espn_dst_team_id
from ..standings import LeagueStandings, TeamRecord, apply_season_weight
from .base import AuthError, HttpClient, ProviderError

log = logging.getLogger(__name__)

READ_HOST = "https://lm-api-reads.fantasy.espn.com"
LEGACY_HOST = "https://fantasy.espn.com"
PATH = "/apis/v3/games/ffl/seasons/{year}/segments/0/leagues/{league_id}"

#: mMatchupScore alone returns only team TOTALS - the per-player roster comes
#: from mBoxscore/mMatchup. Requesting all of them is what the ESPN web app does,
#: and is the difference between named players and anonymous stat lines.
ROSTER_VIEWS = ["mMatchup", "mMatchupScore", "mBoxscore", "mRoster", "mTeam"]

BENCH_SLOT, IR_SLOT = 20, 21
TAXI_SLOTS = {24}
SLOT_NAMES = {
    0: "QB", 2: "RB", 3: "RB/WR", 4: "WR", 5: "WR/TE", 6: "TE", 7: "OP",
    16: "D/ST", 17: "K", 18: "P", 19: "HC", 20: "BE", 21: "IR", 23: "FLEX", 24: "TAXI",
}

# ESPN statIds we care about for describing (not computing) a league's format.
STAT_RECEPTION, STAT_PASS_TD = 53, 4
ESPN_TE_POSITION_ID = 6


class ESPNProvider:
    platform = Platform.ESPN

    def __init__(self, registry: PlayerRegistry, season: int,
                 client: Optional[HttpClient] = None,
                 cookies: Optional[dict[str, str]] = None) -> None:
        self.registry = registry
        self.season = season
        self.cookies = cookies if cookies is not None else espn_cookies()
        self._unidentified = 0
        self.http = client or HttpClient(cookies=self.cookies, cache_dir=cache_dir(), timeout=25.0)
        self.has_auth = bool(self.cookies)

    # -- raw fetch ----------------------------------------------------------
    def _get(self, league_id: str, views: list[str], week: Optional[int] = None,
             cache_ttl: float = 0.0) -> dict:
        """Same as _league_get but with ESPN's repeated ?view= param encoding."""
        path = PATH.format(year=self.season, league_id=league_id)
        params: dict[str, Any] = {"view": views}
        if week:
            params["scoringPeriodId"] = week
        try:
            return self.http.get(READ_HOST + path, params=params, cache_ttl=cache_ttl)
        except AuthError as exc:
            raise AuthError(
                f"ESPN rejected the request for league {league_id}. If it is a private league, "
                f"set ESPN_S2 and ESPN_SWID in .env (see README). Original: {exc}"
            ) from exc
        except ProviderError as exc:
            log.warning("lm-api-reads failed for league %s (%s); trying legacy host", league_id, exc)
            return self.http.get(LEGACY_HOST + path, params=params, cache_ttl=cache_ttl)

    # -- discovery ----------------------------------------------------------
    def league_meta(self, league_id: str) -> dict:
        return self._get(league_id, ["mSettings", "mTeam"], cache_ttl=1800)

    def teams(self, league_id: str) -> list[dict]:
        data = self.league_meta(league_id)
        out = []
        for t in data.get("teams", []):
            out.append({
                "id": str(t.get("id")),
                "name": team_name(t),
                "abbrev": t.get("abbrev", ""),
                "owners": t.get("owners") or ([t["primaryOwner"]] if t.get("primaryOwner") else []),
            })
        return out

    def my_team_id(self, league_id: str, swid: Optional[str] = None) -> Optional[str]:
        """Match the SWID cookie against team owners."""
        swid = (swid or self.cookies.get("SWID", "")).strip()
        if not swid:
            return None
        variants = {swid, swid.strip("{}"), "{" + swid.strip("{}") + "}"}
        for t in self.teams(league_id):
            if variants & {str(o) for o in t["owners"]}:
                return t["id"]
        return None

    # -- standings ----------------------------------------------------------
    def standings(self, league_id: str, my_team_id: str, week: int) -> LeagueStandings:
        """Season records for every team, for the dynamic importance model."""
        try:
            data = self.league_meta(league_id)
        except ProviderError as exc:
            return LeagueStandings(my_team_id=str(my_team_id), current_week=week,
                                   error=f"ESPN standings unavailable: {exc}")

        sched = (data.get("settings", {}) or {}).get("scheduleSettings", {}) or {}
        teams = []
        for t in data.get("teams", []):
            overall = ((t.get("record") or {}).get("overall") or {})
            teams.append(TeamRecord(
                team_id=str(t.get("id")), name=team_name(t),
                wins=int(overall.get("wins") or 0),
                losses=int(overall.get("losses") or 0),
                ties=int(overall.get("ties") or 0),
                points_for=float(overall.get("pointsFor") or 0),
                points_against=float(overall.get("pointsAgainst") or 0)))

        return LeagueStandings(
            teams=teams,
            my_team_id=str(my_team_id),
            playoff_teams=int(sched.get("playoffTeamCount") or max(2, len(teams) // 2)),
            regular_season_weeks=int(sched.get("matchupPeriodCount") or 14),
            current_week=week,
        )

    # -- scoring settings ---------------------------------------------------
    def scoring(self, league_id: str) -> ScoringSettings:
        data = self.league_meta(league_id)
        items = (data.get("settings", {}).get("scoringSettings", {}) or {}).get("scoringItems", [])
        raw, ppr, pass_td, te_prem = {}, 0.0, 4.0, 0.0
        for it in items:
            sid = it.get("statId")
            pts = float(it.get("points", 0) or 0)
            raw[str(sid)] = pts
            if sid == STAT_RECEPTION:
                ppr = pts
                overrides = it.get("pointsOverrides") or {}
                te_over = overrides.get(str(ESPN_TE_POSITION_ID))
                if te_over is not None:
                    te_prem = float(te_over) - pts
            elif sid == STAT_PASS_TD:
                pass_td = pts
        name = data.get("settings", {}).get("name", "")
        return ScoringSettings(raw=raw, ppr=ppr, pass_td=pass_td,
                               te_premium=te_prem, label=f"ESPN {name}".strip())

    # -- the main event -----------------------------------------------------
    def load_matchup(self, cfg: LeagueConfig, week: int, *,
                     game_index: Optional[dict[str, NFLGame]] = None) -> MatchupState:
        scoring = self.scoring(cfg.league_id)
        meta = self.league_meta(cfg.league_id)
        league = League(
            id=cfg.league_id,
            name=cfg.name or meta.get("settings", {}).get("name", cfg.league_id),
            platform=Platform.ESPN,
            buy_in_usd=cfg.buy_in_usd,
            importance_multiplier=cfg.importance_multiplier,
            season=self.season,
            scoring=scoring,
            my_team_id=cfg.my_team_id or None,
            low_priority=cfg.low_priority,
        )
        state = MatchupState(league=league, week=week)

        my_id = str(cfg.my_team_id or self.my_team_id(cfg.league_id) or "")
        if not my_id:
            state.error = ("Could not identify your ESPN team - set my_team_id in config "
                           "or ESPN_SWID in .env")
            state.tier = DataTier.MINIMUM
            return state

        team_names = {str(t["id"]): team_name(t) for t in meta.get("teams", [])}
        league.my_team_name = team_names.get(my_id, "My Team")

        data = self._get(cfg.league_id, ROSTER_VIEWS, week=week)
        pair = self._find_matchup(data, my_id, week)
        if pair is None:
            state.error = f"No week {week} matchup found in ESPN league {league.name}"
            state.tier = DataTier.MINIMUM
            return state
        mine_block, opp_block = pair
        opp_id = str(opp_block.get("teamId", "")) if opp_block else ""
        state.opponent_name = team_names.get(opp_id, "Opponent")

        self._unidentified = 0
        for block, side, starters, bench in (
            (mine_block, Side.MINE, state.my_starters, state.my_bench),
            (opp_block, Side.OPPONENT, state.opp_starters, state.opp_bench),
        ):
            if block:
                self._fill(block, league, week, game_index or {}, side, starters, bench)
        if self._unidentified:
            log.warning(
                "%s: %d ESPN roster entries carried no player id or name and were "
                "skipped. Run scripts/espn_payload_shape.py %s to report the payload shape.",
                league.name, self._unidentified, cfg.league_id)

        state.reported_score_mine = _block_points(mine_block)
        state.reported_score_opponent = _block_points(opp_block)
        if not any(p.has_projection for p in state.all_starters()):
            state.tier = DataTier.GOOD

        # Season context (reuses the cached mTeam payload, so no extra requests).
        standings = self.standings(cfg.league_id, my_id, week)
        standings.punished_places = cfg.punished_places
        apply_season_weight(league, standings,
                            loser_punishment=cfg.loser_punishment,
                            dynamic=cfg.dynamic_importance)
        return state

    def _find_matchup(self, data: dict, my_id: str, week: int):
        """My matchup for this scoring period.

        Normally matchupPeriodId == the week.  In leagues with multi-week playoff
        matchups they diverge, so we fall back to the latest matchup period that
        has already started - never to an arbitrary earlier week.
        """
        candidates: list[tuple[int, tuple[dict, dict]]] = []
        for m in data.get("schedule", []):
            home, away = m.get("home") or {}, m.get("away") or {}
            if str(home.get("teamId")) == my_id:
                pair = (home, away)
            elif str(away.get("teamId")) == my_id:
                pair = (away, home)
            else:
                continue
            period = m.get("matchupPeriodId")
            if period == week:
                return pair
            if isinstance(period, int) and period <= week:
                candidates.append((period, pair))
        if not candidates:
            return None
        return max(candidates, key=lambda c: c[0])[1]

    def _resolve_entry(self, entry: dict) -> tuple[Optional[Any], dict]:
        """Identify the player in one roster entry, whatever shape ESPN sent.

        ESPN's payload is not uniform: depending on the view combination and
        whether the scoring period has started, `playerPoolEntry.player` can come
        back sparse - carrying `stats` but no `id`, `fullName` or
        `defaultPositionId`. The player id is then only on the entry itself.

        So we gather the id from every place it can hide, and let the registry
        (which already indexes the full ESPN player universe) supply the name,
        position and team. Returns (canonical_player, raw_player_dict).
        """
        pool = entry.get("playerPoolEntry") or {}
        p = pool.get("player") or {}

        eid = 0
        for candidate in (entry.get("playerId"), p.get("id"), pool.get("id")):
            try:
                eid = int(candidate)
            except (TypeError, ValueError):
                continue
            if eid:
                break

        name = (p.get("fullName")
                or " ".join(x for x in (p.get("firstName"), p.get("lastName")) if x).strip())
        pos = ESPN_POSITIONS.get(p.get("defaultPositionId")) or ""
        team = self.registry.espn_team_abbrev.get(p.get("proTeamId", 0), "")
        if pos == "DST" or (eid and eid <= -16000):
            dt = espn_dst_team_id(eid)
            if dt is not None:
                team = self.registry.espn_team_abbrev.get(dt, team)
                pos = "DST"

        # The id alone is enough: the registry knows this player already.
        hit = self.registry.resolve(name=name, position=pos, team=team,
                                    espn_id=eid or None)
        if hit is not None:
            return hit, p
        if eid:
            return self.registry.ensure_espn(eid, name, pos, team), p
        # No id and no name - there is nothing to identify him by. Minting a
        # shared placeholder here would silently merge every such player into
        # one, so drop the entry and let the caller report the count.
        self._unidentified += 1
        return None, p

    def _fill(self, block: dict, league: League, week: int,
              game_index: dict[str, NFLGame], side: Side,
              starters: list, bench: list) -> None:
        roster = (block.get("rosterForCurrentScoringPeriod")
                  or block.get("rosterForMatchupPeriod") or {})
        for entry in roster.get("entries", []):
            slot = entry.get("lineupSlotId")
            canonical, p = self._resolve_entry(entry)
            if canonical is None:
                continue

            actual, projected = _entry_points(p, week)
            status = (LineupStatus.BENCH if slot == BENCH_SLOT else
                      LineupStatus.IR if slot == IR_SLOT else
                      LineupStatus.TAXI if slot in TAXI_SLOTS else
                      LineupStatus.STARTER)
            game = game_index.get(normalize_team(canonical.nfl_team))
            gstate = game.state if game else PlayerGameState.BYE_OR_UNKNOWN
            frac = game.fraction_remaining if game else (1.0 if projected else 0.0)

            exp = FantasyPlayerExposure(
                canonical=canonical,
                league=league,
                side=side,
                lineup_status=status,
                slot=SLOT_NAMES.get(slot, str(slot)),
                current_points=round(actual, 2),
                projected_points=round(projected, 2),
                game_state=gstate,
                game_fraction_remaining=frac,
                has_projection=projected > 0,
                injury_status=normalize_injury(p.get("injuryStatus")),
            )
            (starters if status == LineupStatus.STARTER else bench).append(exp)


# ---------------------------------------------------------------------------


def team_name(t: dict) -> str:
    return (t.get("name")
            or f"{t.get('location','')} {t.get('nickname','')}".strip()
            or t.get("abbrev", "")
            or f"Team {t.get('id')}")


def _entry_points(player: dict, week: int) -> tuple[float, float]:
    """(actual, projected) for this scoring period, in the league's own scoring."""
    actual = projected = 0.0
    for s in player.get("stats", []) or []:
        if s.get("scoringPeriodId") != week or s.get("statSplitTypeId") not in (1, None):
            continue
        total = float(s.get("appliedTotal", 0) or 0)
        if s.get("statSourceId") == 0:
            actual = total
        elif s.get("statSourceId") == 1:
            projected = total
    return actual, projected


def _block_points(block: Optional[dict]) -> Optional[float]:
    if not block:
        return None
    for key in ("totalPointsLive", "totalPoints"):
        if block.get(key) is not None:
            return round(float(block[key]), 2)
    return None
