"""Game-level guides, league weighting, exposure and graceful degradation
(spec sections 2, 12, 23, 25)."""

from __future__ import annotations

import pytest

from app.analysis import Analyzer, GameGuide
from app.config import AppConfig, LeagueConfig
from app.formatting import long_report, phone_message, phone_title
from app.models import DataTier, MatchupState, Platform, PlayerGameState as GS, SlotType
from app.leverage import compute_leverage

from .factories import filler, finished, make_game, make_league, make_state, player


class Ctx:
    """Minimal stand-in for WeekContext."""

    def __init__(self, states, games):
        self.ok_states = states
        self.states = states
        self.games = games
        self.season, self.week = 2026, 5
        self.errors = []


def analyzer() -> Analyzer:
    cfg = AppConfig(season=2026, minutes_before=15)
    return Analyzer(cfg, registry=None, nfl=object())


# ---------------------------------------------------------------------------
# League weighting is transparent, not a black box
# ---------------------------------------------------------------------------

def test_effective_weight_is_just_buy_in_times_multiplier():
    assert make_league("Dynasty", 100).effective_weight == 100
    assert make_league("Family", 20).effective_weight == 20
    assert make_league("Work", 50, mult=1.5).effective_weight == 75
    assert make_league("Casual", 40, mult=0.75).effective_weight == 30


def test_a_hundred_dollar_league_matters_five_times_a_twenty_dollar_one():
    big, small = make_league("Dynasty", 100), make_league("Family", 20)
    assert big.effective_weight / small.effective_weight == 5.0


def test_free_leagues_keep_a_usable_weight_rather_than_going_to_zero():
    free = make_league("Free For All", 0)
    assert free.effective_weight == 10
    assert make_league("Free But Loved", 0, mult=2.0).effective_weight == 20


# ---------------------------------------------------------------------------
# Game guide
# ---------------------------------------------------------------------------

