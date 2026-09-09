"""Different leagues score differently, and the engine must not pretend a point
in one is a point in another (spec section 8)."""

from __future__ import annotations

import pytest

from app.leverage import compute_leverage
from app.models import PlayerGameState as GS, ScoringSettings
from app.providers.sleeper import score_stats
from app.rooting import analyze_player
from app.scenarios import build_curve

from .factories import HALF, PPR, SUPERFLEX_6TD, TE_PREM, filler, finished, make_league, make_state, player


def test_scoring_format_names_are_human_readable():
    assert PPR.format_name == "PPR"
    assert HALF.format_name == "Half-PPR"
    assert SUPERFLEX_6TD.format_name == "PPR (6pt pass TD)"
    assert TE_PREM.format_name == "PPR (TE+1)"
    assert ScoringSettings(ppr=0.0).format_name == "Standard"


def test_materially_different_formats_are_flagged_as_not_comparable():
    assert PPR.comparable_to(ScoringSettings(ppr=0.9))
    assert not PPR.comparable_to(HALF)
    assert not PPR.comparable_to(SUPERFLEX_6TD)
    assert not PPR.comparable_to(TE_PREM)


def test_raw_stat_lines_are_scored_with_each_leagues_own_rules():
    """The same stat line yields different points in PPR vs half-PPR vs 6pt-TD."""
    line = {"rec": 7, "rec_yd": 92, "rec_td": 1, "rush_yd": 10,
            "pass_td": 2, "pass_yd": 250, "gp": 1, "pts_ppr": 999}
    ppr = {"rec": 1.0, "rec_yd": 0.1, "rec_td": 6, "rush_yd": 0.1,
           "pass_td": 4, "pass_yd": 0.04}
    half = dict(ppr, rec=0.5)
    six = dict(ppr, pass_td=6)

    assert score_stats(line, ppr) == pytest.approx(7 + 9.2 + 6 + 1 + 8 + 10, abs=0.01)
    assert score_stats(line, half) == pytest.approx(score_stats(line, ppr) - 3.5, abs=0.01)
    assert score_stats(line, six) == pytest.approx(score_stats(line, ppr) + 4, abs=0.01)
    # Sleeper's own precomputed pts_* keys must never leak into the total.
    assert score_stats(line, ppr) < 100


def test_scenario_axis_is_rescaled_per_league_so_thresholds_stay_honest():
    """A player projected 20 in a TE-premium league and 14 in a standard league
    must not be swept on a single shared point scale."""
    kelce = player("Travis Kelce", "TE", "KC")
    prem = make_league("TE Premium", 50, scoring=TE_PREM)
    std = make_league("Standard", 50, scoring=HALF)
    s1 = make_state(prem, my_players=[(kelce, 20.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                    opp_players=filler("t", 9, 14))
    s2 = make_state(std, my_players=[(kelce, 14.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                    opp_players=filler("t", 9, 14))
    scenarios = [(s, s.my_starters[0], compute_leverage(s)) for s in (s1, s2)]
    curve = build_curve(kelce, scenarios)

    assert curve.reference_projection == pytest.approx(17.0, abs=0.01)
    prem_scale = next(l.scale for l in curve.leagues if l.league.name == "TE Premium")
    std_scale = next(l.scale for l in curve.leagues if l.league.name == "Standard")
    assert prem_scale > 1.0 > std_scale
    assert prem_scale == pytest.approx(20 / 17, abs=0.01)
    assert std_scale == pytest.approx(14 / 17, abs=0.01)


def test_thresholds_are_reported_in_each_leagues_own_points():
    """Same player, two formats: the two numbers differ and each is labelled."""
    kelce = player("Travis Kelce", "TE", "KC")
    prem = make_league("TE Premium", 50, scoring=TE_PREM)
    std = make_league("Half PPR", 50, scoring=HALF)
    s1 = make_state(prem, my_players=[(kelce, 20.0, 0, GS.NOT_STARTED)] + finished("m", 8, 13),
                    opp_players=finished("t", 9, 14))
    s2 = make_state(std, my_players=[(kelce, 14.0, 0, GS.NOT_STARTED)] + finished("m", 8, 13),
                    opp_players=finished("t", 9, 14))
    scenarios = [(s, s.my_starters[0], compute_leverage(s)) for s in (s1, s2)]
    r = analyze_player(build_curve(kelce, scenarios))
    descriptions = [l.threshold.describe() for l in r.lines]
    assert any("TE+1" in d for d in descriptions)
    assert any("Half-PPR" in d for d in descriptions)


def test_sweet_spot_is_quoted_in_the_dominant_leagues_points_and_named():
    """A range spanning leagues with different scoring must say which league's
    points it is quoting, and must agree with that league's own threshold."""
    from app.models import PlayerGameState as GS2

    x = player("Conflicted Guy", "TE", "KC")
    prem = make_league("TE Premium", 50, scoring=TE_PREM)     # he projects 22 here
    std = make_league("Standard", 20, scoring=HALF)           # ...and 14 here
    own = make_state(prem,
                     my_players=[(x, 22.0, 0, GS2.NOT_STARTED)] + finished("m", 8, 12.0),
                     opp_players=finished("t", 9, 12.4))
    face = make_state(std,
                      my_players=finished("m", 9, 14.0),
                      opp_players=[(x, 14.0, 0, GS2.NOT_STARTED)] + finished("t", 8, 13.0))
    scenarios = []
    for st in (own, face):
        lev = compute_leverage(st)
        for e in st.all_starters():
            if e.canonical.key == x.key:
                scenarios.append((st, e, lev))
    r = analyze_player(build_curve(x, scenarios))

    assert r.mixed_scoring, "TE-premium vs half-PPR must be flagged as incomparable"
    phrase = r.range_phrase()
    assert phrase, "a conflicted player should get a range"
    assert "in the $" in phrase, f"the league must be named: {phrase!r}"
    dom = r.dominant_line
    number = float(phrase.split()[0].rstrip("+").split("-")[0])
    assert number == pytest.approx(dom.threshold.value, abs=1.5), (phrase, dom.threshold.value)


def test_comparable_leagues_do_not_clutter_the_range_with_a_league_name():
    from app.models import PlayerGameState as GS2

    x = player("Simple Guy", "RB", "PHI")
    a = make_league("A", 100, scoring=PPR)
    b = make_league("B", 20, scoring=PPR)
    own = make_state(a, my_players=[(x, 16.0, 0, GS2.NOT_STARTED)] + finished("m", 8, 12.0),
                     opp_players=finished("t", 9, 12.4))
    face = make_state(b, my_players=finished("m", 9, 14.0),
                      opp_players=[(x, 16.0, 0, GS2.NOT_STARTED)] + finished("t", 8, 13.0))
    scenarios = []
    for st in (own, face):
        lev = compute_leverage(st)
        for e in st.all_starters():
            if e.canonical.key == x.key:
                scenarios.append((st, e, lev))
    r = analyze_player(build_curve(x, scenarios))
    assert not r.mixed_scoring
    assert "in the $" not in r.range_phrase()
