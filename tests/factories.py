"""Synthetic league/matchup builders so the engine can be tested without network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from app.models import (
    CanonicalPlayer, FantasyPlayerExposure, League, LineupStatus, MatchupState,
    NFLGame, Platform, PlayerGameState, ScoringSettings, Side, SlotType,
)

PPR = ScoringSettings(ppr=1.0, pass_td=4.0, label="PPR")
HALF = ScoringSettings(ppr=0.5, pass_td=4.0, label="Half-PPR")
SUPERFLEX_6TD = ScoringSettings(ppr=1.0, pass_td=6.0, label="PPR 6pt")
TE_PREM = ScoringSettings(ppr=1.0, pass_td=4.0, te_premium=1.0, label="TE premium")


def player(name: str, pos: str = "RB", team: str = "PHI") -> CanonicalPlayer:
    return CanonicalPlayer(key=f"test:{name.lower().replace(' ', '')}",
                           name=name, position=pos, nfl_team=team)


def make_league(name: str, buy_in: float, *, mult: float = 1.0,
                platform: Platform = Platform.SLEEPER,
                scoring: ScoringSettings = PPR, low_priority: bool = False) -> League:
    return League(id=f"L-{name}", name=name, platform=platform, buy_in_usd=buy_in,
                  importance_multiplier=mult, season=2026, scoring=scoring,
                  my_team_id="1", my_team_name="Me", low_priority=low_priority)


def exposure(league: League, p: CanonicalPlayer, side: Side, *, proj: float = 0.0,
             cur: float = 0.0, state: PlayerGameState = PlayerGameState.NOT_STARTED,
             starter: bool = True, frac: float = 1.0) -> FantasyPlayerExposure:
    return FantasyPlayerExposure(
        canonical=p, league=league, side=side,
        lineup_status=LineupStatus.STARTER if starter else LineupStatus.BENCH,
        slot="FLEX", current_points=cur, projected_points=proj,
        game_state=state, game_fraction_remaining=frac,
    )


def make_state(
    league: League,
    *,
    my_players: Iterable[tuple] = (),
    opp_players: Iterable[tuple] = (),
    week: int = 5,
    opponent: str = "Them",
) -> MatchupState:
    """Each tuple is (CanonicalPlayer, projection, current, game_state)."""
    st = MatchupState(league=league, week=week, opponent_name=opponent)
    for p, proj, cur, gs in my_players:
        st.my_starters.append(exposure(league, p, Side.MINE, proj=proj, cur=cur, state=gs))
    for p, proj, cur, gs in opp_players:
        st.opp_starters.append(exposure(league, p, Side.OPPONENT, proj=proj, cur=cur, state=gs))
    st.reported_score_mine = sum(e.current_points for e in st.my_starters)
    st.reported_score_opponent = sum(e.current_points for e in st.opp_starters)
    return st


def filler(prefix: str, n: int, proj: float, *, cur: Optional[float] = None,
           state: PlayerGameState = PlayerGameState.NOT_STARTED,
           pos: str = "WR", team: str = "XXX") -> list[tuple]:
    """n interchangeable roster-filler players, each projected `proj`."""
    return [(player(f"{prefix}{i}", pos, team), proj,
             proj if (cur is None and state == PlayerGameState.FINAL) else (cur or 0.0),
             state) for i in range(n)]


def finished(prefix: str, n: int, points: float, **kw) -> list[tuple]:
    return filler(prefix, n, points, cur=points, state=PlayerGameState.FINAL, **kw)


def make_game(away: str = "DAL", home: str = "PHI", *, slot: SlotType = SlotType.SNF,
              minutes_out: float = 15, state: PlayerGameState = PlayerGameState.NOT_STARTED,
              broadcast: str = "NBC", week: int = 5, gid: str = "G1") -> NFLGame:
    return NFLGame(id=gid, away=away, home=home,
                   kickoff=datetime.now(timezone.utc) + timedelta(minutes=minutes_out),
                   slot=slot, broadcast=broadcast, week=week, season=2026, state=state)
