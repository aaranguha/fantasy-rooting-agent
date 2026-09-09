"""Dynamic per-week league importance driven by the standings.

Rules being locked down:
  * A good record with a live title shot makes a league matter MORE.
  * Elimination makes it matter LESS.
  * In a league with a loser punishment, sliding toward last makes it matter
    more - and increasingly so as the regular season runs out.
"""

from __future__ import annotations

import pytest

from app.models import League, Platform
from app.standings import (
    LeagueStandings, SeasonOutlook, TeamRecord, apply_season_weight,
    compute_outlook, season_multiplier,
)


def league_of(records, *, my_id="1", playoff_teams=6, weeks=14, week=1, punished=1):
    """records = [(wins, losses, points_for), ...] - team ids are 1..N."""
    teams = [TeamRecord(team_id=str(i), name=f"Team {i}", wins=w, losses=l, points_for=pf)
             for i, (w, l, pf) in enumerate(records, 1)]
    return LeagueStandings(teams=teams, my_team_id=my_id, playoff_teams=playoff_teams,
                           regular_season_weeks=weeks, current_week=week,
                           punished_places=punished)


def ten_team(my_record, others_record, *, my_pf=None, other_pf=None, **kw):
    """Me plus nine clones, so only my record distinguishes me."""
    w, l = my_record
    ow, ol = others_record
    games = w + l
    my_points = my_pf if my_pf is not None else games * (130 if w > l else 95)
    other_points = other_pf if other_pf is not None else games * 112
    records = [(w, l, my_points)] + [(ow, ol, other_points) for _ in range(9)]
    return league_of(records, week=games + 1, **kw)


# ---------------------------------------------------------------------------
# Odds model
# ---------------------------------------------------------------------------

def test_week_one_has_no_opinion():
    st = league_of([(0, 0, 0.0)] * 10, playoff_teams=6, week=1)
    o = compute_outlook(st)
    assert o.available
    assert 0.3 <= o.playoff_odds <= 0.7, "with no games played nobody is favoured"
    assert season_multiplier(o).multiplier == pytest.approx(1.0, abs=0.35)


def test_a_strong_record_produces_high_playoff_odds():
    o = compute_outlook(ten_team((7, 2), (4, 5)))
    assert o.playoff_odds > 0.85
    assert o.record == "7-2"
    assert o.seed == 1


def test_a_terrible_record_late_is_effectively_eliminated():
    o = compute_outlook(ten_team((1, 11), (7, 5), weeks=14))
    assert o.playoff_odds < 0.05
    assert o.eliminated


def test_last_place_odds_rise_as_the_record_collapses():
    good = compute_outlook(ten_team((8, 2), (5, 5)))
    bad = compute_outlook(ten_team((1, 9), (5, 5)))
    assert bad.last_place_odds > 0.5 > good.last_place_odds
    assert good.last_place_odds < 0.05


def test_urgency_climbs_through_the_regular_season():
    early = league_of([(1, 1, 200.0)] * 10, weeks=14, week=2)
    late = league_of([(1, 1, 200.0)] * 10, weeks=14, week=14)
    assert early.urgency < 0.2 < late.urgency


def test_missing_standings_degrade_to_a_neutral_multiplier():
    st = LeagueStandings(error="ESPN standings unavailable: 500")
    o = compute_outlook(st)
    assert not o.available
    w = season_multiplier(o)
    assert w.multiplier == 1.0 and "static weight" in w.reason


# ---------------------------------------------------------------------------
# The multiplier - the behaviours actually requested
# ---------------------------------------------------------------------------

def test_a_good_record_makes_a_league_matter_more():
    contender = season_multiplier(compute_outlook(ten_team((7, 2), (4, 5))))
    assert contender.multiplier > 1.2
    assert "contend" in contender.reason or "clinched" in contender.reason


def test_being_out_of_it_makes_a_league_matter_less():
    dead = season_multiplier(compute_outlook(ten_team((1, 11), (7, 5))))
    assert dead.multiplier < 0.5
    assert "eliminated" in dead.reason


def test_contender_outweighs_a_dead_team_by_a_wide_margin():
    good = season_multiplier(compute_outlook(ten_team((8, 2), (5, 5)))).multiplier
    dead = season_multiplier(compute_outlook(ten_team((1, 11), (7, 5)))).multiplier
    assert good / dead > 3.0


