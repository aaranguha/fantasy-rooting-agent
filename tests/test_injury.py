"""Injury / availability layer:

  * providers stamp a normalized injury code onto every exposure,
  * a player ruled out while still in one of my lineups is a loud alert,
  * a near-decided league still contributes a name-level lean (not a full block),
  * a mid-game "ruled out" is its own live event.
"""

from __future__ import annotations

import pytest

from app.analysis import Analyzer
from app.config import AppConfig, LeagueConfig
from app.formatting import long_report, phone_message
from app.models import PlayerGameState as GS
from app.models import normalize_injury

from .factories import finished, make_game, make_league, make_state, player
from .test_providers import (
    ESPN_META, FakeHttp, SLEEPER_LEAGUE, SLEEPER_MATCHUPS, SLEEPER_ROSTERS,
    SLEEPER_USERS, registry,  # noqa: F401 - registry is a fixture
)
from app.providers.espn import ESPNProvider
from app.providers.sleeper import SleeperProvider


class Ctx:
    def __init__(self, states, games):
        self.ok_states = self.states = states
        self.games = games
        self.season, self.week = 2026, 5
        self.errors = []


def analyzer():
    return Analyzer(AppConfig(season=2026), registry=None, nfl=object())


# -- normalization -----------------------------------------------------------

@pytest.mark.parametrize("raw,code", [
    ("ACTIVE", ""), (None, ""), ("Questionable", "Q"), ("DOUBTFUL", "DOUBT"),
    ("OUT", "OUT"), ("O", "OUT"), ("INJURY_RESERVE", "IR"), ("IR-R", "IR"),
    ("Suspension", "SUSP"), ("Day_To_Day", "Q"),
])
def test_normalize_injury(raw, code):
    assert normalize_injury(raw) == code


# -- providers -------------------------------------------------------------

def test_sleeper_stamps_injury_status_onto_exposures(registry):
    http = FakeHttp({
        "/league/111/rosters": SLEEPER_ROSTERS,
        "/league/111/users": SLEEPER_USERS,
        "/league/111/matchups/5": SLEEPER_MATCHUPS,
        "/league/111": SLEEPER_LEAGUE,
        "/projections/nfl": [],
        "/players/nfl": {"5859": {"injury_status": "Out"}, "4866": {"injury_status": None}},
    })
    sp = SleeperProvider(registry, 2026, client=http)
    st = sp.load_matchup(LeagueConfig(platform="sleeper", league_id="111",
                                      my_team_id="1", season=2026), 5)
    ajb = next(e for e in st.my_starters if e.canonical.name == "A.J. Brown")
    saquon = next(e for e in st.my_starters if e.canonical.name == "Saquon Barkley")
    assert ajb.injury_status == "OUT" and ajb.is_sidelined
    assert saquon.injury_status == "" and not saquon.is_sidelined


def test_sleeper_injury_feed_outage_is_survived(registry):
    http = FakeHttp({
        "/league/111/rosters": SLEEPER_ROSTERS,
        "/league/111/users": SLEEPER_USERS,
        "/league/111/matchups/5": SLEEPER_MATCHUPS,
        "/league/111": SLEEPER_LEAGUE,
        "/projections/nfl": [],
    })  # no /players/nfl route -> ProviderError, swallowed
    sp = SleeperProvider(registry, 2026, client=http)
    st = sp.load_matchup(LeagueConfig(platform="sleeper", league_id="111",
                                      my_team_id="1", season=2026), 5)
    assert st.my_starters and all(e.injury_status == "" for e in st.all_starters())


def test_espn_reads_injury_status_from_the_player_entry(registry):
    schedule = {"schedule": [{"id": 1, "matchupPeriodId": 5,
        "home": {"teamId": 6, "rosterForCurrentScoringPeriod": {"entries": [
            {"lineupSlotId": 2, "playerId": 3929630, "playerPoolEntry": {"player": {
                "id": 3929630, "fullName": "Saquon Barkley", "defaultPositionId": 2,
                "proTeamId": 21, "injuryStatus": "OUT", "stats": []}}}]}},
        "away": {"teamId": 1, "rosterForCurrentScoringPeriod": {"entries": []}}}]}
    http = FakeHttp({"/leagues/222": {**ESPN_META, **schedule}})
    ep = ESPNProvider(registry, 2026, client=http, cookies={"swid": "{X}", "espn_s2": "y"})
    st = ep.load_matchup(LeagueConfig(platform="espn", league_id="222",
                                      my_team_id="6", season=2026), 5)
    saquon = next(e for e in st.my_starters if e.canonical.name == "Saquon Barkley")
    assert saquon.injury_status == "OUT"


# -- rendering: lineup alert + near-decided lean ----------------------------

def _blowout_state(target, injury=""):
    """A league we've all but won: target is our only unresolved starter."""
    lg = make_league("Dynasties and Dystopia", 40)
    my = [(target, 12.0, 0.0, GS.NOT_STARTED, injury)] + finished("m", 8, 22)
    opp = finished("t", 9, 6)
    return make_state(lg, my_players=my, opp_players=opp)


def test_near_decided_league_still_produces_a_lean_in_the_push():
    target = player("Jadarian Price", "RB", "SEA")
    st = _blowout_state(target)
    game = make_game("NE", "SEA")
    guide = analyzer().analyze_game(Ctx([st], [game]), game)

    assert guide.foreground_relevant == []
    assert [p.player.name for p in guide.footnote_relevant] == ["Jadarian Price"]
    msg = phone_message(guide)
    assert "near-decided" in msg and "Jadarian Price" in msg


def test_player_ruled_out_in_my_lineup_is_a_loud_alert():
    target = player("A.J. Brown", "WR", "PHI")
    st = _blowout_state(target, injury="OUT")
    game = make_game("DAL", "PHI")
    guide = analyzer().analyze_game(Ctx([st], [game]), game)

    assert [p.player.name for p in guide.lineup_alerts] == ["A.J. Brown"]
    msg = phone_message(guide)
    assert msg.startswith("⚠️ LINEUP")
    assert "A.J. Brown" in msg and "OUT" in msg
    assert "LINEUP (bench now)" in long_report(guide)
