"""Live in-game updates: react when a player we care about actually does something.

The pre-kickoff push tells you what to want. This tells you what changed and what
to want NOW - because a touchdown rewrites the arithmetic. If we needed A.J. Brown
to clear 15 and he just scored 6.7, we need 8 more, not 15. If we could afford 14
from Stevenson and he just took one to the house, we can afford 8 the rest of the way.

Mechanism: snapshot every starter's live points, poll, diff. Any player whose
score jumps by more than the threshold in one interval is an event. The rooting
engine is then re-run on fresh data, so the instruction attached to the event
reflects the new state of every affected matchup - not the pre-game one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .analysis import GameGuide
from .formatting import display_name, slot_word
from .leverage import compute_leverage
from .models import MatchupState, NFLGame, Side
from .rooting import PlayerRooting

log = logging.getLogger(__name__)

#: A rushing/receiving TD is 6, a passing TD 4, a long catch 2-3. Four points
#: filters out yardage drip while catching every score.
DEFAULT_THRESHOLD = 4.0
#: Never send more than this many player events in one message.
MAX_EVENTS = 4


@dataclass
class Snapshot:
    """What we last saw, per game. Persisted so restarts don't replay events."""

    points: dict[str, float] = field(default_factory=dict)     # "league|player" -> pts
    win_prob: dict[str, float] = field(default_factory=dict)   # league id -> P(win)

    def to_dict(self) -> dict[str, Any]:
        return {"points": self.points, "win_prob": self.win_prob}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Snapshot":
        d = d or {}
        return cls(points=dict(d.get("points") or {}),
                   win_prob=dict(d.get("win_prob") or {}))

    @property
    def is_empty(self) -> bool:
        return not self.points


def take_snapshot(states: Iterable[MatchupState], game: NFLGame) -> Snapshot:
    """Current points for every starter of ours in this game, plus win odds."""
    snap = Snapshot()
    for state in states:
        touched = False
        for exp in state.all_starters():
            if exp.canonical.nfl_team not in game.teams:
                continue
            snap.points[f"{state.league.id}|{exp.canonical.key}"] = exp.current_points
            touched = True
        if touched:
            snap.win_prob[state.league.id] = compute_leverage(state).win_prob
    return snap


@dataclass
class LiveEvent:
    """One player did something worth interrupting you for."""

    rooting: PlayerRooting
    delta: float                       # biggest per-league jump, for headline use
    new_points: float
    per_league: dict[str, float] = field(default_factory=dict)   # league id -> delta

    @property
    def player(self):
        return self.rooting.player

    @property
    def ours(self) -> bool:
        return bool(self.rooting.owned_lines) and not self.rooting.faced_lines

    @property
    def theirs(self) -> bool:
        return bool(self.rooting.faced_lines) and not self.rooting.owned_lines

    @property
    def mixed(self) -> bool:
        return self.rooting.is_conflicted

    # -- language -----------------------------------------------------------
    def reaction(self) -> str:
        """The gut-response line. Deterministic: picked by size and by side."""
        name = display_name(self.player).upper()
        d = self.delta
        if self.mixed:
            return f"{self.rooting.emoji} {name} +{d:.1f} — mixed bag for us."
        if self.ours:
            if d >= 12:
                return f"🔥🔥 {name} WENT NUCLEAR! +{d:.1f}"
            if d >= 6:
                return f"🔥 {name} BIG PLAY, LET'S GO! +{d:.1f}"
            return f"🟢 {name} is cooking. +{d:.1f}"
        if d >= 12:
            return f"☠️ Brutal — {name} just went off. +{d:.1f}"
        if d >= 6:
            return f"😩 UH OH... {name} scored. +{d:.1f}"
        return f"🔻 {name} chipping away. +{d:.1f}"

    def instruction(self) -> str:
        """What to want from here, recomputed on post-play data."""
        r = self.rooting
        owned = [l for l in r.lines if l.mine and l.threshold.feasible]
        faced = [l for l in r.lines if not l.mine and l.threshold.feasible]

        if self.ours and not owned:
            return "KEEP GOING OFF — every point is a bonus now."
        if self.ours:
            lead = max(owned, key=lambda l: l.dollar_swing)
            left = lead.threshold.remaining_required
            if left <= 0:
                return f"That's enough in {lead.league.name} — anything else is gravy."
            return f"KEEP GOING — need {left:.0f} more in {lead.league.name}."
        if self.theirs:
            if not faced:
                lead = max(r.faced_lines, key=lambda l: l.dollar_swing)
                return (f"He's past the line in {lead.league.name}. "
                        f"We need help elsewhere now.")
            lead = max(faced, key=lambda l: l.dollar_swing)
            left = lead.threshold.remaining_required
            if left <= 0:
                return (f"He's past what we could afford in {lead.league.name}. "
                        f"We need help elsewhere now.")
            return f"Need him under {left:.0f} the rest of the way ({lead.league.name})."
        rng = r.range_phrase()
        return f"Now want him {rng}." if rng else "Roughly neutral for us now."

    def league_moves(self, before: Snapshot) -> list[str]:
        """'Turf Wars 52% → 61%' for every league this actually moved."""
        out = []
        for line in sorted(self.rooting.lines, key=lambda l: -l.dollar_swing):
            lid = line.league.id
            now = line.leverage.win_prob
            was = before.win_prob.get(lid)
            side = "ours" if line.mine else "vs us"
            if was is None or abs(now - was) < 0.01:
                out.append(f"   {line.league.name} ({side}): {now:.0%}")
            else:
                arrow = "↑" if now > was else "↓"
                out.append(f"   {line.league.name} ({side}): {was:.0%} {arrow} {now:.0%}")
        return out