def test_a_100_dollar_dead_league_drops_below_a_20_dollar_contender():
    """The whole point: money alone should not decide where your attention goes."""
    big = League(id="a", name="Dynasty", platform=Platform.ESPN, buy_in_usd=100)
    small = League(id="b", name="Family", platform=Platform.SLEEPER, buy_in_usd=20)

    apply_season_weight(big, ten_team((2, 9), (6, 5)))          # dead
    apply_season_weight(small, ten_team((8, 3), (5, 6)))        # contending

    assert big.static_weight == 100 and small.static_weight == 20
    assert big.effective_weight < 40
    assert small.effective_weight > 25
    assert small.effective_weight > big.effective_weight * 0.7


# ---------------------------------------------------------------------------
# The punishment league
# ---------------------------------------------------------------------------

def test_punishment_league_importance_climbs_as_the_season_ends():
    """Same bad record, different point in the season: late must matter more."""
    early = ten_team((1, 4), (3, 2), weeks=14)                  # week 6
    late = league_of([(2, 10, 1100.0)] + [(7, 5, 1400.0)] * 9,
                     weeks=14, week=13)

    e = season_multiplier(compute_outlook(early), loser_punishment=1.0)
    l = season_multiplier(compute_outlook(late), loser_punishment=1.0)
    assert l.multiplier > e.multiplier
    assert "PUNISHMENT WATCH" in l.reason
    assert "Do not lose this" in l.reason


def test_punishment_beats_elimination_in_the_same_league():
    """Eliminated from the playoffs AND sliding toward last: the league gets MORE
    important, not less - because now you're playing to avoid the punishment."""
    st = league_of([(2, 10, 1050.0)] + [(7, 5, 1400.0)] * 9, weeks=14, week=13)
    o = compute_outlook(st)
    assert o.eliminated

    without = season_multiplier(o, loser_punishment=0.0)
    with_punish = season_multiplier(o, loser_punishment=1.0)

    assert without.multiplier < 0.5, "no punishment: a dead league is a dead league"
    assert with_punish.multiplier > 1.0, "punishment: suddenly this is urgent"
    assert with_punish.downside > with_punish.upside


def test_punishment_does_not_inflate_a_league_you_are_winning():
    st = league_of([(9, 3, 1500.0)] + [(5, 7, 1250.0)] * 9, weeks=14, week=13)
    w = season_multiplier(compute_outlook(st), loser_punishment=1.5)
    assert w.downside < 0.1
    assert w.multiplier == pytest.approx(w.upside, abs=0.01)
    assert "PUNISHMENT" not in w.reason


def test_dread_level_scales_the_punishment_response():
    st = league_of([(2, 10, 1050.0)] + [(7, 5, 1400.0)] * 9, weeks=14, week=13)
    o = compute_outlook(st)
    mild = season_multiplier(o, loser_punishment=0.5).multiplier
    normal = season_multiplier(o, loser_punishment=1.0).multiplier
    brutal = season_multiplier(o, loser_punishment=1.5).multiplier
    assert mild < normal < brutal


def test_multiplier_stays_inside_its_documented_bounds():
    hopeless = league_of([(0, 13, 800.0)] + [(9, 4, 1600.0)] * 9, weeks=14, week=14)
    o = compute_outlook(hopeless)
    assert 0.25 <= season_multiplier(o, loser_punishment=3.0).multiplier <= 2.50
    assert 0.25 <= season_multiplier(o).multiplier <= 2.50


def test_dynamic_importance_can_be_switched_off():
    o = compute_outlook(ten_team((1, 11), (7, 5)))
    w = season_multiplier(o, enabled=False)
    assert w.multiplier == 1.0 and "off" in w.reason


# ---------------------------------------------------------------------------
# Integration with the weighting used by the rooting engine
# ---------------------------------------------------------------------------

def test_effective_weight_is_the_transparent_product_of_three_numbers():
    lg = League(id="x", name="Work", platform=Platform.SLEEPER,
                buy_in_usd=50, importance_multiplier=1.5)
    assert lg.static_weight == 75
    lg.season_multiplier = 1.4
    assert lg.effective_weight == 105
    lg.season_multiplier = 0.3
    assert lg.effective_weight == 22.5


def test_free_league_still_scales_with_season_context():
    lg = League(id="x", name="Free", platform=Platform.SLEEPER, buy_in_usd=0)
    apply_season_weight(lg, ten_team((8, 2), (5, 5)))
    assert lg.static_weight == 10
    assert lg.effective_weight > 10


def test_apply_season_weight_records_a_human_readable_reason():
    lg = League(id="x", name="Punish", platform=Platform.ESPN, buy_in_usd=50)
    st = league_of([(2, 10, 1050.0)] + [(7, 5, 1400.0)] * 9, weeks=14, week=13)
    w = apply_season_weight(lg, st, loser_punishment=1.0)
    assert lg.season_note == w.reason
    assert lg.outlook is not None and lg.outlook.record == "2-10"
    assert "%" in lg.season_note
