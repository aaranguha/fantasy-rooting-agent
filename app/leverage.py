"""Matchup leverage: how much is still genuinely in doubt, and how much a single
player can move it.

Money is not the only factor (spec section 3).  A $100 league that is a 45-point
blowout has almost no leverage; a $50 league projected to finish within 2 points
has enormous leverage.  Leverage here is a real probability quantity, not a vibe.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .models import FantasyPlayerExposure, MatchupState, PlayerGameState

# Per-position coefficient of variation on the *remaining* projected points.
# QBs are the most predictable weekly scorers; defenses the least.
POSITION_CV = {
    "QB": 0.42, "RB": 0.58, "WR": 0.66, "TE": 0.70, "K": 0.60, "DST": 0.90,
}
DEFAULT_CV = 0.65
MIN_PLAYER_SD = 1.0   # nobody is truly deterministic until their game is final
MIN_TOTAL_SD = 1.5    # keeps win probabilities off the 0/1 rails


def player_sd(exposure: FantasyPlayerExposure) -> float:
    """Standard deviation of a player's *remaining* production."""
    remaining = exposure.unresolved_points
    if remaining <= 0 or exposure.game_state == PlayerGameState.FINAL:
        return 0.0
    cv = POSITION_CV.get(exposure.canonical.position, DEFAULT_CV)
    return max(MIN_PLAYER_SD, cv * remaining)


def matchup_sd(state: MatchupState, exclude_key: Optional[str] = None) -> float:
    """Total SD of the projected margin, optionally holding one player fixed."""
    var = 0.0
    for p in state.all_starters():
        if exclude_key and p.canonical.key == exclude_key:
            continue
        var += player_sd(p) ** 2
    return max(MIN_TOTAL_SD, math.sqrt(var))


def normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def win_probability(margin: float, sd: float) -> float:
    """P(my final > opponent final) given a projected margin and its SD."""
    return min(0.999, max(0.001, normal_cdf(margin / max(sd, 1e-6))))


@dataclass
class MatchupLeverage:
    """How much this league's outcome is still up for grabs."""

    state: MatchupState
    win_prob: float
    sd: float
    margin: float
    unresolved: float

    @property
    def league_weight(self) -> float:
        return self.state.league.effective_weight

    @property
    def closeness(self) -> float:
        """0 = decided, 1 = coin flip.  Peaks at a 50% win probability."""
        return round(1.0 - abs(self.win_prob - 0.5) * 2.0, 4)

    @property
    def leverage(self) -> float:
        """Dollars of *live* value: weight scaled by how undecided the matchup is."""
        return round(self.league_weight * self.closeness, 2)

    @property
    def is_close(self) -> bool:
        return 0.25 <= self.win_prob <= 0.75

    @property
    def is_blowout(self) -> bool:
        return self.win_prob >= 0.93 or self.win_prob <= 0.07

    @property
    def descriptor(self) -> str:
        if self.win_prob >= 0.93:
            return "basically locked up"
        if self.win_prob >= 0.75:
            return "comfortably ahead"
        if self.win_prob >= 0.60:
            return "favored"
        if self.win_prob > 0.40:
            return "a coin flip"
        if self.win_prob > 0.25:
            return "an underdog"
        if self.win_prob > 0.07:
            return "in trouble"
        return "basically lost"


def compute_leverage(state: MatchupState) -> MatchupLeverage:
    sd = matchup_sd(state)
    margin = state.projected_margin
    return MatchupLeverage(
        state=state,
        win_prob=win_probability(margin, sd),
        sd=sd,
        margin=round(margin, 2),
        unresolved=round(state.unresolved_points_total, 2),
    )


def conditional_win_probability(
    state: MatchupState, exposure: FantasyPlayerExposure, player_final: float
) -> float:
    """My win probability in this league if `exposure` finishes with exactly
    `player_final` points.  The target's own variance is removed, because we are
    conditioning on it rather than projecting it."""
    key = exposure.canonical.key
    sd = matchup_sd(state, exclude_key=key)

    mine = state.projected_final_mine
    opp = state.projected_final_opponent
    if exposure.side.value == "mine":
        mine = mine - exposure.expected_final + player_final
    else:
        opp = opp - exposure.expected_final + player_final
    return win_probability(mine - opp, sd)
