"""Orchestration: load every league, then answer 'who do I root for in this game?'

This module owns the graceful-degradation policy (spec section 25): a single
league failing to load, or projections being unavailable, never kills the run.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .config import AppConfig, LeagueConfig, espn_cookies
from .leverage import MatchupLeverage, compute_leverage
from .models import (
    CanonicalPlayer, DataTier, FantasyPlayerExposure, League, MatchupState,
    NFLGame, Platform, RootingCategory, Side, SlotType,
)
from .playerids import PlayerRegistry, build_registry
from .providers.base import AuthError, ProviderError
from .providers.espn import ESPNProvider
from .providers.nfl import NFLScheduleProvider, primetime_games, team_game_index
from .providers.sleeper import SleeperProvider
from .rooting import PlayerRooting, analyze_player, mark_public_enemy, rank
from .scenarios import build_curve

log = logging.getLogger(__name__)


@dataclass
class GameGuide:
    """Everything worth knowing about one NFL game, across all my leagues."""

    game: NFLGame
    players: list[PlayerRooting] = field(default_factory=list)
    leverages: list[MatchupLeverage] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def root_for(self) -> list[PlayerRooting]:
        return [p for p in self.players if p.category.is_positive and not p.is_conflicted]

    @property
    def root_against(self) -> list[PlayerRooting]:
        return [p for p in self.players if p.category.is_negative and not p.is_conflicted]

    @property
    def conflicted(self) -> list[PlayerRooting]:
        return [p for p in self.players if p.is_conflicted and p.matters]

    @property
    def relevant(self) -> list[PlayerRooting]:
        return [p for p in self.players if p.matters]

    @property
    def foreground_relevant(self) -> list[PlayerRooting]:
        """Relevant players who matter in at least one NORMAL-priority league.

        A low-priority league (spec: "mention it, don't make it a priority") never
        by itself earns a player a full block in the push - it only demotes him
        to `background_relevant` unless a real league also has him.
        """
        return [p for p in self.relevant
               if any(not l.league.low_priority for l in p.lines)]

    @property
    def background_relevant(self) -> list[PlayerRooting]:
        """Relevant players who ONLY matter because of a low-priority league."""
        return [p for p in self.relevant
               if p.lines and all(l.league.low_priority for l in p.lines)]

    @property
    def money_at_stake(self) -> float:
        """Buy-ins of every league with a starter in this game."""
        leagues = {}
        for p in self.players:
            for line in p.lines:
                leagues[line.league.id] = line.league.buy_in_usd
        return round(sum(leagues.values()), 2)

    @property
    def live_dollars(self) -> float:
        """Dollars actually swingable by this game's players."""
        return round(sum(l.leverage for l in self.leverages), 2)

    @property
    def affected_leagues(self) -> list[MatchupLeverage]:
        return sorted(self.leverages, key=lambda l: -l.league_weight)

    @property
    def biggest_swing(self) -> Optional[PlayerRooting]:
        rel = [p for p in self.players if p.dollar_swing > 0]
        return max(rel, key=lambda p: p.dollar_swing) if rel else None

    @property
    def top_league(self) -> Optional[MatchupLeverage]:
        return max(self.leverages, key=lambda l: l.leverage) if self.leverages else None


@dataclass
class WeekContext:
    """Loaded once, reused for every game in the week."""

    season: int
    week: int
    states: list[MatchupState]
    games: list[NFLGame]
    registry: PlayerRegistry
    errors: list[str] = field(default_factory=list)

    @property
    def ok_states(self) -> list[MatchupState]:
        return [s for s in self.states if not s.error or s.my_starters]

    @property
    def tier(self) -> DataTier:
        if not self.ok_states:
            return DataTier.MINIMUM
        # Report the WORST tier present - the run is only as good as its weakest league.
        return max((s.tier for s in self.ok_states),
                   key=lambda t: list(DataTier).index(t), default=DataTier.BEST)


