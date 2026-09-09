"""Turning the utility curve into a rooting verdict, in plain English.

The category is derived from how much the weighted cross-league win-probability
actually moves between a bad game and a big game for this player - never from a
naive count of how many leagues own him (spec section 11).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .leverage import MatchupLeverage
from .models import CanonicalPlayer, Confidence, RootingCategory, Side
from .scenarios import ScenarioCurve
from .thresholds import Threshold, compute_threshold

# Score bands (score is a signed 0-100 measure of weighted win-prob movement).
HARD_FOR, FOR, SLIGHT_FOR = 30.0, 12.0, 4.0
LOW_PERF, HIGH_PERF = 0.30, 1.70   # multiples of the player's projection

# Dynamic emoji spectrum for a player rostered on both sides at once (spec:
# "give more of a proper story per player" - and make it dynamic, not just a
# league count).  The ratio here is WEIGHTED leverage - each side's summed
# `dollar_swing` (league weight x how much this player can actually move that
# league's win probability) - not a raw count of leagues.  That's the whole
# point: a league we're up 60 points in contributes ~$0 of real leverage no
# matter how big its buy-in is, because his stat line there can't change
# anything.  So "owned in 1, facing in 2" can still read as pure upside (🚀) if
# both leagues we face him in are blowouts we can absorb - and, going the other
# way, "owned in 2, facing in 1" can read as heavily against (🟥) if the one we
# own him in is already decided and the one we face him in is a nail-biter.
_NO_LEVERAGE = 1.0   # dollars; below this, a league's swing is noise, not a story

_CONFLICT_SPECTRUM = (
    (0.60, "\U0001f7e2"),   # 🟢 comfortably ours despite the conflict
    (0.40, "\U0001f7e1"),   # 🟡 a genuine toss-up
    (0.20, "\U0001f7e7"),   # 🟧 leans against
    (0.00, "\U0001f7e5"),   # 🟥 heavily against, though we own him somewhere
)


def conflict_emoji(owned_weight: float, faced_weight: float) -> str:
    """Color for a player who is rostered on both sides at once.

    Both weights must be positive - a player with zero real leverage on one
    side isn't a live conflict on that side and shouldn't reach this function
    (the caller resolves that case as a rocket before calling in).
    """
    ratio = owned_weight / (owned_weight + faced_weight)
    for cutoff, emoji in _CONFLICT_SPECTRUM:
        if ratio >= cutoff:
            return emoji
    return _CONFLICT_SPECTRUM[-1][1]  # pragma: no cover - ratio is always >= 0


@dataclass
class LeagueLine:
    """One league's contribution to a player's rooting case."""

    threshold: Threshold
    leverage: MatchupLeverage
    swing: float
    dollar_swing: float
    scale: float

    @property
    def league(self):
        return self.threshold.state.league

    @property
    def side(self) -> Side:
        return self.threshold.side

    @property
    def mine(self) -> bool:
        return self.side == Side.MINE

    def describe(self) -> str:
        t = self.threshold
        tag = "OURS" if self.mine else "AGAINST"
        head = f"{self.league.label} - {tag}"
        if not t.feasible:
            body = t.impossible_reason
        elif self.mine:
            body = (f"need {t.qualifier} {t.value:.1f} "
                    f"({t.remaining_required:+.1f} more)")
        else:
            body = (f"can afford {t.qualifier} {t.value:.1f} "
                    f"({t.remaining_required:+.1f} more)")
        return (f"{head}: {body} | win prob {self.leverage.win_prob:.0%} "
                f"({self.leverage.descriptor}), swing {self.swing:.0%}, "
                f"${self.dollar_swing:.0f} live")


@dataclass
class PlayerRooting:
    player: CanonicalPlayer
    curve: ScenarioCurve
    lines: list[LeagueLine] = field(default_factory=list)
    score: float = 0.0
    category: RootingCategory = RootingCategory.NEUTRAL
    sweet_spot: Optional[tuple[float, float]] = None
    public_enemy: bool = False

    # -- shape --------------------------------------------------------------
    @property
    def owned_lines(self) -> list[LeagueLine]:
        return [l for l in self.lines if l.mine]

    @property
    def faced_lines(self) -> list[LeagueLine]:
        return [l for l in self.lines if not l.mine]

    @property
    def is_conflicted(self) -> bool:
        return bool(self.owned_lines) and bool(self.faced_lines)

    @property
    def money_at_stake(self) -> float:
        return self.curve.money_at_stake

    @property
    def dollar_swing(self) -> float:
        return self.curve.dollar_swing

    @property
    def best_confidence(self) -> Confidence:
        order = [Confidence.EXACT, Confidence.HIGH, Confidence.PROJECTION, Confidence.LOW]
        found = [l.threshold.confidence for l in self.lines]
        for c in order:
            if c in found:
                return c
        return Confidence.LOW

    @property
    def emoji(self) -> str:
        """The one glyph that has to tell the whole story at a glance.

        Priority: the game's single worst offender always gets the skull.
        A player owned somewhere and faced nowhere has no downside at all -
        🚀, no further nuance needed.

        A player rostered on both sides is judged by LEVERAGE, not a raw
        league count: how much real win-probability swing (weighted by league
        stakes) is riding on him where we own him, versus where we face him.
        If the leagues we face him in are blowouts he can't flip, that side
        contributes ~$0 regardless of how many of them there are - so he still
        reads 🚀, because there's genuinely nothing to fear. Flip it around and
        the same logic applies: owning him in a league we've already locked up,
        while facing him in one close game, reads as heavily against (🟥)
        despite technically being "ours" somewhere too.
        """
        if self.public_enemy:
            return "☠️"
        if self.owned_lines and not self.faced_lines:
            return "\U0001f680"  # 🚀 pure upside, structurally
        if not self.owned_lines:
            return self.category.emoji  # pure against: plain root-against red

        faced_weight = sum(l.dollar_swing for l in self.faced_lines)
        if faced_weight < _NO_LEVERAGE:
            # Technically faced somewhere, but nothing real is riding on it
            # there (a blowout he can't flip) - no real downside to rooting hard.
            return "\U0001f680"
        owned_weight = sum(l.dollar_swing for l in self.owned_lines)
        return conflict_emoji(owned_weight, faced_weight)

    @property
    def dominant_line(self) -> Optional[LeagueLine]:
        """The league where this player moves the most real money."""
        return max(self.lines, key=lambda l: l.dollar_swing) if self.lines else None

    @property
    def mixed_scoring(self) -> bool:
        """True when the affected leagues don't score alike, so a raw point total
        is not directly transferable between them (spec section 8)."""
        settings = [l.league.scoring for l in self.lines]
        return any(not a.comparable_to(b) for a in settings for b in settings)

    @property
    def matters(self) -> bool:
        return abs(self.score) >= SLIGHT_FOR or (self.is_conflicted and self.dollar_swing >= 5)

    # -- language -----------------------------------------------------------
    @property
    def verdict(self) -> str:
        if self.public_enemy:
            return "PUBLIC ENEMY #1 - ROOT AGAINST"
        return self.category.value

    def range_phrase(self) -> str:
        """Human wording for the sweet spot, or "" when there isn't a real one.

        The band is computed on the reference scale, but quoted in the points of
        the league that matters most - and named, when the leagues score
        differently enough that the numbers aren't interchangeable.
        """
        if not self.sweet_spot:
            return ""
        lo, hi = self.sweet_spot
        top = self.curve.grid[-1]
        open_bottom, open_top = lo <= 0.01, hi >= top - 0.01
        if open_bottom and open_top:
            return ""                      # every outcome is equally fine

        dom = self.dominant_line
        scale = (dom.scale if dom and dom.scale else 1.0)
        lo, hi = lo * scale, hi * scale
        where = f" in the {dom.league.label}" if (self.mixed_scoring and dom) else ""

        if open_top:
            return f"{lo:.0f}+ points{where}"
        if open_bottom:
            # "under 0" is nonsense; below ~1 point this is just "root against".
            return f"under {hi:.0f} points{where}" if hi >= 1.0 else ""
        if hi - lo < 0.5:
            return f"right around {lo:.0f} points{where}"
        return f"{lo:.0f}-{hi:.0f} points{where}"

    def headline(self) -> str:
        name = self.player.name.upper()
        base = f"{self.emoji} {name}: {self.verdict}"
        rng = self.range_phrase()
        return f"{base} (sweet spot {rng})" if rng and self.is_conflicted else base

    def narrative(self) -> str:
        """The 'knowledgeable friend' summary (spec section 29)."""
        name = self.player.short_name
        owned, faced = self.owned_lines, self.faced_lines
        money = f"${self.money_at_stake:.0f}"

        if not self.matters:
            if self.is_conflicted:
                return (f"{name} genuinely cancels out - the leagues where we own him and "
                        f"face him are worth about the same and neither matchup is close.")
            return f"{name} barely moves the needle for us tonight."

        if self.is_conflicted:
            best_own = max(owned, key=lambda l: l.dollar_swing) if owned else None
            best_face = max(faced, key=lambda l: l.dollar_swing) if faced else None
            bits = []
            if best_own and best_own.threshold.feasible:
                bits.append(f"we need {best_own.threshold.qualifier} "
                            f"{best_own.threshold.value:.0f} from him in the "
                            f"{best_own.league.label}")
            if best_face and best_face.threshold.feasible:
                bits.append(f"we can survive {best_face.threshold.qualifier} "
                            f"{best_face.threshold.value:.0f} in the {best_face.league.label}")
            joined = ", and ".join(bits) if bits else "the two sides mostly offset"
            rng = self.range_phrase()
            tail = f" Sweet spot is {rng}." if rng else ""
            if self.category.is_positive:
                return (f"Weird one with {name}: {joined}. The league we own him in matters "
                        f"more, so we're rooting for him.{tail}")
            if self.category.is_negative:
                return (f"{name} is technically ours somewhere, but {joined} - the matchup "
                        f"we're facing him in is worth more. Keep him quiet.{tail}")
            return f"{name} is a genuine coin flip: {joined}.{tail}"

        if self.category.is_positive:
            lead = max(owned, key=lambda l: l.dollar_swing)
            t = lead.threshold
            if not t.feasible:
                return (f"{name} is ours in {len(owned)} league(s) worth {money}, but "
                        f"{t.impossible_reason}")
            if t.confidence == Confidence.EXACT:
                return (f"{name} needs exactly {t.value:.2f} to win the {lead.league.label} - "
                        f"{t.remaining_required:+.2f} from here. Nothing else is unresolved.")
            return (f"{name} is the guy we want tonight - {money} of leagues care, and we need "
                    f"{t.qualifier} {t.value:.0f} in the {lead.league.label} "
                    f"(currently {lead.leverage.win_prob:.0%} to win it).")

        lead = max(faced, key=lambda l: l.dollar_swing)
        t = lead.threshold
        if self.public_enemy:
            return (f"{name} is public enemy #1. He's threatening {money} worth of our "
                    f"leagues - keep him under {t.value:.0f} in the {lead.league.label}.")
        if not t.feasible:
            return f"{name} is on the other side in {lead.league.label}, but {t.impossible_reason}"
        return (f"We're facing {name} in {money} worth of leagues. Danger starts around "
                f"{t.value:.0f} in the {lead.league.label} "
                f"(we're {lead.leverage.win_prob:.0%} there right now).")


# ---------------------------------------------------------------------------


def _clip(x: float, grid: list[float]) -> float:
    return max(grid[0], min(grid[-1], x))


def analyze_player(curve: ScenarioCurve) -> PlayerRooting:
    """Score, categorize and narrate one player's cross-league rooting case."""
    ref = curve.reference_projection or 10.0
    lo = _clip(ref * LOW_PERF, curve.grid)
    hi = _clip(max(ref * HIGH_PERF, ref + 8), curve.grid)

    # Signed movement in weighted win probability between a dud and a big game.
    score = round(100.0 * (curve.utility_at(hi) - curve.utility_at(lo)), 1)
    score = max(-100.0, min(100.0, score))

    lines: list[LeagueLine] = []
    for ls in curve.leagues:
        lines.append(
            LeagueLine(
                threshold=compute_threshold(ls.state, ls.exposure),
                leverage=ls.leverage,
                swing=ls.swing,
                dollar_swing=ls.dollar_swing,
                scale=ls.scale,
            )
        )

    if score >= HARD_FOR:
        cat = RootingCategory.ROOT_HARD_FOR
    elif score >= FOR:
        cat = RootingCategory.ROOT_FOR
    elif score >= SLIGHT_FOR:
        cat = RootingCategory.SLIGHT_ROOT_FOR
    elif score <= -HARD_FOR:
        cat = RootingCategory.ROOT_AGAINST
    elif score <= -FOR:
        cat = RootingCategory.ROOT_AGAINST
    elif score <= -SLIGHT_FOR:
        cat = RootingCategory.SLIGHT_ROOT_AGAINST
    else:
        owns = any(l.mine for l in lines)
        faces = any(not l.mine for l in lines)
        cat = (RootingCategory.CONFLICTED if owns and faces and curve.dollar_swing >= 5
               else RootingCategory.NEUTRAL)

    result = PlayerRooting(player=curve.player, curve=curve, lines=lines,
                           score=score, category=cat)
    # A sweet spot is only interesting when the curve actually turns over, or
    # when the player is conflicted (where the range IS the answer).
    if result.is_conflicted:
        # Prefer the band that satisfies every league. When the needs and the
        # affordable ceilings cross, no single performance pleases everyone, so
        # the weighted utility curve decides which side to sacrifice.
        result.sweet_spot = threshold_band(lines, curve) or curve.sweet_spot()
    elif curve.is_capped:
        result.sweet_spot = curve.sweet_spot()
    return result


