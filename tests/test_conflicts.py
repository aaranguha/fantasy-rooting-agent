"""The two cases the spec calls out as VERY IMPORTANT (section 26), plus the rest
of the conflicted-player logic.  A conflicted player must never be waved away as
'neutral' — the engine has to weigh stakes AND leverage."""

from __future__ import annotations

import pytest

from app.leverage import compute_leverage
from app.models import PlayerGameState as GS, RootingCategory, Side
from app.rooting import analyze_player
from app.scenarios import build_curve

from .factories import PPR, filler, finished, make_league, make_state, player


def curve_for(target, states_and_sides):
    """Build the cross-league curve for `target` given (state, side) pairs."""
    scenarios = []
    for state in states_and_sides:
        lev = compute_leverage(state)
        for exp in state.all_starters():
            if exp.canonical.key == target.key:
                scenarios.append((state, exp, lev))
    return build_curve(target, scenarios)


# ---------------------------------------------------------------------------
# VERY IMPORTANT TEST #1
# ---------------------------------------------------------------------------

def test_own_in_big_league_needing_18_beats_facing_in_small_league_allowing_27():
    """OURS $100 (need ~18) vs AGAINST $20 (can tolerate ~27) => ROOT FOR, with a range."""
    x = player("Player X", "RB", "PHI")

    # $100 league: we own him, and we need roughly 18 from him.
    big = make_league("Big Money", 100)
    big_state = make_state(
        big,
        my_players=[(x, 15.0, 0.0, GS.NOT_STARTED)] + finished("mine", 8, 12.0),
        opp_players=finished("theirs", 9, 12.6),
    )

    # $20 league: we face him, and can tolerate roughly 27.
    small = make_league("Small Money", 20)
    small_state = make_state(
        small,
        my_players=finished("mine", 9, 14.0),
        opp_players=[(x, 15.0, 0.0, GS.NOT_STARTED)] + finished("theirs", 8, 12.375),
    )

    from app.thresholds import compute_threshold
    need = compute_threshold(big_state, big_state.my_starters[0])
    afford = compute_threshold(small_state, small_state.opp_starters[0])
    assert need.side == Side.MINE and 17 <= need.value <= 19, need.value
    assert afford.side == Side.OPPONENT and 26 <= afford.value <= 28, afford.value

    result = analyze_player(curve_for(x, [big_state, small_state]))

    assert result.is_conflicted
    assert result.category.is_positive, f"expected a positive verdict, got {result.category}"
    assert result.category is not RootingCategory.NEUTRAL
    assert result.score > 10
    lo, hi = result.sweet_spot
    assert lo == pytest.approx(need.value, abs=2.0), (lo, need.value)
    assert hi == pytest.approx(afford.value, abs=2.5), (hi, afford.value)
    assert "root" in result.narrative().lower()


# ---------------------------------------------------------------------------
# VERY IMPORTANT TEST #2
# ---------------------------------------------------------------------------

def test_owning_him_in_a_won_20_dollar_league_loses_to_a_close_100_dollar_league():
    """OURS $20 but projected +40, AGAINST $100 and close (tolerate ~16)
    => ROOT AGAINST despite technically owning him."""
    x = player("Player X", "WR", "DAL")

    small = make_league("Casual Twenty", 20)
    small_state = make_state(
        small,
        my_players=[(x, 14.0, 0.0, GS.NOT_STARTED)] + finished("mine", 8, 15.0),
        opp_players=finished("theirs", 9, 9.33),      # we're up ~40 already
    )

    big = make_league("Hundred Dollar", 100)
    big_state = make_state(
        big,
        my_players=finished("mine", 9, 13.0),
        opp_players=[(x, 14.0, 0.0, GS.NOT_STARTED)] + finished("theirs", 8, 12.625),
    )

    from app.thresholds import compute_threshold
    afford = compute_threshold(big_state, big_state.opp_starters[0])
    assert 15 <= afford.value <= 17, afford.value

    result = analyze_player(curve_for(x, [small_state, big_state]))

    assert result.is_conflicted
    assert result.category.is_negative, f"expected ROOT AGAINST, got {result.category}"
    assert result.score < -10
    assert "quiet" in result.narrative().lower() or "against" in result.narrative().lower()


