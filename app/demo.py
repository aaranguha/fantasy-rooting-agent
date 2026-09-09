"""Realistic synthetic leagues, so the rooting engine can be verified before any
credentials exist.

`fantasy-agent demo` builds five leagues (2 ESPN + 3 Sleeper, $100/$50/$35/$20/$10)
around a REAL upcoming primetime game with REAL players from that game, then runs
the genuine analysis pipeline end to end.  Nothing here is used by the live path.
"""

from __future__ import annotations

import random
from typing import Optional

from .analysis import Analyzer, GameGuide
from .config import AppConfig
from .models import (
    CanonicalPlayer, FantasyPlayerExposure, League, LineupStatus, MatchupState,
    NFLGame, Platform, PlayerGameState, ScoringSettings, Side,
)
from .standings import LeagueStandings, TeamRecord, apply_season_weight
from .playerids import PlayerRegistry

# (name, platform, buy-in, manual multiplier, scoring, my record, loser punishment)
DEMO_LEAGUES = [
    ("Dynasty Money", Platform.ESPN, 100.0, 1.0,
     ScoringSettings(ppr=1.0, pass_td=4, label="PPR"), (8, 2), 0.0),
    ("Work League", Platform.SLEEPER, 50.0, 1.5,
     ScoringSettings(ppr=0.5, pass_td=6, label="Half"), (6, 4), 0.0),
    ("College Buddies", Platform.SLEEPER, 35.0, 1.0,
     ScoringSettings(ppr=1.0, pass_td=4, label="PPR"), (2, 8), 0.0),
    # The punishment league: eliminated, and sliding toward the bottom.
    ("Family League", Platform.ESPN, 20.0, 0.75,
     ScoringSettings(ppr=0.0, pass_td=4, label="Std"), (1, 9), 1.0),
    ("Reddit Free Roll", Platform.SLEEPER, 10.0, 1.0,
     ScoringSettings(ppr=1.0, pass_td=4, te_premium=1.0, label="TEP"), (5, 5), 0.0),
]
DEMO_REG_SEASON_WEEKS = 14
DEMO_STANDINGS_WEEK = 12       # late enough that the punishment ramp has teeth

STARTER_COUNT = 9


class DemoContext:
    """Duck-typed WeekContext for the demo path."""

    def __init__(self, states, games, season, week, registry):
        self.states = self.ok_states = states
        self.games = games
        self.season, self.week = season, week
        self.registry = registry
        self.errors = []

    @property
    def tier(self):
        from .models import DataTier
        return DataTier.BEST


def _demo_standings(my_record: tuple[int, int]) -> LeagueStandings:
    """A ten-team league where only my record distinguishes me."""
    wins, losses = my_record
    games = wins + losses
    my_ppg = 118.0 + (wins - losses) * 3.0
    teams = [TeamRecord(team_id="1", name="My Squad", wins=wins, losses=losses,
                        points_for=my_ppg * games)]
    for i in range(2, 11):
        w = games // 2 + (1 if i % 2 else 0)
        teams.append(TeamRecord(team_id=str(i), name=f"Team {i}", wins=w,
                                losses=games - w, points_for=116.0 * games))
    return LeagueStandings(teams=teams, my_team_id="1", playoff_teams=6,
                           regular_season_weeks=DEMO_REG_SEASON_WEEKS,
                           current_week=DEMO_STANDINGS_WEEK)


def _pool(registry: PlayerRegistry, teams: set[str]) -> list[CanonicalPlayer]:
    """A believable skill-position spread from this game, not fourteen backup QBs."""
    wanted = ["QB", "RB", "WR", "WR", "TE", "RB", "WR"]
    by_pos: dict[str, list[CanonicalPlayer]] = {}
    for p in registry.players.values():
        if p.nfl_team in teams and p.position in ("QB", "RB", "WR", "TE"):
            by_pos.setdefault(p.position, []).append(p)
    for group in by_pos.values():
        group.sort(key=lambda p: p.name)
    out, cursors = [], {k: 0 for k in by_pos}
    for pos in wanted:
        group = by_pos.get(pos) or []
        i = cursors.get(pos, 0)
        if i < len(group):
            out.append(group[i])
            cursors[pos] = i + 1
    return out