def threshold_band(lines: list[LeagueLine], curve: ScenarioCurve) -> Optional[tuple[float, float]]:
    """The performance range to hope for, on the reference scale.

    Low end  = the largest amount we NEED anywhere we own him.
    High end = the smallest amount we can AFFORD anywhere we face him.

    Thresholds live in each league's own scoring, so each is divided by that
    league's scale factor before they are compared (spec section 8).

    When the two cross - we need 22 somewhere and can only afford 20 elsewhere -
    no single performance satisfies everyone.  Rather than shrug, we let the side
    carrying more live money anchor a one-sided range, which is what you'd
    actually do watching the game.
    """
    needs = [(l.threshold.value / (l.scale or 1.0), l.dollar_swing)
             for l in lines if l.mine and l.threshold.feasible]
    affords = [(l.threshold.value / (l.scale or 1.0), l.dollar_swing)
               for l in lines if not l.mine and l.threshold.feasible]
    if not needs and not affords:
        return None

    top = curve.grid[-1] if curve.grid else 42.0
    half = lambda x: round(x * 2) / 2  # noqa: E731 - report to the nearest half point
    lo = half(max(v for v, _ in needs)) if needs else 0.0
    hi = half(min(v for v, _ in affords)) if affords else top
    lo, hi = max(0.0, lo), min(top, hi)

    if lo < hi:
        return lo, hi

    # Thresholds cross: whichever side has more money genuinely in play wins.
    own_money = sum(d for _, d in needs)
    face_money = sum(d for _, d in affords)
    if own_money >= face_money and needs:
        return lo, top
    if affords:
        return 0.0, hi
    return lo, top


def rank(players: list[PlayerRooting]) -> list[PlayerRooting]:
    """Most decision-relevant first: biggest live dollar swing wins."""
    return sorted(players, key=lambda p: (-abs(p.score) * 0.01 - p.dollar_swing, p.player.name))


def mark_public_enemy(players: list[PlayerRooting], *, min_score: float = -20.0,
                      min_dollars: float = 20.0) -> None:
    """At most one player per game earns the skull."""
    worst = [p for p in players if p.score <= min_score and p.dollar_swing >= min_dollars]
    if not worst:
        return
    top = min(worst, key=lambda p: (p.score, -p.dollar_swing))
    top.public_enemy = True
    top.category = RootingCategory.PUBLIC_ENEMY
