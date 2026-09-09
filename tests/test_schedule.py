"""Primetime detection from real scoreboard shapes (spec section 17).
Nothing is hardcoded per week - slots come from kickoff time, broadcast and
whether the game stands alone in its window."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import NFLGame, PlayerGameState, SlotType
from app.providers.nfl import classify_slots, primetime_games, team_game_index


def g(gid, away, home, iso, broadcast, **kw):
    return NFLGame(id=gid, away=away, home=home,
                   kickoff=datetime.fromisoformat(iso).astimezone(timezone.utc),
                   broadcast=broadcast, week=1, season=2026, **kw)


def sunday_slate():
    """Nine 1pm ET regional games + a 4:25 doubleheader, as a real week looks."""
    out = [g(f"e{i}", "AAA", f"B{i:02d}", "2026-09-13T13:00:00-04:00", "FOX")
           for i in range(9)]
    out += [g(f"l{i}", "CCC", f"D{i:02d}", "2026-09-13T16:25:00-04:00", "CBS")
            for i in range(4)]
    return out


def test_tnf_snf_and_mnf_are_detected():
    games = sunday_slate() + [
        g("tnf", "KC", "BAL", "2026-09-10T20:15:00-04:00", "Prime Video"),
        g("snf", "DAL", "NYG", "2026-09-13T20:20:00-04:00", "NBC"),
        g("mnf", "DEN", "KC", "2026-09-14T20:15:00-04:00", "ESPN/ABC"),
    ]
    classify_slots(games)
    slots = {x.id: x.slot for x in games}
    assert slots["tnf"] is SlotType.TNF
    assert slots["snf"] is SlotType.SNF
    assert slots["mnf"] is SlotType.MNF
    assert all(slots[x.id] is SlotType.REGULAR for x in sunday_slate())


def test_regional_sunday_afternoon_games_are_never_primetime():
    games = sunday_slate()
    classify_slots(games)
    assert primetime_games(games) == []


def test_double_monday_night_produces_two_mnf_games():
    games = sunday_slate() + [
        g("mnf1", "BUF", "MIA", "2026-09-14T19:15:00-04:00", "ESPN"),
        g("mnf2", "SEA", "LAR", "2026-09-14T20:30:00-04:00", "ABC"),
    ]
    classify_slots(games)
    mnf = [x for x in games if x.slot is SlotType.MNF]
    assert len(mnf) == 2
    assert {x.id for x in mnf} == {"mnf1", "mnf2"}


def test_wednesday_season_opener_is_primetime_not_regular():
    """2026 really does open on a Wednesday - the rule must not assume Thursday."""
    games = [g("open", "NE", "SEA", "2026-09-09T20:20:00-04:00", "NBC")]
    classify_slots(games)
    assert games[0].slot is SlotType.PRIMETIME
    assert games[0].is_primetime


def test_netflix_christmas_game_is_caught():
    games = [g("xmas", "KC", "PIT", "2026-12-25T13:00:00-05:00", "Netflix")]
    classify_slots(games)
    assert games[0].slot is SlotType.HOLIDAY


def test_london_morning_standalone_is_caught():
    games = [g("lon", "JAX", "NYJ", "2026-10-11T09:30:00-04:00", "NFLN")]
    classify_slots(games)
    assert games[0].slot is SlotType.INTERNATIONAL


def test_flexed_game_reclassifies_purely_from_its_new_kickoff():
    """The same matchup moved from 1pm to SNF becomes SNF with no code change."""
    early = sunday_slate() + [g("flex", "SF", "SEA", "2026-09-13T13:00:00-04:00", "FOX")]
    classify_slots(early)
    assert early[-1].slot is SlotType.REGULAR

    flexed = sunday_slate() + [g("flex", "SF", "SEA", "2026-09-13T20:20:00-04:00", "NBC")]
    classify_slots(flexed)
    assert flexed[-1].slot is SlotType.SNF


def test_include_slots_filter_respects_configuration():
    games = [
        g("tnf", "KC", "BAL", "2026-09-10T20:15:00-04:00", "Prime Video"),
        g("mnf", "DEN", "KC", "2026-09-14T20:15:00-04:00", "ESPN"),
    ]
    classify_slots(games)
    assert len(primetime_games(games, ["MNF"])) == 1
    assert primetime_games(games, ["MNF"])[0].id == "mnf"


def test_fraction_remaining_tracks_live_game_progress():
    pre = g("a", "A", "B", "2026-09-13T20:20:00-04:00", "NBC")
    assert pre.fraction_remaining == 1.0

    half = g("b", "A", "B", "2026-09-13T20:20:00-04:00", "NBC",
             state=PlayerGameState.IN_PROGRESS, period=3, clock="15:00")
    assert half.fraction_remaining == pytest.approx(0.5, abs=0.01)

    done = g("c", "A", "B", "2026-09-13T20:20:00-04:00", "NBC",
             state=PlayerGameState.FINAL, period=4)
    assert done.fraction_remaining == 0.0


def test_team_game_index_maps_both_teams():
    games = [g("x", "BUF", "MIA", "2026-09-14T20:15:00-04:00", "ESPN")]
    idx = team_game_index(games)
    assert idx["BUF"] is idx["MIA"] is games[0]
