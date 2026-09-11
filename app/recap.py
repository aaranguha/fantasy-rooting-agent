"""Post-game recap for a finished TNF/SNF/MNF game: one "how did tonight
actually go" message once the final whistle blows.

Nothing new is computed here - this is the exact same rooting/leverage engine
(`Analyzer.analyze_game`) run one more time on final data. The pre-kickoff
push answers "what do I want to happen"; this answers "what actually did," by
comparing each rostered starter's final line to his pre-game projection and
naming who moved the needle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .analysis import GameGuide
from .formatting import display_name, slot_word
from .rooting import PlayerRooting

#: A performance only earns a line here past these bars - projected 3, scored
#: 6 isn't a story; projected 3, scored 14 is.
SMASH_MULTIPLE = 1.4
SMASH_FLOOR = 8.0
BUST_MULTIPLE = 0.5
BUST_MIN_PROJECTION = 6.0
#: Cap each bucket so a wild game doesn't turn the recap into a wall of text.
MAX_PER_BUCKET = 4

_VERDICT_LABEL = {
    ("smash", "ours"): ("✅", "smashed it for you"),
    ("smash", "vs us"): ("\U0001f62c", "smashed it against you"),
    ("smash", "mixed"): ("⚖️", "went off (mixed bag)"),
    ("bust", "ours"): ("\U0001f62c", "busted for you"),
    ("bust", "vs us"): ("✅", "busted against you — lucky break"),
    ("bust", "mixed"): ("⚖️", "busted (mixed bag)"),
}


@dataclass
class Verdict:
    rooting: PlayerRooting
    tag: str          # "smash" | "bust"
    side: str         # "ours" | "vs us" | "mixed"
    actual: float
    proj: float
    league: str

    @property
    def surprise(self) -> float:
        return abs(self.actual - self.proj)

    def line(self) -> str:
        emoji, label = _VERDICT_LABEL[(self.tag, self.side)]
        return (f"{emoji} {display_name(self.rooting.player)} — {label} "
                f"({self.actual:.1f} vs {self.proj:.1f} proj, {self.league})")


def _dominant_exposure(p: PlayerRooting):
    """The exposure (and its league line) that moves the most real money for
    him - actual/projected are read off of it so the recap uses the same
    scoring format the pre-game push already leaned on."""
    lead = p.dominant_line
    if lead is None:
        return None, None
    for ls in p.curve.leagues:
        if ls.league.id == lead.league.id:
            return ls.exposure, lead
    return None, lead


def _side(p: PlayerRooting) -> str:
    if p.is_conflicted:
        return "mixed"
    return "ours" if p.owned_lines else "vs us"


def verdict_for(p: PlayerRooting) -> Optional[Verdict]:
    """A notable over/under vs projection, or None if he was roughly on line."""
    exp, lead = _dominant_exposure(p)
    if exp is None or lead is None or exp.projected_points <= 0:
        return None
    actual, proj = exp.current_points, exp.projected_points
    side = _side(p)
    if actual >= max(proj * SMASH_MULTIPLE, proj + 10) and actual >= SMASH_FLOOR:
        return Verdict(p, "smash", side, actual, proj, lead.league.name)
    if proj >= BUST_MIN_PROJECTION and actual <= proj * BUST_MULTIPLE:
        return Verdict(p, "bust", side, actual, proj, lead.league.name)
    return None


def recap_title(game) -> str:
    return (f"\U0001f3c1 {slot_word(game.slot)} RECAP: {game.matchup} final "
            f"({game.away_score}-{game.home_score})")


def recap_body(guide: GameGuide) -> str:
    out: list[str] = []

    swing = guide.biggest_swing
    if swing and swing.dollar_swing >= 5:
        exp, lead = _dominant_exposure(swing)
        if exp is not None and lead is not None:
            out.append(f"\U0001f3af Biggest swing: {display_name(swing.player)} "
                       f"(${swing.dollar_swing:.0f} real) — finished with "
                       f"{exp.current_points:.1f} in {lead.league.name}.")

    verdicts = [v for v in (verdict_for(p) for p in guide.players) if v]
    smashes = sorted((v for v in verdicts if v.tag == "smash"),
                     key=lambda v: -v.surprise)[:MAX_PER_BUCKET]
    busts = sorted((v for v in verdicts if v.tag == "bust"),
                   key=lambda v: -v.surprise)[:MAX_PER_BUCKET]
    if smashes:
        out.append("SMASHED PROJECTION\n" + "\n".join(f"   {v.line()}" for v in smashes))
    if busts:
        out.append("BUSTED\n" + "\n".join(f"   {v.line()}" for v in busts))

    verdict_ids = {id(v.rooting) for v in verdicts}
    hurt = [p for p in guide.players if p.sidelined and id(p) not in verdict_ids]
    if hurt:
        out.append("\U0001f691 " + ", ".join(
            f"{display_name(p.player)} ({p.injury})" for p in hurt))

    leagues = guide.affected_leagues
    if leagues:
        out.append("LEAGUES NOW\n" + "\n".join(
            f"   {lev.state.league.name}: {lev.win_prob:.0%} ({lev.descriptor})"
            for lev in leagues))

    if guide.live_dollars > 0:
        out.append(f"${guide.live_dollars:.0f} still genuinely in play elsewhere this week.")

    if not out:
        out.append("Quiet night for your teams — nobody moved the needle much.")
    return "\n\n".join(out)