class Analyzer:
    def __init__(self, cfg: AppConfig, *, registry: Optional[PlayerRegistry] = None,
                 nfl: Optional[NFLScheduleProvider] = None) -> None:
        self.cfg = cfg
        self.nfl = nfl or NFLScheduleProvider()
        self._registry = registry
        self.season = cfg.season or 0

    # -- setup --------------------------------------------------------------
    def resolve_week(self, week: Optional[int] = None) -> tuple[int, int, int]:
        season, cur_week, stype = self.nfl.current_week()
        season = self.cfg.season or season
        return season, (week or cur_week), stype

    def registry(self, season: int) -> PlayerRegistry:
        if self._registry is None:
            self._registry = build_registry(season)
        return self._registry

    # -- loading ------------------------------------------------------------
    def load_week(self, week: Optional[int] = None, *, refresh: bool = True) -> WeekContext:
        """Fetch every league's live matchup state, in parallel."""
        season, wk, stype = self.resolve_week(week)
        reg = self.registry(season)
        games = self.nfl.games(season, wk, stype, cache_ttl=0 if refresh else 120)
        gindex = team_game_index(games)

        sleeper = SleeperProvider(reg, season)
        espn = ESPNProvider(reg, season, cookies=espn_cookies())
        errors: list[str] = []

        def load(lc: LeagueConfig) -> MatchupState:
            provider = sleeper if lc.platform == Platform.SLEEPER.value else espn
            try:
                return provider.load_matchup(lc, wk, game_index=gindex)
            except AuthError as exc:
                log.error("Auth failure for %s league %s: %s", lc.platform, lc.name, exc)
                return _failed_state(lc, wk, f"AUTH: {exc}")
            except ProviderError as exc:
                log.error("Load failure for %s league %s: %s", lc.platform, lc.name, exc)
                return _failed_state(lc, wk, str(exc))
            except Exception as exc:  # noqa: BLE001 - never let one league kill the run
                log.exception("Unexpected failure loading %s", lc.name)
                return _failed_state(lc, wk, f"{type(exc).__name__}: {exc}")

        states: list[MatchupState] = []
        if self.cfg.leagues:
            with ThreadPoolExecutor(max_workers=min(8, len(self.cfg.leagues))) as pool:
                states = list(pool.map(load, self.cfg.leagues))
        for s in states:
            if s.error:
                errors.append(f"{s.league.name}: {s.error}")
        return WeekContext(season=season, week=wk, states=states, games=games,
                           registry=reg, errors=errors)

    # -- analysis -----------------------------------------------------------
    def analyze_game(self, ctx: WeekContext, game: NFLGame) -> GameGuide:
        """Build the full rooting guide for a single NFL game."""
        teams = game.teams
        levs: dict[str, MatchupLeverage] = {}
        by_player: dict[str, list[tuple[MatchupState, FantasyPlayerExposure, MatchupLeverage]]] = {}

        for state in ctx.ok_states:
            lev = compute_leverage(state)
            touched = False
            for exp in state.all_starters():      # starters only (spec section 15)
                if exp.canonical.nfl_team not in teams:
                    continue
                by_player.setdefault(exp.canonical.key, []).append((state, exp, lev))
                touched = True
            if touched:
                levs[state.league.id] = lev

        players: list[PlayerRooting] = []
        for key, scenarios in by_player.items():
            curve = build_curve(scenarios[0][1].canonical, scenarios)
            players.append(analyze_player(curve))

        players = rank(players)
        mark_public_enemy(players)
        return GameGuide(game=game, players=players, leverages=list(levs.values()))

    def analyze_player(self, ctx: WeekContext, player: CanonicalPlayer) -> Optional[PlayerRooting]:
        """Cross-league analysis for one named player, regardless of game."""
        scenarios = []
        for state in ctx.ok_states:
            lev = compute_leverage(state)
            for exp in state.all_starters():
                if exp.canonical.key == player.key:
                    scenarios.append((state, exp, lev))
        if not scenarios:
            return None
        return analyze_player(build_curve(player, scenarios))

    # -- exposure ------------------------------------------------------------
    def exposure_table(self, ctx: WeekContext) -> list[dict]:
        """Every started player across every league, with weighted rooting value."""
        rows: dict[str, dict] = {}
        for state in ctx.ok_states:
            lev = compute_leverage(state)
            for exp in state.all_starters():
                r = rows.setdefault(exp.canonical.key, {
                    "player": exp.canonical, "ours": [], "against": [],
                    "weighted": 0.0, "live": 0.0,
                })
                bucket = "ours" if exp.side == Side.MINE else "against"
                r[bucket].append(state.league)
                sign = 1 if exp.side == Side.MINE else -1
                r["weighted"] += sign * state.league.effective_weight
                r["live"] += sign * lev.leverage
        out = list(rows.values())
        out.sort(key=lambda r: -abs(r["live"]))
        return out

    # -- selection -----------------------------------------------------------
    def primetime(self, ctx: WeekContext) -> list[NFLGame]:
        return primetime_games(ctx.games, self.cfg.include_slots)

    def find_game(self, ctx: WeekContext, query: str) -> Optional[NFLGame]:
        """Match 'BUF MIA', 'buf@mia', 'MNF' or a team abbreviation."""
        q = query.upper().replace("@", " ").replace("VS", " ").replace("-", " ").split()
        if len(q) == 1 and q[0] in {s.value for s in SlotType}:
            hits = [g for g in ctx.games if g.slot.value == q[0]]
            return hits[0] if hits else None
        for g in ctx.games:
            if set(q) & g.teams and (len(q) == 1 or set(q) <= g.teams):
                return g
        return None


def _failed_state(lc: LeagueConfig, week: int, error: str) -> MatchupState:
    league = lc.to_league()
    return MatchupState(league=league, week=week, error=error, tier=DataTier.MINIMUM)