# ---------------------------------------------------------------------------
# Supporting conflict cases
# ---------------------------------------------------------------------------

def test_conflicted_is_never_silently_neutral_when_stakes_differ():
    """Same raw exposure (1 own, 1 face) but very different money => not neutral."""
    x = player("Bijan Robinson", "RB", "ATL")
    own = make_league("Dynasty", 150)
    face = make_league("Family", 10)
    own_state = make_state(own,
                           my_players=[(x, 16.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                           opp_players=filler("t", 9, 13.5))
    face_state = make_state(face,
                            my_players=filler("m", 9, 13.5),
                            opp_players=[(x, 16.0, 0, GS.NOT_STARTED)] + filler("t", 8, 13))
    r = analyze_player(curve_for(x, [own_state, face_state]))
    assert r.category is not RootingCategory.NEUTRAL
    assert r.category.is_positive


def test_high_buyin_blowout_loses_to_low_buyin_nail_biter():
    """A $100 league up 60 has little leverage; a $25 coin flip has a lot."""
    x = player("Achane", "RB", "MIA")
    blowout = make_league("Rich Blowout", 100)
    close = make_league("Cheap Thriller", 25)
    blow_state = make_state(blowout,
                            my_players=finished("m", 9, 20.0),
                            opp_players=[(x, 15.0, 0, GS.NOT_STARTED)] + finished("t", 8, 15.0))
    close_state = make_state(close,
                             my_players=[(x, 15.0, 0, GS.NOT_STARTED)] + finished("m", 8, 13.0),
                             opp_players=finished("t", 9, 13.44))
    r = analyze_player(curve_for(x, [blow_state, close_state]))
    # We face him in the expensive league, but it's already won, so the cheap
    # close league we own him in should carry the day.
    assert r.category.is_positive, r.category
    blow_line = next(l for l in r.lines if l.league.buy_in_usd == 100)
    close_line = next(l for l in r.lines if l.league.buy_in_usd == 25)
    assert blow_line.dollar_swing < close_line.dollar_swing


def test_same_player_ours_in_two_leagues_is_a_clean_root_for():
    x = player("A.J. Brown", "WR", "PHI")
    states = []
    for name, money in (("Dynasty", 100), ("Work", 35)):
        lg = make_league(name, money)
        states.append(make_state(lg,
                                 my_players=[(x, 16.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                                 opp_players=filler("t", 9, 14.5)))
    r = analyze_player(curve_for(x, states))
    assert not r.is_conflicted
    assert r.category.is_positive and r.score > 15
    assert r.money_at_stake == 135


def test_same_player_against_us_in_three_leagues_is_a_clean_root_against():
    x = player("CeeDee Lamb", "WR", "DAL")
    states = []
    for name, money in (("Dynasty", 100), ("Work", 35), ("Family", 20)):
        lg = make_league(name, money)
        states.append(make_state(lg,
                                 my_players=filler("m", 9, 14.0),
                                 opp_players=[(x, 17.0, 0, GS.NOT_STARTED)] + filler("t", 8, 13)))
    r = analyze_player(curve_for(x, states))
    assert r.category.is_negative and r.score < -15
    assert r.money_at_stake == 155


def test_public_enemy_is_awarded_to_the_single_worst_player():
    from app.rooting import mark_public_enemy

    villain = player("Josh Allen", "QB", "BUF")
    friend = player("Achane", "RB", "MIA")
    lg = make_league("Dynasty", 100)
    v_state = make_state(lg, my_players=filler("m", 9, 14.0),
                         opp_players=[(villain, 22.0, 0, GS.NOT_STARTED)] + filler("t", 8, 13))
    f_state = make_state(make_league("Work", 35),
                         my_players=[(friend, 14.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                         opp_players=filler("t", 9, 14.0))
    players = [analyze_player(curve_for(villain, [v_state])),
               analyze_player(curve_for(friend, [f_state]))]
    mark_public_enemy(players)
    assert players[0].public_enemy and players[0].category is RootingCategory.PUBLIC_ENEMY
    assert not players[1].public_enemy
    assert "public enemy" in players[0].narrative().lower()
