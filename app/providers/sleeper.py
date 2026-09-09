"""Sleeper integration.

Sleeper's API is public and unauthenticated, which makes it the easy one.  The
valuable trick here is that ``api.sleeper.com/projections`` and ``/stats`` return
*raw stat lines*, not points.  We apply each league's own ``scoring_settings`` to
those raw stats, so a projection in a 6-pt-passing-TD TE-premium league is
genuinely that league's number rather than a borrowed PPR figure.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..config import LeagueConfig, cache_dir
from ..models import (
    DataTier, FantasyPlayerExposure, League, LineupStatus, MatchupState,
    NFLGame, Platform, PlayerGameState, ScoringSettings,
)
from ..playerids import PlayerRegistry
from ..standings import LeagueStandings, TeamRecord, apply_season_weight
from .base import HttpClient, ProviderError

log = logging.getLogger(__name__)

API = "https://api.sleeper.app/v1"
API2 = "https://api.sleeper.com"

# Stat keys that appear in stat lines but are never scoring categories.
NON_SCORING = {"gp", "gms_active", "gs", "off_snp", "def_snp", "st_snp", "tm_off_snp",
               "tm_def_snp", "tm_st_snp", "team_snp"}


def score_stats(stats: dict[str, float], scoring: dict[str, float]) -> float:
    """Apply a Sleeper scoring_settings dict to a raw stat line."""
    total = 0.0
    for key, value in (stats or {}).items():
        if key in NON_SCORING or key.startswith("pts_") or key.startswith("adp"):
            continue
        mult = scoring.get(key)
        if mult:
            total += float(value) * float(mult)
    return round(total, 2)


class SleeperProvider:
    platform = Platform.SLEEPER

    def __init__(self, registry: PlayerRegistry, season: int,
                 client: Optional[HttpClient] = None) -> None:
        self.registry = registry
        self.season = season
        self.http = client or HttpClient(cache_dir=cache_dir())
        self._proj_cache: dict[int, dict[str, dict]] = {}
        self._stat_cache: dict[int, dict[str, dict]] = {}

    # -- discovery ----------------------------------------------------------
    def user_id(self, username: str) -> str:
        data = self.http.get(f"{API}/user/{username}", cache_ttl=3600)
        if not data or not data.get("user_id"):
            raise ProviderError(f"No Sleeper user named {username!r}")
        return str(data["user_id"])

    def user_leagues(self, user_id: str, season: Optional[int] = None) -> list[dict]:
        yr = season or self.season
        return self.http.get(f"{API}/user/{user_id}/leagues/nfl/{yr}", cache_ttl=1800) or []

    def league_info(self, league_id: str) -> dict:
        return self.http.get(f"{API}/league/{league_id}", cache_ttl=1800) or {}

    def rosters(self, league_id: str) -> list[dict]:
        return self.http.get(f"{API}/league/{league_id}/rosters", cache_ttl=120) or []

    def league_users(self, league_id: str) -> list[dict]:
        return self.http.get(f"{API}/league/{league_id}/users", cache_ttl=1800) or []

    def matchups(self, league_id: str, week: int) -> list[dict]:
        return self.http.get(f"{API}/league/{league_id}/matchups/{week}", cache_ttl=0) or []

    def state(self) -> dict:
        return self.http.get(f"{API}/state/nfl", cache_ttl=600) or {}

    def my_roster_id(self, league_id: str, user_id: str) -> Optional[str]:
        for r in self.rosters(league_id):
            if str(r.get("owner_id")) == str(user_id) or user_id in (r.get("co_owners") or []):
                return str(r.get("roster_id"))
        return None

    # -- standings ----------------------------------------------------------
    def standings(self, league_id: str, my_roster_id: str, week: int) -> LeagueStandings:
        """Season records for every team, for the dynamic importance model."""
        try:
            info = self.league_info(league_id)
            rosters = self.rosters(league_id)
            users = {str(u["user_id"]): u for u in self.league_users(league_id)}
        except ProviderError as exc:
            return LeagueStandings(my_team_id=str(my_roster_id), current_week=week,
                                   error=f"Sleeper standings unavailable: {exc}")

        settings = info.get("settings") or {}
        playoff_start = int(settings.get("playoff_week_start") or 15)
        teams = []
        for r in rosters:
            st = r.get("settings") or {}
            owner = users.get(str(r.get("owner_id")), {})
            name = ((owner.get("metadata") or {}).get("team_name")
                    or owner.get("display_name") or f"Roster {r.get('roster_id')}")
            fpts = float(st.get("fpts") or 0) + float(st.get("fpts_decimal") or 0) / 100.0
            against = (float(st.get("fpts_against") or 0)
                       + float(st.get("fpts_against_decimal") or 0) / 100.0)
            teams.append(TeamRecord(
                team_id=str(r.get("roster_id")), name=name,
                wins=int(st.get("wins") or 0), losses=int(st.get("losses") or 0),
                ties=int(st.get("ties") or 0), points_for=fpts, points_against=against))

        return LeagueStandings(
            teams=teams,
            my_team_id=str(my_roster_id),
            playoff_teams=int(settings.get("playoff_teams") or max(2, len(teams) // 2)),
            regular_season_weeks=max(1, playoff_start - 1),
            current_week=week,
        )

    # -- stats / projections ------------------------------------------------
    def _fetch_weekly(self, kind: str, week: int, ttl: float) -> dict[str, dict]:
        """kind is 'projections' or 'stats'; returns sleeper_id -> raw stat dict."""
        out: dict[str, dict] = {}
        try:
            rows = self.http.get(
                f"{API2}/{kind}/nfl/{self.season}/{week}",
                params={"season_type": "regular"},
                cache_ttl=ttl,
            ) or []
        except ProviderError as exc:
            log.warning("Sleeper %s unavailable for week %s: %s", kind, week, exc)
            return out
        for row in rows:
            pid = str(row.get("player_id") or (row.get("player") or {}).get("player_id") or "")
            if pid:
                out[pid] = row.get("stats") or {}
        return out

    def projections(self, week: int) -> dict[str, dict]:
        if week not in self._proj_cache:
            self._proj_cache[week] = self._fetch_weekly("projections", week, ttl=3600)
        return self._proj_cache[week]

    def stats(self, week: int) -> dict[str, dict]:
        if week not in self._stat_cache:
            self._stat_cache[week] = self._fetch_weekly("stats", week, ttl=60)
        return self._stat_cache[week]

    # -- scoring settings ---------------------------------------------------
    @staticmethod
    def parse_scoring(info: dict) -> ScoringSettings:
        raw = {k: float(v) for k, v in (info.get("scoring_settings") or {}).items()
               if isinstance(v, (int, float))}
        return ScoringSettings(
            raw=raw,
            ppr=raw.get("rec", 0.0),
            pass_td=raw.get("pass_td", 4.0),
            te_premium=raw.get("bonus_rec_te", 0.0),
            label="Sleeper",
        )

    # -- the main event -----------------------------------------------------
    def load_matchup(
        self,
        cfg: LeagueConfig,
        week: int,
        *,
        game_index: Optional[dict[str, NFLGame]] = None,
    ) -> MatchupState:
        info = self.league_info(cfg.league_id)
        scoring = self.parse_scoring(info)
        league = League(
            id=cfg.league_id,
            name=cfg.name or info.get("name", cfg.league_id),
            platform=Platform.SLEEPER,
            buy_in_usd=cfg.buy_in_usd,
            importance_multiplier=cfg.importance_multiplier,
            season=self.season,
            scoring=scoring,
            my_team_id=cfg.my_team_id or None,
            low_priority=cfg.low_priority,
        )
        roster_positions = [str(x) for x in (info.get("roster_positions") or [])]
        state = MatchupState(league=league, week=week)

        rosters = {str(r["roster_id"]): r for r in self.rosters(cfg.league_id)}
        users = {str(u["user_id"]): u for u in self.league_users(cfg.league_id)}
        my_rid = str(cfg.my_team_id) if cfg.my_team_id else None
        if my_rid not in rosters:
            state.error = f"Roster {my_rid} not found in Sleeper league {cfg.league_id}"
            state.tier = DataTier.MINIMUM
            return state

        me = rosters[my_rid]
        owner = users.get(str(me.get("owner_id")), {})
        league.my_team_name = (owner.get("metadata", {}) or {}).get("team_name") \
            or owner.get("display_name", "My Team")

        rows = {str(m["roster_id"]): m for m in self.matchups(cfg.league_id, week)}
        my_row = rows.get(my_rid)
        if not my_row:
            state.error = _no_matchup_reason(info, league.name, week)
            state.tier = DataTier.MINIMUM
            return state

        opp_row = None
        if my_row.get("matchup_id") is not None:
            for rid, row in rows.items():
                if rid != my_rid and row.get("matchup_id") == my_row["matchup_id"]:
                    opp_row = row
                    break
        if opp_row is None:
            state.error = f"{league.name}: no opponent this week (bye or unscheduled)"
            state.opponent_name = "BYE"
        else:
            opp_owner = users.get(str(rosters.get(str(opp_row["roster_id"]), {}).get("owner_id")), {})
            state.opponent_name = (opp_owner.get("metadata", {}) or {}).get("team_name") \
                or opp_owner.get("display_name", "Opponent")

        projections = self.projections(week)
        for row, side_list, bench_list, is_mine in (
            (my_row, state.my_starters, state.my_bench, True),
            (opp_row, state.opp_starters, state.opp_bench, False),
        ):
            if not row:
                continue
            self._fill(row, league, scoring, projections, game_index or {},
                       side_list, bench_list, is_mine, roster_positions)

        state.reported_score_mine = _row_points(my_row)
        state.reported_score_opponent = _row_points(opp_row) if opp_row else 0.0
        if not projections:
            state.tier = DataTier.GOOD

        # Season context (uses already-cached responses, so no extra requests).
        standings = self.standings(cfg.league_id, my_rid, week)
        standings.punished_places = cfg.punished_places
        apply_season_weight(league, standings,
                            loser_punishment=cfg.loser_punishment,
                            dynamic=cfg.dynamic_importance)
        return state

    def _fill(self, row, league, scoring, projections, game_index,
              starters, bench, is_mine, roster_positions=()) -> None:
        from ..models import Side

        side = Side.MINE if is_mine else Side.OPPONENT
        starter_ids = [str(p) for p in (row.get("starters") or []) if p and str(p) != "0"]
        starter_pts = row.get("starters_points") or []
        all_pts = {str(k): float(v or 0) for k, v in (row.get("players_points") or {}).items()}
        slots = row.get("starters") or []

        for pid in [str(p) for p in (row.get("players") or []) if p]:
            player = self.registry.by_sleeper_id(pid)
            if player is None:
                log.debug("Unknown Sleeper player id %s in league %s", pid, league.id)
                continue
            is_starter = pid in starter_ids
            if is_starter:
                idx = starter_ids.index(pid)
                cur = float(starter_pts[idx]) if idx < len(starter_pts) and starter_pts[idx] is not None \
                    else all_pts.get(pid, 0.0)
            else:
                cur = all_pts.get(pid, 0.0)

            raw_proj = projections.get(pid) or {}
            proj = score_stats(raw_proj, scoring.raw) if raw_proj else 0.0
            game = game_index.get(player.nfl_team)
            gstate = game.state if game else PlayerGameState.BYE_OR_UNKNOWN
            frac = game.fraction_remaining if game else (1.0 if proj else 0.0)

            exp = FantasyPlayerExposure(
                canonical=player,
                league=league,
                side=side,
                lineup_status=LineupStatus.STARTER if is_starter else LineupStatus.BENCH,
                slot=_slot_label(slots, pid, roster_positions),
                current_points=round(cur, 2),
                projected_points=proj,
                game_state=gstate,
                game_fraction_remaining=frac,
                has_projection=bool(raw_proj),
            )
            (starters if is_starter else bench).append(exp)


#: Sleeper league lifecycle, used to explain an empty matchup properly.
LEAGUE_STATUS_HELP = {
    "pre_draft": "hasn't drafted yet - nothing to root for until the draft is done",
    "drafting": "is drafting right now - lineups appear once the draft finishes",
    "complete": "season is over",
}


def _no_matchup_reason(info: dict, name: str, week: int) -> str:
    status = (info or {}).get("status") or ""
    help_text = LEAGUE_STATUS_HELP.get(status)
    if help_text:
        return f"{name} {help_text}"
    return (f"No week {week} matchup posted yet in {name} "
            f"(status: {status or 'unknown'})")


def _row_points(row: Optional[dict]) -> Optional[float]:
    if not row:
        return None
    if row.get("custom_points") is not None:
        return float(row["custom_points"])
    if row.get("points") is not None:
        return round(float(row["points"]), 2)
    return None


def _slot_label(starters: list, pid: str, roster_positions: "list[str] | tuple" = ()) -> str:
    """Sleeper's `starters` array is positional: index i is the i-th roster slot,
    so `roster_positions[i]` (QB, RB, WR, FLEX, SUPER_FLEX...) names it."""
    try:
        idx = [str(p) for p in starters].index(str(pid))
    except ValueError:
        return "BN"
    positions = list(roster_positions)
    return positions[idx] if idx < len(positions) else "STARTER"
