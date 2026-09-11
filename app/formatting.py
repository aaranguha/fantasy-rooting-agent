"""Message rendering.

Two audiences (spec section 24): the phone gets something you can read at a
glance while the anthem plays; the CLI/dashboard/logs get the full working.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from .analysis import GameGuide
from .models import Confidence, SlotType
from .rooting import PlayerRooting

MAX_PHONE_PLAYERS = 5
PHONE_CHAR_BUDGET = 1100


def _minutes_out(game, now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    return max(0, int((game.kickoff - now).total_seconds() // 60))


def slot_word(slot: SlotType) -> str:
    """TNF / SNF / MNF by name; anything else is just 'Primetime'.

    Wednesday openers, Black Friday, Christmas and London games are all real
    standalone national windows, but naming each variety adds nothing on a lock
    screen - the matchup right beside it already says which game it is.
    """
    return {SlotType.TNF: "TNF", SlotType.SNF: "SNF",
            SlotType.MNF: "MNF"}.get(slot, "Primetime")


# ---------------------------------------------------------------------------
# Phone-sized
# ---------------------------------------------------------------------------


def phone_title(guide: GameGuide, now: Optional[datetime] = None) -> str:
    mins = _minutes_out(guide.game, now)
    if mins <= 0:
        when = "KICKING OFF"
    elif mins < 90:
        when = f"in {mins}"
    elif mins < 24 * 60:
        when = f"in {mins / 60:.0f}h"
    else:
        when = f"in {mins / 1440:.0f}d"
    return f"\U0001f3c8 {slot_word(guide.game.slot)} {when}: {guide.game.matchup}"


def _league_tag(line, width: int = 28) -> str:
    """Just the league name. Buy-ins still drive every calculation behind the
    scenes, but on a phone the name is what you recognise - the dollar figure is
    noise you have to re-add in your head."""
    name = line.league.name.strip()
    return name if len(name) <= width else name[: width - 1].rstrip() + "…"


def display_name(player) -> str:
    """Defenses read as a team name otherwise, which is ambiguous on a phone."""
    return f"{player.name} D/ST" if player.position == "DST" else player.name


def _best(lines: list) -> "tuple":
    """The most decision-relevant line, preferring one with a usable threshold.

    A league we're already winning without him tells us nothing about what to
    want, so it must not speak for the player when another league does need him.
    """
    if not lines:
        return None
    usable = [l for l in lines if l.threshold.feasible]
    return max(usable or lines, key=lambda l: l.dollar_swing)


def want_phrase(p: PlayerRooting) -> str:
    """The single instruction that answers 'what do I want from this guy?'"""
    owned = [l for l in p.lines if l.mine]
    faced = [l for l in p.lines if not l.mine]

    # Ruled out: the only actionable instruction is bench him (where we own him)
    # or nothing to do (faced only). The faced side never rescues an owned OUT.
    if p.sidelined:
        return f"{p.injury} — BENCH HIM" if owned else f"{p.injury} — good for us"

    # Ours everywhere and against us nowhere: there is no ceiling, so no
    # threshold is worth printing. More is strictly better.
    if owned and not faced:
        return f"GO OFF (proj: {p.curve.reference_projection:.0f})"

    if p.is_conflicted and p.range_phrase():
        lo, hi = p.sweet_spot
        dom = p.dominant_line
        scale = dom.scale if dom and dom.scale else 1.0
        lo, hi = lo * scale, hi * scale
        top = p.curve.grid[-1] * scale
        if hi >= top - 0.01:
            return f"NEED {lo:.0f}+"
        if lo <= 0.01:
            return f"UNDER {hi:.0f}"
        return f"WANT {lo:.0f}-{hi:.0f}"

    if p.category.is_positive and owned:
        lead = _best(owned)
        t = lead.threshold
        if not t.feasible:
            return "MORE IS BETTER" if t.value == 0 else "LONG SHOT"
        if t.confidence == Confidence.EXACT:
            return f"NEED {t.value:.2f}"
        return f"NEED {t.value:.0f}+"

    if p.category.is_negative and faced:
        lead = _best(faced)
        t = lead.threshold
        if not t.feasible:
            return "CAN'T HURT US" if t.value == 0 else "NEED HIM SHUT DOWN"
        if t.confidence == Confidence.EXACT:
            return f"UNDER {t.value:.2f}"
        return f"UNDER {t.value:.0f}"

    return "LOW STAKES"


def _injury_mark(p: PlayerRooting) -> str:
    """' (Q)' for a soft tag. Nothing when sidelined - the row's 🚑 emoji and
    'OUT — BENCH HIM' instruction already carry it - and nothing when healthy."""
    if p.sidelined:
        return ""
    return f" ({p.injury})" if p.injury else ""


def _decided_footer(players: list[PlayerRooting]) -> str:
    """One condensed block per league whose matchup is effectively over, still
    naming which side each player is on so a lean survives even when the
    leverage math has (correctly) stopped caring."""
    by_leagues: dict[str, dict[str, list[str]]] = {}
    for p in players:
        key = ", ".join(sorted({l.league.name for l in p.lines}))
        sides = by_leagues.setdefault(key, {"ours": [], "vs us": []})
        name = display_name(p.player) + (f" ({p.injury})" if p.injury else "")
        if p.owned_lines:
            sides["ours"].append(name)
        if p.faced_lines:
            sides["vs us"].append(name)
    out = []
    for leagues, sides in by_leagues.items():
        out.append(f"\U0001f4cb {leagues} (matchup near-decided)")
        if sides["ours"]:
            out.append(f"   lean ours: {', '.join(sides['ours'])}")
        if sides["vs us"]:
            out.append(f"   lean vs us: {', '.join(sides['vs us'])}")
    return "\n".join(out)


def _lineup_alert(players: list[PlayerRooting]) -> str:
    """The one line that has to jump out: someone ruled out is still starting."""
    if not players:
        return ""
    parts = []
    for p in players:
        lgs = ", ".join(sorted({l.league.name for l in p.owned_lines}))
        parts.append(f"{display_name(p.player)} ({p.injury}) in {lgs}")
    return "⚠️ LINEUP — bench now: " + "; ".join(parts)


def _group_by_emoji(players: list[PlayerRooting]) -> list[PlayerRooting]:
    """Cluster same-verdict players together instead of interleaving them.

    Stable "group by first appearance": the significance ranking already
    picked *which* players make the cut and in what priority, so within that
    set we only reorder for display - every 🚀 together, then whichever emoji
    shows up next, and so on - without needing an arbitrary category table.
    """
    buckets: dict[str, list[PlayerRooting]] = {}
    order: list[str] = []
    for p in players:
        e = p.emoji
        if e not in buckets:
            buckets[e] = []
            order.append(e)
        buckets[e].append(p)
    return [p for e in order for p in buckets[e]]


def _phone_player_block(p: PlayerRooting) -> str:
    """Three lines: what we want, where he's ours, where he's against us."""
    owned = [l for l in p.lines if l.mine]
    faced = [l for l in p.lines if not l.mine]

    lines = [f"{p.emoji} {display_name(p.player)}{_injury_mark(p)} · {want_phrase(p)}"]
    if owned:
        lines.append("   ours: " + ", ".join(
            _league_tag(l) for l in sorted(owned, key=lambda l: -l.dollar_swing)))
    if faced:
        lines.append("   vs us: " + ", ".join(
            _league_tag(l) for l in sorted(faced, key=lambda l: -l.dollar_swing)))
    return "\n".join(lines)


def _background_footer(players: list[PlayerRooting]) -> str:
    """One condensed block per low-priority league, still saying which side
    each name is on - "mentioned, not a priority" doesn't mean "unlabeled."
    A player owned in one low-priority league and faced in another (rare) shows
    up on both lines, same as a full block would.
    """
    by_leagues: dict[str, dict[str, list[str]]] = {}
    for p in players:
        key = ", ".join(sorted({l.league.name for l in p.lines}))
        sides = by_leagues.setdefault(key, {"ours": [], "vs us": []})
        name = display_name(p.player)
        if p.owned_lines:
            sides["ours"].append(name)
        if p.faced_lines:
            sides["vs us"].append(name)

    out = []
    for leagues, sides in by_leagues.items():
        out.append(f"\U0001f515 {leagues} (low priority)")
        if sides["ours"]:
            out.append(f"   ours: {', '.join(sides['ours'])}")
        if sides["vs us"]:
            out.append(f"   vs us: {', '.join(sides['vs us'])}")
    return "\n".join(out)


def phone_message(guide: GameGuide, now: Optional[datetime] = None) -> str:
    """The 15-minute-warning push body.

    Reads top to bottom as: what do I want from this player, and which of my
    leagues does he sit on. Nothing else earns a line on a lock screen.

    A league flagged `low_priority` never earns its players a full block here -
    they're condensed into one footer line instead, so a league you still want
    tracked never crowds out the ones that actually drive your night.
    """
    fg, bg = guide.foreground_relevant, guide.background_relevant
    fn = guide.footnote_relevant
    alert = _lineup_alert(guide.lineup_alerts)
    if not fg and not bg and not fn and not alert:
        return "Nobody of ours starting, nobody against us. Neutral watch."

    # Significance order picks WHO makes the cut; display order then clusters
    # same-verdict players together (see `_group_by_emoji`) rather than
    # interleaving them by raw score.
    selected = fg[:MAX_PHONE_PLAYERS]

    # Deliberately no money total here - the per-player league tags already say
    # what is at stake, and a dollar figure only adds arithmetic to a glance.
    tail = []
    if bg:
        tail.append(_background_footer(bg))
    if fn:
        tail.append(_decided_footer(fn))
    priority = _priority_line(guide)
    if priority:
        tail.append(f"\U0001f3af {priority}")

    def render(players):
        parts = [_phone_player_block(p) for p in _group_by_emoji(players)]
        if tail:
            parts.append("\n".join(tail))
        core = "\n\n".join(parts)
        return (alert + "\n\n" + core).strip() if alert else core

    body = render(selected)
    while len(body) > PHONE_CHAR_BUDGET and len(selected) > 2:
        selected.pop()      # drop the least significant (still in rank order)
        body = render(selected)
    return body


def _priority_line(guide: GameGuide) -> str:
    """One short line naming the matchup this game will actually turn on."""
    lead = guide.top_league
    if not lead:
        return ""
    return f"{lead.state.league.name} is tightest ({lead.win_prob:.0%} to win)"


# ---------------------------------------------------------------------------
# Full report
# ---------------------------------------------------------------------------


def long_report(guide: GameGuide, tz: Optional[ZoneInfo] = None,
                now: Optional[datetime] = None) -> str:
    tz = tz or ZoneInfo("America/Los_Angeles")
    g = guide.game
    local = g.kickoff.astimezone(tz)
    out: list[str] = []
    out.append("=" * 68)
    out.append(f"\U0001f3c8 {slot_word(g.slot)} - {g.matchup}   ({local:%a %b %-d, %-I:%M %p %Z})")
    if g.broadcast:
        out.append(f"   {g.broadcast}" + (f"   [{g.state.value}]" if g.state.value != "not_started" else ""))
    out.append("=" * 68)

    if not guide.players:
        out.append("\nNobody in this game is starting for us or against us. Neutral watch.\n")
        return "\n".join(out)

    # -- Leagues affected ---------------------------------------------------
    out.append("\nLEAGUES AFFECTED")
    out.append("-" * 68)
    for lev in guide.affected_leagues:
        s = lev.state
        tag = " [low priority]" if s.league.low_priority else ""
        out.append(
            f"  {(s.league.name[:26] + tag):<34} ${s.league.buy_in_usd:>5.0f} "
            f"x{s.league.importance_multiplier:<4g} = {s.league.effective_weight:>6.1f}w | "
            f"{s.current_score_mine:6.1f} vs {s.current_score_opponent:6.1f} | "
            f"proj {s.projected_final_mine:6.1f}-{s.projected_final_opponent:6.1f} "
            f"({s.projected_margin:+.1f}) | win {lev.win_prob:5.0%} {lev.descriptor}"
        )
    out.append(f"  {'':26} money in play: ${guide.money_at_stake:.0f}   "
               f"live (closeness-weighted): ${guide.live_dollars:.0f}")

    # -- Players ------------------------------------------------------------
    out.append("\nROOTING GUIDE")
    out.append("-" * 68)
    for p in guide.players:
        if not p.matters and abs(p.score) < 1 and not p.injury:
            continue
        inj = f" 🚑 {p.injury}" if p.sidelined else (f" ({p.injury})" if p.injury else "")
        out.append(f"\n{p.headline()}{inj}   [score {p.score:+.1f} | ${p.dollar_swing:.0f} live "
                   f"| {p.best_confidence.value}]")
        for line in sorted(p.lines, key=lambda l: -l.dollar_swing):
            out.append(f"    {line.describe()}")
        out.append(f"    → {p.narrative()}")

    # -- Game plan ----------------------------------------------------------
    out.append("\n" + "-" * 68)
    out.append("GAME PLAN")
    for label, group in (("\U0001f7e2 ROOT FOR", guide.root_for),
                         ("\U0001f534 ROOT AGAINST", guide.root_against),
                         ("⚖️  CONFLICTED", guide.conflicted)):
        if group:
            out.append(f"  {label}: " + ", ".join(
                p.player.short_name + (f" ({p.range_phrase()})" if p.range_phrase() else "")
                for p in group))
    if guide.lineup_alerts:
        out.append("  ⚠️ LINEUP (bench now): " + ", ".join(
            f"{p.player.short_name} {p.injury}" for p in guide.lineup_alerts))
    if guide.footnote_relevant:
        out.append("  \U0001f4cb Near-decided leans: " + ", ".join(
            f"{p.player.short_name} ({'ours' if p.owned_lines else 'vs us'})"
            + (f" 🚑{p.injury}" if p.sidelined else "")
            for p in guide.footnote_relevant))
    swing = guide.biggest_swing
    if swing:
        out.append(f"  \U0001f3af Biggest swing: {swing.player.name} (${swing.dollar_swing:.0f} live)")
    pr = _priority_line(guide)
    if pr:
        out.append(f"  VERDICT: {pr}")
    out.append("")
    return "\n".join(out)


def week_summary(guides: list[GameGuide], tz: ZoneInfo) -> str:
    out = ["", "THIS WEEK'S PRIMETIME SLATE", "=" * 68]
    for g in guides:
        local = g.game.kickoff.astimezone(tz)
        rel = g.relevant
        top = ", ".join(f"{p.emoji} {p.player.short_name}" for p in rel[:4]) or "nothing of ours"
        out.append(f"{slot_word(g.game.slot):<10} {g.game.matchup:<11} {local:%a %-I:%M%p}  "
                   f"${g.money_at_stake:>5.0f} in play  |  {top}")
    out.append("")
    return "\n".join(out)
