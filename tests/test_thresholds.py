"""Points-needed / points-affordable maths and honest confidence grading
(spec sections 5, 6, 7)."""

from __future__ import annotations

import pytest

from app.models import Confidence, PlayerGameState as GS, Side
from app.thresholds import compute_threshold

from .factories import filler, finished, make_league, make_state, player


def test_points_needed_matches_the_worked_example_from_the_spec():
    """Opponent projected 126.4, our other starters 109.2 => need ~17.2."""
    ajb = player("A.J. Brown", "WR", "PHI")
    lg = make_league("Dynasty", 100)
    st = make_state(
        lg,
        my_players=[(ajb, 16.0, 0.0, GS.NOT_STARTED)] + finished("m", 8, 13.65),  # 109.2
        opp_players=finished("t", 8, 15.8),                                        # 126.4
    )
    assert st.projected_final_opponent == pytest.approx(126.4, abs=0.01)
    t = compute_threshold(st, st.my_starters[0])
    assert t.is_need
    assert t.value == pytest.approx(17.2, abs=0.05)


def test_points_we_can_afford_matches_the_worked_example():
    """Our projected 134.5, their others 109.3 => can afford ~25.2."""
    allen = player("Josh Allen", "QB", "BUF")
    lg = make_league("Dynasty", 100)
    st = make_state(
        lg,
        my_players=finished("m", 10, 13.45),                                      # 134.5
        opp_players=[(allen, 22.0, 0.0, GS.NOT_STARTED)] + finished("t", 10, 10.93),  # 109.3
    )
    t = compute_threshold(st, st.opp_starters[0])
    assert not t.is_need
    assert t.value == pytest.approx(25.2, abs=0.05)


def test_mnf_last_player_standing_is_an_exact_threshold():
    """Everyone else final => EXACT, stated to the hundredth."""
    ajb = player("A.J. Brown", "WR", "PHI")
    lg = make_league("Dynasty", 100)
    st = make_state(
        lg,
        my_players=[(ajb, 15.0, 0.0, GS.NOT_STARTED)] + finished("m", 8, 12.0),
        opp_players=finished("t", 9, 12.86),
    )
    t = compute_threshold(st, st.my_starters[0])
    assert t.confidence is Confidence.EXACT
    assert t.is_exact
    assert "need" in t.describe().lower() and f"{t.value:.2f}" in t.describe()


def test_thursday_night_threshold_is_projection_based_not_exact():
    """Nothing has been played yet => low confidence, and the wording says so."""
    saquon = player("Saquon Barkley", "RB", "PHI")
    lg = make_league("Dynasty", 100)
    st = make_state(
        lg,
        my_players=[(saquon, 18.0, 0.0, GS.NOT_STARTED)] + filler("m", 8, 13.0),
        opp_players=filler("t", 9, 14.0),
    )
    t = compute_threshold(st, st.my_starters[0])
    assert t.confidence is Confidence.LOW
    assert t.qualifier == "very roughly"
    assert "confidence" in t.describe().lower()


def test_confidence_climbs_as_the_week_resolves():
    p = player("Target", "RB", "PHI")
    lg = make_league("L", 50)
    seen = []
    for done in (0, 5, 8, 9):
        st = make_state(
            lg,
            my_players=[(p, 15.0, 0.0, GS.NOT_STARTED)]
            + finished("m", done, 13.0) + filler("m2", 9 - done, 13.0),
            opp_players=finished("t", 9, 13.0),
        )
        seen.append(compute_threshold(st, st.my_starters[0]).confidence)
    order = [Confidence.LOW, Confidence.PROJECTION, Confidence.HIGH, Confidence.EXACT]
    ranks = [order.index(c) for c in seen]
    assert ranks == sorted(ranks), seen
    assert seen[-1] is Confidence.EXACT


def test_threshold_reports_infeasible_when_we_are_already_winning_without_him():
    p = player("Target", "RB", "PHI")
    st = make_state(make_league("L", 50),
                    my_players=[(p, 15.0, 0, GS.NOT_STARTED)] + finished("m", 8, 25.0),
                    opp_players=finished("t", 9, 10.0))
    t = compute_threshold(st, st.my_starters[0])
    assert not t.feasible and t.value == 0.0
    assert "shut out" in t.impossible_reason


def test_threshold_reports_when_we_are_behind_even_with_him_shut_out():
    p = player("Villain", "WR", "DAL")
    st = make_state(make_league("L", 50),
                    my_players=finished("m", 9, 10.0),
                    opp_players=[(p, 15.0, 0, GS.NOT_STARTED)] + finished("t", 8, 25.0))
    t = compute_threshold(st, st.opp_starters[0])
    assert not t.feasible
    assert "behind" in t.impossible_reason and "help elsewhere" in t.impossible_reason


def test_threshold_says_we_can_survive_a_normal_game_when_the_bar_is_absurd():
    p = player("Villain", "WR", "DAL")
    st = make_state(make_league("L", 50),
                    my_players=finished("m", 9, 25.0),
                    opp_players=[(p, 15.0, 0, GS.NOT_STARTED)] + finished("t", 8, 10.0))
    t = compute_threshold(st, st.opp_starters[0])
    assert not t.feasible and "comfortably survive" in t.impossible_reason


def test_remaining_required_accounts_for_points_already_banked():
    p = player("Target", "RB", "PHI")
    st = make_state(make_league("L", 50),
                    my_players=[(p, 20.0, 8.0, GS.IN_PROGRESS)] + finished("m", 8, 12.0),
                    opp_players=finished("t", 9, 13.5))
    t = compute_threshold(st, st.my_starters[0])
    assert t.already_scored == 8.0
    assert t.remaining_required == pytest.approx(t.value - 8.0, abs=0.01)
