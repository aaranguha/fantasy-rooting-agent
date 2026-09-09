"""'How many points do we need?' / 'How many can we afford?' (spec sections 5-7).

Both questions reduce to the same algebra: hold every other starter at its best
estimate, then solve for the target player's FINAL score at which the projected
matchup result flips.

Crucially, the *confidence* of that answer is graded honestly.  On Monday night
with one player left it is exact arithmetic.  On Thursday it is a projection and
we say so.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .models import (
    Confidence, FantasyPlayerExposure, MatchupState, PlayerGameState, Side,
)

WIN_EPSILON = 0.01  # ties: most leagues award a tie, so "flip point" + a hair


@dataclass
class Threshold:
    """The flip point for one player in one league."""

    exposure: FantasyPlayerExposure
    state: MatchupState
    value: float                # target's FINAL score at the flip point
    confidence: Confidence
    unresolved_other: float     # expected points still unknown, excluding target
    feasible: bool = True       # False when the flip point is unreachable
    impossible_reason: str = ""

    @property
    def side(self) -> Side:
        return self.exposure.side

    @property
    def is_need(self) -> bool:
        return self.side == Side.MINE

    @property
    def already_scored(self) -> float:
        return self.exposure.current_points

    @property
    def remaining_required(self) -> float:
        """Additional points still needed/affordable from here."""
        return round(self.value - self.already_scored, 2)

    @property
    def league_label(self) -> str:
        return self.state.league.label

    @property
    def is_exact(self) -> bool:
        return self.confidence == Confidence.EXACT

    @property
    def qualifier(self) -> str:
        return {
            Confidence.EXACT: "exactly",
            Confidence.HIGH: "about",
            Confidence.PROJECTION: "roughly",
            Confidence.LOW: "very roughly",
        }[self.confidence]

    def describe(self) -> str:
        """One conversational sentence."""
        league = self.state.league.label
        fmt = self.state.league.scoring.format_name
        if self.is_need:
            if not self.feasible:
                return (f"{league}: {self.impossible_reason}")
            if self.confidence == Confidence.EXACT:
                return (f"{league}: need {self.value:.2f}+ {fmt} points from "
                        f"{self.exposure.canonical.short_name} to win.")
            return (f"{league}: need {self.qualifier} {self.value:.0f} "
                    f"({fmt}) - {self.confidence.value.lower()}.")
        if not self.feasible:
            return f"{league}: {self.impossible_reason}"
        if self.confidence == Confidence.EXACT:
            return (f"{league}: {self.exposure.canonical.short_name} must stay under "
                    f"{self.value:.2f} {fmt} points.")
        return (f"{league}: can afford {self.qualifier} {self.value:.0f} "
                f"({fmt}) - {self.confidence.value.lower()}.")


def grade_confidence(state: MatchupState, target: FantasyPlayerExposure) -> tuple[Confidence, float]:
    """How much of the matchup, other than the target, is still unknown?"""
    unresolved_other = sum(
        p.unresolved_points for p in state.all_starters()
        if p is not target and not (p.canonical.key == target.canonical.key
                                    and p.side == target.side)
    )
    total_expected = max(1.0, state.projected_final_mine + state.projected_final_opponent)
    share = unresolved_other / total_expected

    if unresolved_other <= 0.5:
        return Confidence.EXACT, unresolved_other
    if share <= 0.10:
        return Confidence.HIGH, unresolved_other
    if share <= 0.45:
        return Confidence.PROJECTION, unresolved_other
    return Confidence.LOW, unresolved_other


def compute_threshold(state: MatchupState, target: FantasyPlayerExposure) -> Threshold:
    """Solve for the target's final score at which the matchup result flips."""
    confidence, unresolved_other = grade_confidence(state, target)

    mine = state.projected_final_mine
    opp = state.projected_final_opponent

    if target.side == Side.MINE:
        # Everything of ours except the target.
        mine_without = mine - target.expected_final
        value = opp - mine_without + WIN_EPSILON
        feasible, reason = True, ""
        if value <= 0:
            feasible = False
            reason = (f"we're projected to win {state.league.label} even if "
                      f"{target.canonical.short_name} is shut out.")
            value = 0.0
        elif value > 60:
            feasible = False
            reason = (f"{target.canonical.short_name} alone can't save "
                      f"{state.league.label} - we'd need about {value:.0f}.")
    else:
        opp_without = opp - target.expected_final
        value = mine - opp_without - WIN_EPSILON
        feasible, reason = True, ""
        if value <= 0:
            feasible = False
            reason = (f"we're already projected behind in {state.league.label} even if "
                      f"{target.canonical.short_name} is shut out - we need help elsewhere.")
            value = 0.0
        elif value > 60:
            feasible = False
            reason = (f"we can comfortably survive any normal "
                      f"{target.canonical.short_name} game in {state.league.label}.")

    return Threshold(
        exposure=target,
        state=state,
        value=round(max(0.0, value), 2),
        confidence=confidence,
        unresolved_other=round(unresolved_other, 2),
        feasible=feasible,
        impossible_reason=reason,
    )