def detect_events(before: Snapshot, guide: GameGuide, states: Iterable[MatchupState],
                  game: NFLGame, *, threshold: float = DEFAULT_THRESHOLD) -> list[LiveEvent]:
    """Diff the new state against the snapshot and build events for real jumps."""
    if before.is_empty:
        return []      # first sight of this game: establish a baseline, don't alert

    by_key = {p.player.key: p for p in guide.players}
    deltas: dict[str, dict[str, float]] = {}
    totals: dict[str, float] = {}

    for state in states:
        for exp in state.all_starters():
            if exp.canonical.nfl_team not in game.teams:
                continue
            key = f"{state.league.id}|{exp.canonical.key}"
            was = before.points.get(key)
            if was is None:
                continue          # player entered the lineup after the snapshot
            delta = round(exp.current_points - was, 2)
            if delta >= threshold:
                deltas.setdefault(exp.canonical.key, {})[state.league.id] = delta
                totals[exp.canonical.key] = max(totals.get(exp.canonical.key, 0.0),
                                                exp.current_points)

    events = []
    for player_key, per_league in deltas.items():
        rooting = by_key.get(player_key)
        if rooting is None:
            continue
        events.append(LiveEvent(rooting=rooting, delta=max(per_league.values()),
                                new_points=totals.get(player_key, 0.0),
                                per_league=per_league))
    # Biggest swing in real stakes first.
    events.sort(key=lambda e: -(e.rooting.dollar_swing + e.delta))
    return events


# ---------------------------------------------------------------------------


def live_title(game: NFLGame, events: list[LiveEvent]) -> str:
    lead = events[0] if events else None
    who = display_name(lead.player) if lead else "Update"
    return f"🏈 {slot_word(game.slot)} · {game.matchup} — {who}"


def live_message(events: list[LiveEvent], before: Snapshot, game: NFLGame) -> str:
    """The in-game push: what happened, then what to want now."""
    blocks = []
    for ev in events[:MAX_EVENTS]:
        block = [ev.reaction(), f"➡️ {ev.instruction()}"]
        block += ev.league_moves(before)
        blocks.append("\n".join(block))

    tail = ""
    if game.state.value == "in_progress" and game.period:
        tail = f"\n\n⏱ Q{game.period} {game.clock}".rstrip()
    return "\n\n".join(blocks) + tail
