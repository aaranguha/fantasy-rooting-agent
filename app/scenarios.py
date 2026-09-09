"""Deterministic scenario engine (spec section 9).

For one NFL player we sweep every realistic performance and ask, at each level,
"what happens to me across all five leagues?"  No LLM, no randomness - the same
inputs always produce the same curve.

Cross-scoring honesty (spec section 8): the sweep axis is the player's points in
a *reference* scoring format (the average of their projections across the leagues
that own or face them).  Each league converts a point on that axis into its own
scoring using the ratio of its projection to the reference projection, so a
sweep value of 20 becomes 18.4 in a standard league and 22.6 in a TE-premium one.
Leagues are never compared on raw point totals - only on win probability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .leverage import MatchupLeverage, conditional_win_probability
from .models import CanonicalPlayer, FantasyPlayerExposure, MatchupState, Side

GRID_STEP = 0.5
GRID_MIN_MAX = 42.0      # always sweep at least 0-42 points
GRID_HEADROOM = 2.4      # ...or 2.4x the player's projection, whichever is larger


def build_grid(reference_projection: float) -> list[float]:
    top = max(GRID_MIN_MAX, round(reference_projection * GRID_HEADROOM))
    n = int(top / GRID_STEP) + 1
    return [round(i * GRID_STEP, 2) for i in range(n)]


@dataclass
class LeagueScenario:
    """One league's slice of the curve for a single player."""

    state: MatchupState
    exposure: FantasyPlayerExposure
    leverage: MatchupLeverage
    scale: float                       # reference points -> this league's points
    win_probs: list[float] = field(default_factory=list)
    baseline_win_prob: float = 0.0     # at the player's own projection

    @property
    def league(self):
        return self.state.league

    @property
    def weight(self) -> float:
        return self.league.effective_weight

    @property
    def side(self) -> Side:
        return self.exposure.side

    @property
    def swing(self) -> float:
        """Total probability this single player can move in this league."""
        if not self.win_probs:
            return 0.0
        return round(max(self.win_probs) - min(self.win_probs), 4)

    @property
    def dollar_swing(self) -> float:
        """Buy-in dollars genuinely riding on this player in this league."""
        return round(self.weight * self.swing, 2)


@dataclass
class ScenarioCurve:
    """The player's cross-league utility curve."""

    player: CanonicalPlayer
    grid: list[float]
    leagues: list[LeagueScenario]
    utility: list[float] = field(default_factory=list)
    reference_projection: float = 0.0

    # -- summary numbers ----------------------------------------------------
    @property
    def total_weight(self) -> float:
        return sum(l.weight for l in self.leagues) or 1.0

    @property
    def baseline_utility(self) -> float:
        return self.utility_at(self.reference_projection)

    def utility_at(self, reference_points: float) -> float:
        if not self.grid:
            return 0.0
        idx = min(range(len(self.grid)), key=lambda i: abs(self.grid[i] - reference_points))
        return self.utility[idx]

    @property
    def best_points(self) -> float:
        """The performance we'd pick if we could choose."""
        i = max(range(len(self.utility)), key=lambda i: self.utility[i])
        return self.grid[i]

    @property
    def worst_points(self) -> float:
        i = min(range(len(self.utility)), key=lambda i: self.utility[i])
        return self.grid[i]

    @property
    def utility_range(self) -> float:
        return round(max(self.utility) - min(self.utility), 5) if self.utility else 0.0

    def sweet_spot(self, tolerance: float = 0.02) -> Optional[tuple[float, float]]:
        """Contiguous band around the optimum that is within `tolerance` of the
        best achievable utility.  Returns None when the curve is monotonic and
        the band runs to an edge on both sides (i.e. there is no 'range')."""
        if not self.utility or self.utility_range < 1e-6:
            return None
        best = max(self.utility)
        cutoff = best - tolerance * self.utility_range
        best_i = self.utility.index(best)
        lo = best_i
        while lo > 0 and self.utility[lo - 1] >= cutoff:
            lo -= 1
        hi = best_i
        while hi < len(self.utility) - 1 and self.utility[hi + 1] >= cutoff:
            hi += 1
        return self.grid[lo], self.grid[hi]

    @property
    def is_capped(self) -> bool:
        """True when the best outcome is an interior amount of production - i.e.
        more eventually starts hurting us, but zero is not the ideal either.

        A player we purely root against peaks at zero; that is a direction, not a
        range, so it must not be reported as a 'sweet spot'."""
        if not self.utility or self.utility_range < 1e-6:
            return False
        peak = self.utility.index(max(self.utility))
        return 0 < peak < len(self.utility) - 1

    @property
    def money_at_stake(self) -> float:
        """Sum of buy-ins for leagues where this player can move things at all."""
        return round(sum(l.league.buy_in_usd for l in self.leagues if l.swing >= 0.02), 2)

    @property
    def dollar_swing(self) -> float:
        return round(sum(l.dollar_swing for l in self.leagues), 2)


def build_curve(
    player: CanonicalPlayer,
    scenarios: list[tuple[MatchupState, FantasyPlayerExposure, MatchupLeverage]],
) -> ScenarioCurve:
    """Sweep the player's performance and score every league at every level."""
    projections = [e.projected_points for _, e, _ in scenarios if e.projected_points > 0]
    reference = round(sum(projections) / len(projections), 2) if projections else 10.0

    grid = build_grid(reference)
    leagues: list[LeagueScenario] = []
    for state, exposure, lev in scenarios:
        scale = (exposure.projected_points / reference) if (reference > 0 and exposure.projected_points > 0) else 1.0
        ls = LeagueScenario(state=state, exposure=exposure, leverage=lev, scale=round(scale, 4))
        ls.win_probs = [
            conditional_win_probability(state, exposure, x * ls.scale) for x in grid
        ]
        ls.baseline_win_prob = conditional_win_probability(
            state, exposure, exposure.expected_final
        )
        leagues.append(ls)

    total_w = sum(l.weight for l in leagues) or 1.0
    utility = [
        round(sum(l.weight * l.win_probs[i] for l in leagues) / total_w, 6)
        for i in range(len(grid))
    ]
    return ScenarioCurve(
        player=player, grid=grid, leagues=leagues,
        utility=utility, reference_projection=reference,
    )