def _elsewhere(registry: PlayerRegistry, teams: set[str], n: int,
               rng: random.Random) -> list[CanonicalPlayer]:
    others = [p for p in registry.players.values()
              if p.nfl_team and p.nfl_team not in teams
              and p.position in ("QB", "RB", "WR", "TE")]
    return rng.sample(others, min(n, len(others)))


def build_states(registry: PlayerRegistry, game: NFLGame, week: int,
                 seed: int = 7) -> list[MatchupState]:
    """Five leagues with deliberately interesting, overlapping exposures."""
    rng = random.Random(seed)
    stars = _pool(registry, game.teams)[:14] or []
    if len(stars) < 6:
        return []

    # Exposure plan: index into `stars`, (leagues where he's ours, where we face him)
    plan = [
        (0, [0, 2], []),        # ours in the $100 and $35 leagues
        (1, [], [0, 3]),        # against us in the $100 and $20 leagues
        (2, [1], [3]),          # THE conflicted one: ours $50, against us $20
        (3, [4], [1]),          # ours in the $10, against us in the $50
        (4, [], [2]),           # against us in the $35
    ]

    states: list[MatchupState] = []
    for li, (name, platform, buy_in, mult, scoring, record, punishment) in enumerate(DEMO_LEAGUES):
        league = League(id=f"demo-{li}", name=name, platform=platform, buy_in_usd=buy_in,
                        importance_multiplier=mult, season=game.season, scoring=scoring,
                        my_team_id="1", my_team_name="My Squad")
        apply_season_weight(league, _demo_standings(record),
                            loser_punishment=punishment, dynamic=True)
        st = MatchupState(league=league, week=week, opponent_name=f"Opponent {li + 1}")

        mine_special = [stars[i] for i, own, _ in plan if li in own]
        opp_special = [stars[i] for i, _, face in plan if li in face]

        # Deliberately varied matchup states: a nail-biter, a blowout, and middles.
        margin = [0.0, -3.0, 6.0, 40.0, -22.0][li]
        base = 12.0 + li * 0.4

        for side, specials, bucket in ((Side.MINE, mine_special, st.my_starters),
                                       (Side.OPPONENT, opp_special, st.opp_starters)):
            for p in specials:
                proj = round(rng.uniform(13.0, 21.0) * (0.75 if scoring.ppr == 0 else 1.0), 1)
                bucket.append(FantasyPlayerExposure(
                    canonical=p, league=league, side=side, lineup_status=LineupStatus.STARTER,
                    slot=p.position, current_points=0.0, projected_points=proj,
                    game_state=game.state, game_fraction_remaining=game.fraction_remaining))
            fillers = _elsewhere(registry, game.teams, STARTER_COUNT - len(specials), rng)
            bump = (margin / max(1, len(fillers))) if side is Side.MINE else 0.0
            for p in fillers:
                pts = round(max(2.0, base + bump + rng.uniform(-2.5, 2.5)), 1)
                bucket.append(FantasyPlayerExposure(
                    canonical=p, league=league, side=side, lineup_status=LineupStatus.STARTER,
                    slot=p.position, current_points=pts, projected_points=pts,
                    game_state=PlayerGameState.FINAL, game_fraction_remaining=0.0))
        states.append(st)
    return states


def build_demo(cfg: AppConfig, *, week: Optional[int] = None,
               game_query: Optional[str] = None) -> tuple[DemoContext, list[NFLGame]]:
    an = Analyzer(cfg)
    season, wk, stype = an.resolve_week(week)
    games = an.nfl.games(season, wk, stype)
    registry = an.registry(season)

    from .providers.nfl import primetime_games
    pt = primetime_games(games, cfg.include_slots) or games
    target = an.find_game(type("C", (), {"games": games})(), game_query) if game_query else None
    picks = [target] if target else pt

    states = build_states(registry, picks[0], wk) if picks else []
    ctx = DemoContext(states, games, season, wk, registry)
    return ctx, picks