def build_guide() -> GameGuide:
    ajb = player("A.J. Brown", "WR", "PHI")
    ceedee = player("CeeDee Lamb", "WR", "DAL")
    saquon = player("Saquon Barkley", "RB", "PHI")

    dyn = make_league("Dynasty", 100)
    work = make_league("Work", 50)
    fam = make_league("Family", 20)

    s1 = make_state(dyn,
                    my_players=[(ajb, 17.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                    opp_players=[(ceedee, 16.0, 0, GS.NOT_STARTED)] + filler("t", 8, 13))
    s2 = make_state(work,
                    my_players=[(saquon, 18.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                    opp_players=filler("t", 9, 14))
    s3 = make_state(fam,
                    my_players=filler("m", 9, 14),
                    opp_players=[(saquon, 18.0, 0, GS.NOT_STARTED)] + filler("t", 8, 13))

    game = make_game("DAL", "PHI", slot=SlotType.SNF)
    return analyzer().analyze_game(Ctx([s1, s2, s3], [game]), game)


def test_game_guide_groups_players_and_sums_the_money():
    guide = build_guide()
    names = {p.player.name for p in guide.relevant}
    assert {"A.J. Brown", "CeeDee Lamb", "Saquon Barkley"} <= names
    assert guide.money_at_stake == 170
    assert guide.live_dollars > 0
    assert guide.biggest_swing is not None


def test_game_guide_separates_root_for_against_and_conflicted():
    guide = build_guide()
    assert "A.J. Brown" in {p.player.name for p in guide.root_for}
    assert "CeeDee Lamb" in {p.player.name for p in guide.root_against}
    assert "Saquon Barkley" in {p.player.name for p in guide.conflicted}


def test_only_starters_are_analyzed():
    """A benched player in the same NFL game must never appear."""
    from app.models import LineupStatus, Side
    from .factories import exposure

    star = player("Bench Star", "RB", "PHI")
    lg = make_league("Dynasty", 100)
    st = make_state(lg, my_players=filler("m", 9, 14), opp_players=filler("t", 9, 14))
    st.my_bench.append(exposure(lg, star, Side.MINE, proj=25.0, starter=False))

    game = make_game("DAL", "PHI")
    guide = analyzer().analyze_game(Ctx([st], [game]), game)
    assert "Bench Star" not in {p.player.name for p in guide.players}


def test_players_not_in_this_game_are_excluded():
    elsewhere = player("Other Game Guy", "RB", "KC")
    lg = make_league("Dynasty", 100)
    st = make_state(lg, my_players=[(elsewhere, 20.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                    opp_players=filler("t", 9, 14))
    game = make_game("DAL", "PHI")
    guide = analyzer().analyze_game(Ctx([st], [game]), game)
    assert guide.players == []


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def test_phone_message_names_the_leagues_and_the_number_we_want():
    """The push must answer, per player: how much do I want from him, and which
    of MY leagues is he on - by name, on which side."""
    guide = build_guide()
    title, body = phone_title(guide), phone_message(guide)
    assert "SNF" in title and "DAL @ PHI" in title
    assert len(body) <= 1200, f"push body too long: {len(body)}"

    # Leagues appear by NAME, with the side made explicit.
    assert "Dynasty" in body and "Work" in body and "Family" in body
    assert "ours:" in body and "vs us:" in body

    # Every listed player carries a target number.
    player_lines = [l for l in body.splitlines() if " · " in l]
    assert len(player_lines) >= 3
    for line in player_lines:
        assert any(k in line for k in ("GO OFF", "NEED", "UNDER", "WANT",
                                       "MORE IS BETTER", "CAN'T HURT US",
                                       "LOW STAKES", "LONG SHOT")), line

    assert "across" not in body, "the money total was removed as noise"


def test_phone_message_is_compact():
    """Three lines per player at most: verdict, ours, against."""
    body = phone_message(build_guide())
    for block in body.split("\n\n"):
        assert len(block.splitlines()) <= 3, block


def test_conflicted_player_gets_a_two_sided_range_in_the_push():
    body = phone_message(build_guide())
    saquon = next(b for b in body.split("\n\n") if "Saquon" in b)
    assert "ours:" in saquon and "vs us:" in saquon
    assert any(k in saquon for k in ("WANT", "NEED", "UNDER"))


def test_defenses_are_labelled_as_dst_in_the_push():
    from app.formatting import display_name
    from app.models import CanonicalPlayer

    dst = CanonicalPlayer("dst:NE", "New England Patriots", "DST", "NE")
    wr = CanonicalPlayer("k", "A.J. Brown", "WR", "PHI")
    assert display_name(dst) == "New England Patriots D/ST"
    assert display_name(wr) == "A.J. Brown"


def test_slot_labels_are_tnf_snf_mnf_or_plain_primetime():
    from app.formatting import slot_word
    from app.models import SlotType

    assert slot_word(SlotType.TNF) == "TNF"
    assert slot_word(SlotType.SNF) == "SNF"
    assert slot_word(SlotType.MNF) == "MNF"
    # A Wednesday opener, a London game and Christmas are all just "Primetime".
    assert slot_word(SlotType.PRIMETIME) == "Primetime"
    assert slot_word(SlotType.HOLIDAY) == "Primetime"
    assert slot_word(SlotType.INTERNATIONAL) == "Primetime"


def test_uncontested_player_says_go_off_with_his_projection():
    """Owned somewhere and faced nowhere: no ceiling exists, so no threshold is
    worth printing - just let him cook."""
    from app.formatting import want_phrase
    from app.leverage import compute_leverage
    from app.rooting import analyze_player
    from app.scenarios import build_curve

    ajb = player("A.J. Brown", "WR", "PHI")
    states = []
    for name, money in (("Turf Wars", 50), ("Fantasy Football", 35)):
        lg = make_league(name, money)
        states.append(make_state(lg,
                                 my_players=[(ajb, 16.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                                 opp_players=filler("t", 9, 14.5)))
    scenarios = [(st, st.my_starters[0], compute_leverage(st)) for st in states]
    r = analyze_player(build_curve(ajb, scenarios))

    assert not r.is_conflicted
    phrase = want_phrase(r)
    assert phrase == "GO OFF (proj: 16)", phrase


def test_a_player_we_face_anywhere_keeps_a_ceiling_not_go_off():
    from app.formatting import want_phrase
    from app.leverage import compute_leverage
    from app.rooting import analyze_player
    from app.scenarios import build_curve

    guy = player("Mixed Guy", "RB", "PHI")
    own = make_league("Ours", 50)
    face = make_league("Theirs", 50)
    s1 = make_state(own, my_players=[(guy, 15.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                    opp_players=filler("t", 9, 14))
    s2 = make_state(face, my_players=filler("m", 9, 14),
                    opp_players=[(guy, 15.0, 0, GS.NOT_STARTED)] + filler("t", 8, 13))
    scenarios = []
    for st in (s1, s2):
        lev = compute_leverage(st)
        for e in st.all_starters():
            if e.canonical.key == guy.key:
                scenarios.append((st, e, lev))
    r = analyze_player(build_curve(guy, scenarios))
    assert "GO OFF" not in want_phrase(r)


def test_a_league_we_already_win_without_him_does_not_speak_for_the_player():
    """Regression: the highest-stakes league can have NO usable threshold (we win
    even if he's shut out). It must not speak for the player while another league
    genuinely needs him."""
    from app.formatting import _best

    class FakeThreshold:
        def __init__(self, feasible, value):
            self.feasible, self.value = feasible, value

    class FakeLine:
        def __init__(self, feasible, value, swing):
            self.threshold = FakeThreshold(feasible, value)
            self.dollar_swing = swing

    locked = FakeLine(feasible=False, value=0.0, swing=90.0)   # biggest, useless
    tight = FakeLine(feasible=True, value=12.0, swing=20.0)    # smaller, informative

    assert _best([locked, tight]) is tight, "a decided league must not set the target"
    # ...but if nothing is usable, fall back rather than returning nothing.
    assert _best([locked]) is locked
    assert _best([]) is None
    # Among usable lines, the biggest swing still wins.
    bigger = FakeLine(feasible=True, value=20.0, swing=50.0)
    assert _best([tight, bigger]) is bigger


def test_long_report_shows_league_weights_thresholds_and_a_verdict():
    text = long_report(build_guide())
    assert "LEAGUES AFFECTED" in text and "ROOTING GUIDE" in text and "GAME PLAN" in text
    assert "Dynasty" in text and "$  100" in text.replace("$ 100", "$  100") or "100" in text
    assert "VERDICT" in text
    assert "win " in text


def test_a_game_with_no_exposure_says_so_kindly():
    lg = make_league("Dynasty", 100)
    st = make_state(lg, my_players=filler("m", 9, 14, team="KC"),
                    opp_players=filler("t", 9, 14, team="KC"))
    game = make_game("DAL", "PHI")
    guide = analyzer().analyze_game(Ctx([st], [game]), game)
    assert "neutral" in phone_message(guide).lower()


# ---------------------------------------------------------------------------
# Exposure + degradation
# ---------------------------------------------------------------------------

def test_exposure_table_reports_raw_counts_and_weighted_value():
    ajb = player("A.J. Brown", "WR", "PHI")
    states = []
    for name, money in (("Dynasty", 100), ("Work", 50)):
        lg = make_league(name, money)
        states.append(make_state(lg,
                                 my_players=[(ajb, 17.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                                 opp_players=filler("t", 9, 14)))
    lg = make_league("Family", 20)
    states.append(make_state(lg, my_players=filler("m", 9, 14),
                             opp_players=[(ajb, 17.0, 0, GS.NOT_STARTED)] + filler("t", 8, 13)))

    rows = analyzer().exposure_table(Ctx(states, []))
    row = next(r for r in rows if r["player"].name == "A.J. Brown")
    assert len(row["ours"]) == 2 and len(row["against"]) == 1
    assert row["weighted"] == 100 + 50 - 20


def test_a_failed_league_does_not_stop_the_others():
    good = make_state(make_league("Dynasty", 100),
                      my_players=filler("m", 9, 14), opp_players=filler("t", 9, 14))
    broken = MatchupState(league=make_league("Broken", 50), week=5,
                          error="AUTH: ESPN rejected the request", tier=DataTier.MINIMUM)
    ctx = Ctx([good], [])
    ctx.states = [good, broken]
    ctx.errors = ["Broken: AUTH: ESPN rejected the request"]
    rows = analyzer().exposure_table(ctx)
    assert rows, "the healthy league must still produce analysis"


def test_missing_projections_degrade_to_current_scores_without_crashing():
    p = player("Target", "RB", "PHI")
    lg = make_league("Dynasty", 100)
    st = make_state(lg,
                    my_players=[(p, 0.0, 0.0, GS.NOT_STARTED)] + finished("m", 8, 14),
                    opp_players=finished("t", 9, 13))
    game = make_game("DAL", "PHI")
    guide = analyzer().analyze_game(Ctx([st], [game]), game)
    assert guide.players
    text = long_report(guide)
    assert "ROOTING GUIDE" in text


def test_week_context_reports_the_worst_data_tier_not_the_best():
    """One degraded league must downgrade the whole run's reported tier."""
    from app.analysis import WeekContext

    best = make_state(make_league("Good", 100),
                      my_players=filler("m", 9, 14), opp_players=filler("t", 9, 14))
    best.tier = DataTier.BEST
    degraded = make_state(make_league("No Projections", 50),
                          my_players=filler("m", 9, 14), opp_players=filler("t", 9, 14))
    degraded.tier = DataTier.PROJECTIONS_ONLY

    ctx = WeekContext(season=2026, week=5, states=[best, degraded], games=[], registry=None)
    assert ctx.tier is DataTier.PROJECTIONS_ONLY


def test_push_title_reads_naturally_at_any_distance_from_kickoff():
    from app.formatting import phone_title
    from app.analysis import GameGuide

    def title(minutes):
        return phone_title(GameGuide(game=make_game(minutes_out=minutes), players=[]))

    assert "in 15:" in title(15.1)     # minutes, when kickoff is close
    assert "in 2h:" in title(120.1)    # hours, further out
    assert "in 2d:" in title(2880.1)   # days, for a whole-week preview
    assert "KICKING OFF" in title(-1)


def test_push_body_carries_no_dollar_figures():
    """Buy-ins drive the maths but never appear in the push - league NAMES are
    what you recognise at a glance."""
    body = phone_message(build_guide())
    assert "$" not in body, body
    assert "Dynasty" in body and "Work" in body and "Family" in body
