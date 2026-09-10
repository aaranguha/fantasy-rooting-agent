"""In-game injury tracking: a debounced state machine per player, per game.

The pre-kickoff push already flags anyone ruled out before the game (see
`GameGuide.lineup_alerts`). This module handles what happens *during* a game -
a player limping off, being carted to the locker room, ruled out, or jogging
back on - and turns each real transition into one notification.

Source of truth: ESPN's public game `summary` feed (`NFLScheduleProvider.
game_injuries`), which carries a live `injuries` block that updates within a
few minutes of the TV broadcast. No auth, no Twitter, no scraping.

Debounce: a *new* phase must be seen on two consecutive polls before it fires,
because the feed flickers. `OUT` is authoritative and fires immediately. The
very first time we see a player (already hurt or not) we seed silently - only
transitions from a known prior phase are worth interrupting someone for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .models import CanonicalPlayer
from .rooting import PlayerRooting


class InjuryPhase(str, Enum):
    HEALTHY = "healthy"
    IN_QUESTION = "in_question"   # hurt - questionable/doubtful/being evaluated
    OUT = "out"                   # ruled out for the rest of the game

    @property
    def rank(self) -> int:
        return {"healthy": 0, "in_question": 1, "out": 2}[self.value]


# Phrases (from ESPN's status + type.description) that pin a phase.
_OUT_HINTS = ("ruled out", "out for the game", "out for the season",
              "declared out", "will not return", "won't return", "did not return",
              "injured reserve", "reserve/injured", "coach's decision", "suspend")
_QUESTION_HINTS = ("doubtful", "to return", "being evaluated", "questionable to return",
                   "return is questionable", "evaluated for", "day-to-day", "day to day",
                   "in the blue", "medical tent", "locker room", "left the game",
                   "being looked at", "shaken up")


def classify(status: str, note: str = "") -> InjuryPhase:
    """Map one ESPN injury row to a phase, biased toward the in-game meaning.

    A bare pre-game "Questionable" with no return language means the guy dressed
    and is playing - `HEALTHY` - so we don't later fire a bogus "he's BACK".
    """
    s = f"{status} {note}".strip().lower()
    if any(h in s for h in _OUT_HINTS):
        return InjuryPhase.OUT
    if status.strip().lower() in ("out", "o"):
        return InjuryPhase.OUT
    if any(h in s for h in _QUESTION_HINTS):
        return InjuryPhase.IN_QUESTION
    return InjuryPhase.HEALTHY


@dataclass
class InjuryReport:
    """One player's row in a game's live injury feed, provider-agnostic."""

    espn_id: str
    name: str
    team: str
    phase: InjuryPhase
    detail: str = ""     # body part, e.g. "Ankle"
    note: str = ""        # ESPN's own phrasing, e.g. "Questionable to return"


@dataclass
class PlayerInjuryState:
    """What we persist between polls for one player in one game."""

    phase: str = InjuryPhase.HEALTHY.value
    detail: str = ""
    pending: str = ""                 # a phase seen once, awaiting a 2nd confirm
    pending_polls: int = 0
    notified_phase: str = InjuryPhase.HEALTHY.value
    watched: bool = False             # we promised "will keep you updated"
    seen: bool = False                # have we ever observed this player at all

    def to_dict(self) -> dict:
        return dict(phase=self.phase, detail=self.detail, pending=self.pending,
                    pending_polls=self.pending_polls, notified_phase=self.notified_phase,
                    watched=self.watched, seen=self.seen)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "PlayerInjuryState":
        d = d or {}
        return cls(phase=d.get("phase", "healthy"), detail=d.get("detail", ""),
                   pending=d.get("pending", ""), pending_polls=int(d.get("pending_polls", 0)),
                   notified_phase=d.get("notified_phase", "healthy"),
                   watched=bool(d.get("watched", False)), seen=bool(d.get("seen", False)))


@dataclass
class InjuryUpdate:
    """One transition worth sending."""

    player: CanonicalPlayer
    was: InjuryPhase
    now: InjuryPhase
    detail: str = ""
    note: str = ""
    rooting: Optional[PlayerRooting] = None

    @property
    def _name(self) -> str:
        return self.player.name

    @property
    def ours(self) -> bool:
        r = self.rooting
        return bool(r and r.owned_lines and not r.faced_lines)

    @property
    def theirs(self) -> bool:
        r = self.rooting
        return bool(r and r.faced_lines and not r.owned_lines)

    def headline(self) -> str:
        name = self._name.upper()
        if self.now == InjuryPhase.OUT:
            return f"❌ {name} HAS BEEN RULED OUT"
        if self.now == InjuryPhase.IN_QUESTION:
            what = f" ({self.detail})" if self.detail else ""
            return f"🚑 {name} IS HURT{what}"
        return f"✅ {name} IS BACK IN THE GAME"

    def body(self) -> str:
        if self.now == InjuryPhase.IN_QUESTION:
            note = self.note.strip().rstrip(".") or "status unclear"
            lead = f"{note}. We'll keep you updated."
        elif self.now == InjuryPhase.OUT:
            lead = "He's done for the game."
        else:
            lead = "He's returned to action."
        instr = self._instruction()
        if instr:
            instr = instr[0].upper() + instr[1:]
        return f"{lead} {instr}".strip()

    def _instruction(self) -> str:
        r = self.rooting
        if r is None:
            return ""
        owned = sorted({l.league.name for l in r.owned_lines})
        faced = sorted({l.league.name for l in r.faced_lines})
        if self.now in (InjuryPhase.OUT, InjuryPhase.IN_QUESTION):
            done = self.now == InjuryPhase.OUT
            bits = []
            if owned:
                verb = "you're down a starter in" if done else "he starts for you in"
                bits.append(f"{verb} {', '.join(owned)}")
            if faced:
                verb = "that's points off their board in" if done else "he's against you in"
                bits.append(f"{verb} {', '.join(faced)}")
            return (" - ".join(bits) + ".") if bits else ""
        # back in the game
        bits = []
        if owned:
            bits.append(f"back in your lineup in {', '.join(owned)}")
        if faced:
            bits.append(f"live against you again in {', '.join(faced)}")
        return ("He's " + " and ".join(bits) + ".") if bits else ""


def advance(
    states: dict[str, dict],
    reports: dict[str, InjuryReport],
    *,
    players: dict[str, CanonicalPlayer],
    rooting: Optional[dict[str, PlayerRooting]] = None,
) -> tuple[dict[str, dict], list[InjuryUpdate]]:
    """Run the debounced machine for one game.

    `states`   : player key -> serialized PlayerInjuryState from the last poll
    `reports`  : player key -> this poll's InjuryReport (absent key == HEALTHY)
    `players`  : player key -> CanonicalPlayer, for every player we track here
    `rooting`  : player key -> PlayerRooting, for the recomputed instruction

    Returns (states to persist, updates to send).
    """
    rooting = rooting or {}
    out: dict[str, dict] = {}
    updates: list[InjuryUpdate] = []

    for key, player in players.items():
        st = PlayerInjuryState.from_dict(states.get(key))
        report = reports.get(key)
        observed = report.phase if report else InjuryPhase.HEALTHY
        detail = (report.detail if report else "") or st.detail
        note = report.note if report else ""
        committed = InjuryPhase(st.phase)

        # First ever sighting: seed silently, whatever shape he's in.
        if not st.seen:
            st.seen = True
            st.phase = st.notified_phase = observed.value
            st.detail = detail
            out[key] = st.to_dict()
            continue

        if observed == committed:
            st.pending = ""
            st.pending_polls = 0
            st.detail = detail
            out[key] = st.to_dict()
            continue

        # A real change. OUT is authoritative and commits at once; anything else
        # must be seen on two consecutive polls (these feeds flicker).
        if observed != InjuryPhase.OUT:
            if st.pending != observed.value:
                st.pending = observed.value
                st.pending_polls = 1
                out[key] = st.to_dict()
                continue
            st.pending_polls += 1
            if st.pending_polls < 2:
                out[key] = st.to_dict()
                continue

        # Commit.
        was = committed
        st.phase = observed.value
        st.detail = detail
        st.pending = ""
        st.pending_polls = 0

        notify = observed.value != st.notified_phase
        # Don't announce a return to health for someone we never flagged hurt.
        if observed == InjuryPhase.HEALTHY and not st.watched:
            notify = False

        if notify:
            updates.append(InjuryUpdate(player=player, was=was, now=observed,
                                        detail=detail, note=note,
                                        rooting=rooting.get(key)))
            st.notified_phase = observed.value
            st.watched = observed != InjuryPhase.HEALTHY

        out[key] = st.to_dict()

    # Carry forward state for players no longer in the tracked set (game ended,
    # roster change) so we don't lose their history mid-game.
    for key, raw in states.items():
        out.setdefault(key, PlayerInjuryState.from_dict(raw).to_dict())
    return out, updates


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def injury_title(game, updates: list[InjuryUpdate]) -> str:
    from .formatting import slot_word
    lead = updates[0]
    verb = {"out": "ruled out", "in_question": "hurt", "healthy": "back"}[lead.now.value]
    return f"🚑 {slot_word(game.slot)} · {game.matchup} — {lead.player.short_name} {verb}"


def injury_message(updates: list[InjuryUpdate]) -> str:
    return "\n\n".join(f"{u.headline()}\n{u.body()}".strip() for u in updates)
