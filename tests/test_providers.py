"""Provider adapters against canned API payloads - no network in tests.

Covers the failure paths the spec asks for: ESPN auth failure and Sleeper
outage must degrade, never crash the run."""

from __future__ import annotations

import json

import pytest

from app.config import LeagueConfig
from app.models import LineupStatus, Platform, PlayerGameState, Side
from app.playerids import PlayerRegistry, espn_dst_team_id
from app.models import CanonicalPlayer
from app.providers.base import AuthError, HttpClient, ProviderError
from app.providers.espn import ESPNProvider, _entry_points
from app.providers.sleeper import SleeperProvider, score_stats


class FakeHttp:
    """Serves canned responses keyed by a substring of the URL."""

    def __init__(self, routes: dict, fail: dict | None = None):
        self.routes = routes
        self.fail = fail or {}
        self.calls: list[str] = []

    def get(self, url, params=None, headers=None, cache_ttl=0, **kw):
        self.calls.append(url)
        for needle, exc in self.fail.items():
            if needle in url:
                raise exc
        for needle, payload in self.routes.items():
            if needle in url:
                return payload
        raise ProviderError(f"no canned route for {url}")


@pytest.fixture()
def registry():
    reg = PlayerRegistry()
    reg.espn_team_abbrev = {21: "PHI", 6: "DAL"}
    reg.add(CanonicalPlayer(key="sleeper:4866", name="Saquon Barkley", position="RB",
                            nfl_team="PHI", sleeper_id="4866", espn_id=3929630))
    reg.add(CanonicalPlayer(key="sleeper:5859", name="A.J. Brown", position="WR",
                            nfl_team="PHI", sleeper_id="5859", espn_id=4047646))
    reg.add(CanonicalPlayer(key="sleeper:6786", name="CeeDee Lamb", position="WR",
                            nfl_team="DAL", sleeper_id="6786", espn_id=4241389))
    reg.add(CanonicalPlayer(key="dst:PHI", name="Philadelphia Eagles", position="DST",
                            nfl_team="PHI", sleeper_id="PHI"))
    return reg


# ---------------------------------------------------------------------------
# Sleeper
# ---------------------------------------------------------------------------

SLEEPER_LEAGUE = {
    "name": "The Dynasty", "league_id": "111",
    "scoring_settings": {"rec": 1.0, "rec_yd": 0.1, "rec_td": 6.0,
                         "rush_yd": 0.1, "rush_td": 6.0, "pass_td": 4.0},
}
SLEEPER_ROSTERS = [
    {"roster_id": 1, "owner_id": "me", "players": ["4866", "5859"]},
    {"roster_id": 2, "owner_id": "them", "players": ["6786"]},
]
SLEEPER_USERS = [
    {"user_id": "me", "display_name": "Me", "metadata": {"team_name": "My Squad"}},
    {"user_id": "them", "display_name": "Them", "metadata": {"team_name": "Their Squad"}},
]
SLEEPER_MATCHUPS = [
    {"roster_id": 1, "matchup_id": 3, "points": 40.5, "starters": ["4866", "5859"],
     "starters_points": [22.5, 18.0], "players": ["4866", "5859"],
     "players_points": {"4866": 22.5, "5859": 18.0}},
    {"roster_id": 2, "matchup_id": 3, "points": 12.0, "starters": ["6786"],
     "starters_points": [12.0], "players": ["6786"], "players_points": {"6786": 12.0}},
]
SLEEPER_PROJ = [
    {"player_id": "4866", "stats": {"rush_yd": 90.0, "rush_td": 0.8, "rec": 3.0, "rec_yd": 25.0}},
    {"player_id": "5859", "stats": {"rec": 6.0, "rec_yd": 85.0, "rec_td": 0.6}},
    {"player_id": "6786", "stats": {"rec": 7.0, "rec_yd": 95.0, "rec_td": 0.5}},
]


def sleeper_provider(registry, fail=None):
    http = FakeHttp({
        "/league/111/rosters": SLEEPER_ROSTERS,
        "/league/111/users": SLEEPER_USERS,
        "/league/111/matchups/5": SLEEPER_MATCHUPS,
        "/league/111": SLEEPER_LEAGUE,
        "/projections/nfl": SLEEPER_PROJ,
    }, fail=fail)
    return SleeperProvider(registry, 2026, client=http), http


def test_sleeper_matchup_is_normalized_into_our_model(registry):
    sp, _ = sleeper_provider(registry)
    cfg = LeagueConfig(platform="sleeper", league_id="111", name="", buy_in_usd=50,
                       my_team_id="1", season=2026)
    st = sp.load_matchup(cfg, 5)

    assert st.league.name == "The Dynasty"
    assert st.league.buy_in_usd == 50 and st.league.effective_weight == 50
    assert st.league.my_team_name == "My Squad"
    assert st.opponent_name == "Their Squad"
    assert st.current_score_mine == 40.5 and st.current_score_opponent == 12.0
    assert {e.canonical.name for e in st.my_starters} == {"Saquon Barkley", "A.J. Brown"}
    assert {e.canonical.name for e in st.opp_starters} == {"CeeDee Lamb"}
    assert all(e.side is Side.MINE for e in st.my_starters)
    assert all(e.lineup_status is LineupStatus.STARTER for e in st.all_starters())


def test_sleeper_projections_use_the_leagues_own_scoring(registry):
    sp, _ = sleeper_provider(registry)
    cfg = LeagueConfig(platform="sleeper", league_id="111", my_team_id="1", season=2026)
    st = sp.load_matchup(cfg, 5)
    saquon = next(e for e in st.my_starters if e.canonical.name == "Saquon Barkley")
    # 90*0.1 + 0.8*6 + 3*1 + 25*0.1 = 9 + 4.8 + 3 + 2.5
    assert saquon.projected_points == pytest.approx(19.3, abs=0.01)


def test_sleeper_scoring_settings_are_parsed_into_a_format_label():
    s = SleeperProvider.parse_scoring(SLEEPER_LEAGUE)
    assert s.ppr == 1.0 and s.format_name == "PPR"
    half = SleeperProvider.parse_scoring({"scoring_settings": {"rec": 0.5, "pass_td": 6}})
    assert half.format_name == "Half-PPR (6pt pass TD)"


def test_sleeper_bench_players_are_kept_out_of_the_starting_lineup(registry):
    rosters = [dict(SLEEPER_ROSTERS[0], players=["4866", "5859"]), SLEEPER_ROSTERS[1]]
    matchups = [dict(SLEEPER_MATCHUPS[0], starters=["4866"], starters_points=[22.5]),
                SLEEPER_MATCHUPS[1]]
    http = FakeHttp({"/league/111/rosters": rosters, "/league/111/users": SLEEPER_USERS,
                     "/league/111/matchups/5": matchups, "/league/111": SLEEPER_LEAGUE,
                     "/projections/nfl": SLEEPER_PROJ})
    sp = SleeperProvider(registry, 2026, client=http)
    st = sp.load_matchup(LeagueConfig(platform="sleeper", league_id="111",
                                      my_team_id="1", season=2026), 5)
    assert [e.canonical.name for e in st.my_starters] == ["Saquon Barkley"]
    assert [e.canonical.name for e in st.my_bench] == ["A.J. Brown"]


def test_sleeper_outage_degrades_instead_of_crashing(registry):
    sp, _ = sleeper_provider(registry, fail={"/league/111/rosters": ProviderError("503")})
    cfg = LeagueConfig(platform="sleeper", league_id="111", my_team_id="1", season=2026)
    with pytest.raises(ProviderError):
        sp.load_matchup(cfg, 5)   # the Analyzer catches this and marks the league failed


def test_sleeper_missing_projections_still_produces_a_usable_matchup(registry):
    sp, _ = sleeper_provider(registry, fail={"/projections/nfl": ProviderError("down")})
    cfg = LeagueConfig(platform="sleeper", league_id="111", my_team_id="1", season=2026)
    st = sp.load_matchup(cfg, 5)
    assert st.current_score_mine == 40.5
    assert all(e.projected_points == 0 for e in st.all_starters())
    assert st.tier.name == "GOOD"


def test_sleeper_bye_week_with_no_opponent_is_reported_not_crashed(registry):
    solo = [dict(SLEEPER_MATCHUPS[0], matchup_id=None)]
    http = FakeHttp({"/league/111/rosters": SLEEPER_ROSTERS, "/league/111/users": SLEEPER_USERS,
                     "/league/111/matchups/5": solo, "/league/111": SLEEPER_LEAGUE,
                     "/projections/nfl": SLEEPER_PROJ})
    st = SleeperProvider(registry, 2026, client=http).load_matchup(
        LeagueConfig(platform="sleeper", league_id="111", my_team_id="1", season=2026), 5)
    assert st.opponent_name == "BYE"
    assert st.my_starters and not st.opp_starters


# ---------------------------------------------------------------------------
# ESPN
# ---------------------------------------------------------------------------

def espn_player(pid, name, pos_id, team_id, actual, projected, week=5):
    return {
        "playerPoolEntry": {"player": {
            "id": pid, "fullName": name, "defaultPositionId": pos_id, "proTeamId": team_id,
            "stats": [
                {"scoringPeriodId": week, "statSourceId": 0, "statSplitTypeId": 1,
                 "appliedTotal": actual},
                {"scoringPeriodId": week, "statSourceId": 1, "statSplitTypeId": 1,
                 "appliedTotal": projected},
            ]}}}


ESPN_META = {
    "settings": {"name": "Money League",
                 "scoringSettings": {"scoringItems": [
                     {"statId": 53, "points": 0.5, "pointsOverrides": {"6": 1.5}},
                     {"statId": 4, "points": 6.0}]}},
    "teams": [{"id": 1, "name": "My Squad", "primaryOwner": "{ABC}", "owners": ["{ABC}"]},
              {"id": 2, "name": "Their Squad", "primaryOwner": "{XYZ}", "owners": ["{XYZ}"]}],
}
ESPN_MATCHUP = {
    "schedule": [{
        "matchupPeriodId": 5,
        "home": {"teamId": 1, "totalPoints": 55.5, "rosterForCurrentScoringPeriod": {"entries": [
            dict(espn_player(3929630, "Saquon Barkley", 2, 21, 22.5, 19.0), lineupSlotId=2),
            dict(espn_player(4047646, "A.J. Brown", 3, 21, 18.0, 16.0), lineupSlotId=4),
            dict(espn_player(9999, "Bench Guy", 3, 21, 30.0, 12.0), lineupSlotId=20),
            dict(espn_player(8888, "Hurt Guy", 2, 21, 0.0, 0.0), lineupSlotId=21),
        ]}},
        "away": {"teamId": 2, "totalPoints": 12.0, "rosterForCurrentScoringPeriod": {"entries": [
            dict(espn_player(4241389, "CeeDee Lamb", 3, 6, 12.0, 17.0), lineupSlotId=4),
        ]}},
    }],
}


def espn_provider(registry, fail=None):
    http = FakeHttp({"/leagues/222": {**ESPN_META, **ESPN_MATCHUP}}, fail=fail)
    return ESPNProvider(registry, 2026, client=http, cookies={"espn_s2": "x", "SWID": "{ABC}"})


def test_espn_matchup_is_normalized_and_bench_ir_excluded(registry):
    ep = espn_provider(registry)
    cfg = LeagueConfig(platform="espn", league_id="222", buy_in_usd=100,
                       my_team_id="1", season=2026)
    st = ep.load_matchup(cfg, 5)

    assert st.league.name == "Money League" and st.league.effective_weight == 100
    assert st.opponent_name == "Their Squad"
    assert {e.canonical.name for e in st.my_starters} == {"Saquon Barkley", "A.J. Brown"}
    assert {e.canonical.name for e in st.my_bench} == {"Bench Guy", "Hurt Guy"}
    assert st.current_score_mine == 55.5 and st.current_score_opponent == 12.0
    saquon = next(e for e in st.my_starters if "Saquon" in e.canonical.name)
    assert saquon.current_points == 22.5 and saquon.projected_points == 19.0


def test_espn_scoring_settings_detect_half_ppr_te_premium_and_6pt_tds(registry):
    s = espn_provider(registry).scoring("222")
    assert s.ppr == 0.5 and s.pass_td == 6.0 and s.te_premium == 1.0
    assert "Half-PPR" in s.format_name and "6pt pass TD" in s.format_name


def test_espn_identifies_my_team_from_the_swid_cookie(registry):
    assert espn_provider(registry).my_team_id("222") == "1"


def test_espn_auth_failure_is_reported_with_actionable_guidance(registry):
    ep = espn_provider(registry, fail={"/leagues/222": AuthError("401")})
    with pytest.raises(AuthError) as exc:
        ep.load_matchup(LeagueConfig(platform="espn", league_id="222",
                                     my_team_id="1", season=2026), 5)
    assert "ESPN_S2" in str(exc.value) and "private league" in str(exc.value)


def test_espn_entry_points_ignore_other_weeks():
    player = {"stats": [
        {"scoringPeriodId": 4, "statSourceId": 0, "statSplitTypeId": 1, "appliedTotal": 99},
        {"scoringPeriodId": 5, "statSourceId": 0, "statSplitTypeId": 1, "appliedTotal": 12},
        {"scoringPeriodId": 5, "statSourceId": 1, "statSplitTypeId": 1, "appliedTotal": 14},
    ]}
    assert _entry_points(player, 5) == (12.0, 14.0)


def test_espn_dst_ids_decode_to_a_team():
    assert espn_dst_team_id(-16021) == 21
    assert espn_dst_team_id(3929630) is None


def test_sleeper_slot_labels_come_from_the_leagues_roster_positions(registry):
    league = dict(SLEEPER_LEAGUE, roster_positions=["RB", "WR", "BN"])
    http = FakeHttp({"/league/111/rosters": SLEEPER_ROSTERS, "/league/111/users": SLEEPER_USERS,
                     "/league/111/matchups/5": SLEEPER_MATCHUPS, "/league/111": league,
                     "/projections/nfl": SLEEPER_PROJ})
    st = SleeperProvider(registry, 2026, client=http).load_matchup(
        LeagueConfig(platform="sleeper", league_id="111", my_team_id="1", season=2026), 5)
    slots = {e.canonical.name: e.slot for e in st.my_starters}
    assert slots["Saquon Barkley"] == "RB"      # starters[0]
    assert slots["A.J. Brown"] == "WR"          # starters[1]


def test_espn_multiweek_playoff_matchup_falls_back_to_the_latest_started_period(registry):
    """scoringPeriodId 16 with a matchupPeriodId 15 two-week final must resolve to
    period 15 - never to an arbitrary earlier week."""
    schedule = {"schedule": [
        {"matchupPeriodId": 1, "home": {"teamId": 1, "totalPoints": 1.0,
                                        "rosterForCurrentScoringPeriod": {"entries": []}},
         "away": {"teamId": 2, "totalPoints": 2.0,
                  "rosterForCurrentScoringPeriod": {"entries": []}}},
        {"matchupPeriodId": 15, "home": {"teamId": 1, "totalPoints": 88.0,
                                         "rosterForCurrentScoringPeriod": {"entries": []}},
         "away": {"teamId": 2, "totalPoints": 77.0,
                  "rosterForCurrentScoringPeriod": {"entries": []}}},
    ]}
    http = FakeHttp({"/leagues/222": {**ESPN_META, **schedule}})
    ep = ESPNProvider(registry, 2026, client=http, cookies={"espn_s2": "x", "SWID": "{ABC}"})
    st = ep.load_matchup(LeagueConfig(platform="espn", league_id="222",
                                      my_team_id="1", season=2026), 16)
    assert st.current_score_mine == 88.0 and st.current_score_opponent == 77.0


def test_espn_reports_an_error_when_we_have_no_matchup_that_week(registry):
    schedule = {"schedule": [
        {"matchupPeriodId": 5, "home": {"teamId": 3, "totalPoints": 1.0,
                                        "rosterForCurrentScoringPeriod": {"entries": []}},
         "away": {"teamId": 4, "totalPoints": 2.0,
                  "rosterForCurrentScoringPeriod": {"entries": []}}},
    ]}
    http = FakeHttp({"/leagues/222": {**ESPN_META, **schedule}})
    ep = ESPNProvider(registry, 2026, client=http, cookies={"espn_s2": "x", "SWID": "{ABC}"})
    st = ep.load_matchup(LeagueConfig(platform="espn", league_id="222",
                                      my_team_id="1", season=2026), 5)
    assert st.error and "No week 5 matchup" in st.error
    assert st.tier.name == "MINIMUM"


# ---------------------------------------------------------------------------
# Standings (drive the per-week importance multiplier)
# ---------------------------------------------------------------------------

def test_sleeper_standings_parse_records_and_playoff_settings(registry):
    rosters = [
        {"roster_id": 1, "owner_id": "me",
         "settings": {"wins": 7, "losses": 2, "ties": 0, "fpts": 1240, "fpts_decimal": 55}},
        {"roster_id": 2, "owner_id": "them",
         "settings": {"wins": 3, "losses": 6, "ties": 0, "fpts": 1010, "fpts_decimal": 20}},
    ]
    league = dict(SLEEPER_LEAGUE, settings={"playoff_teams": 6, "playoff_week_start": 15})
    http = FakeHttp({"/league/111/rosters": rosters, "/league/111/users": SLEEPER_USERS,
                     "/league/111": league})
    st = SleeperProvider(registry, 2026, client=http).standings("111", "1", 10)

    assert st.playoff_teams == 6 and st.regular_season_weeks == 14
    assert st.games_remaining == 5
    me = st.me
    assert me.wins == 7 and me.record == "7-2"
    assert me.points_for == pytest.approx(1240.55, abs=0.01)


def test_espn_standings_parse_records_and_playoff_settings(registry):
    meta = {
        "settings": {"name": "Money League",
                     "scheduleSettings": {"playoffTeamCount": 4, "matchupPeriodCount": 13},
                     "scoringSettings": {"scoringItems": []}},
        "teams": [
            {"id": 1, "name": "My Squad", "owners": ["{ABC}"],
             "record": {"overall": {"wins": 2, "losses": 8, "ties": 0,
                                    "pointsFor": 980.4, "pointsAgainst": 1210.0}}},
            {"id": 2, "name": "Their Squad", "owners": ["{XYZ}"],
             "record": {"overall": {"wins": 8, "losses": 2, "ties": 0,
                                    "pointsFor": 1305.2, "pointsAgainst": 1050.0}}},
        ],
    }
    http = FakeHttp({"/leagues/222": meta})
    ep = ESPNProvider(registry, 2026, client=http, cookies={"espn_s2": "x", "SWID": "{ABC}"})
    st = ep.standings("222", "1", 11)

    assert st.playoff_teams == 4 and st.regular_season_weeks == 13
    assert st.me.record == "2-8" and st.me.points_for == pytest.approx(980.4)
    assert st.seed() == 2


def test_a_standings_outage_leaves_the_static_weight_untouched(registry):
    """If standings can't load, the league falls back to buy-in x manual multiplier."""
    from app.standings import apply_season_weight

    http = FakeHttp({}, fail={"/leagues/222": ProviderError("500")})
    ep = ESPNProvider(registry, 2026, client=http, cookies={"espn_s2": "x", "SWID": "{ABC}"})
    st = ep.standings("222", "1", 11)
    assert st.error

    league = LeagueConfig(platform="espn", league_id="222", buy_in_usd=100,
                          my_team_id="1", season=2026).to_league()
    apply_season_weight(league, st, loser_punishment=1.0)
    assert league.season_multiplier == 1.0
    assert league.effective_weight == 100


def test_sleeper_load_matchup_stamps_a_season_multiplier(registry):
    rosters = [
        dict(SLEEPER_ROSTERS[0],
             settings={"wins": 8, "losses": 1, "fpts": 1300, "fpts_decimal": 0}),
        dict(SLEEPER_ROSTERS[1],
             settings={"wins": 2, "losses": 7, "fpts": 950, "fpts_decimal": 0}),
    ]
    league = dict(SLEEPER_LEAGUE, settings={"playoff_teams": 1, "playoff_week_start": 15})
    http = FakeHttp({"/league/111/rosters": rosters, "/league/111/users": SLEEPER_USERS,
                     "/league/111/matchups/5": SLEEPER_MATCHUPS, "/league/111": league,
                     "/projections/nfl": SLEEPER_PROJ})
    st = SleeperProvider(registry, 2026, client=http).load_matchup(
        LeagueConfig(platform="sleeper", league_id="111", buy_in_usd=100,
                     my_team_id="1", season=2026), 5)

    assert st.league.outlook is not None
    assert st.league.season_multiplier > 1.0, st.league.season_note
    assert st.league.effective_weight > st.league.static_weight


# ---------------------------------------------------------------------------
# ESPN sparse payloads (observed live: stats present, identity fields missing)
# ---------------------------------------------------------------------------

def sparse_entry(player_id, slot, actual, projected, week=1):
    """What ESPN actually returns for some weeks: the player sub-object carries
    stats but no id, fullName, defaultPositionId or proTeamId."""
    return {
        "playerId": player_id,
        "lineupSlotId": slot,
        "playerPoolEntry": {"player": {"stats": [
            {"scoringPeriodId": week, "statSourceId": 0, "statSplitTypeId": 1,
             "appliedTotal": actual},
            {"scoringPeriodId": week, "statSourceId": 1, "statSplitTypeId": 1,
             "appliedTotal": projected},
        ]}},
    }


def test_sparse_espn_roster_still_resolves_real_player_names(registry):
    """Regression: every player came back as 'ESPN 0' and collapsed into ONE
    canonical player, which silently merged unrelated players across leagues."""
    schedule = {"schedule": [{
        "matchupPeriodId": 1,
        "home": {"teamId": 1, "totalPoints": 0.0, "rosterForCurrentScoringPeriod": {
            "entries": [sparse_entry(3929630, 2, 0.0, 19.0),      # Saquon
                        sparse_entry(4047646, 4, 0.0, 16.9)]}},   # A.J. Brown
        "away": {"teamId": 2, "totalPoints": 0.0, "rosterForCurrentScoringPeriod": {
            "entries": [sparse_entry(4241389, 4, 0.0, 17.4)]}},   # CeeDee
    }]}
    http = FakeHttp({"/leagues/222": {**ESPN_META, **schedule}})
    ep = ESPNProvider(registry, 2026, client=http, cookies={"espn_s2": "x", "SWID": "{ABC}"})
    st = ep.load_matchup(LeagueConfig(platform="espn", league_id="222", buy_in_usd=50,
                                      my_team_id="1", season=2026), 1)

    names = {e.canonical.name for e in st.my_starters}
    assert names == {"Saquon Barkley", "A.J. Brown"}, names
    assert not any("ESPN 0" in e.canonical.name for e in st.all_starters())

    # ...and they must remain DISTINCT canonical players.
    keys = [e.canonical.key for e in st.all_starters()]
    assert len(set(keys)) == 3, f"players collapsed into one another: {keys}"

    saquon = next(e for e in st.my_starters if e.canonical.name == "Saquon Barkley")
    assert saquon.canonical.position == "RB" and saquon.canonical.nfl_team == "PHI"
    assert saquon.projected_points == 19.0


def test_sparse_espn_defense_still_resolves_from_its_negative_id(registry):
    schedule = {"schedule": [{
        "matchupPeriodId": 1,
        "home": {"teamId": 1, "totalPoints": 0.0, "rosterForCurrentScoringPeriod": {
            "entries": [sparse_entry(-16021, 16, 0.0, 7.5)]}},
        "away": {"teamId": 2, "totalPoints": 0.0,
                 "rosterForCurrentScoringPeriod": {"entries": []}},
    }]}
    http = FakeHttp({"/leagues/222": {**ESPN_META, **schedule}})
    ep = ESPNProvider(registry, 2026, client=http, cookies={"espn_s2": "x", "SWID": "{ABC}"})
    st = ep.load_matchup(LeagueConfig(platform="espn", league_id="222",
                                      my_team_id="1", season=2026), 1)
    dst = st.my_starters[0]
    assert dst.canonical.key == "dst:PHI"
    assert dst.canonical.position == "DST"


def test_unidentifiable_espn_entry_is_dropped_not_merged(registry):
    """No id and no name: better to skip the row than to invent a shared player."""
    bad = {"lineupSlotId": 2, "playerPoolEntry": {"player": {"stats": []}}}
    schedule = {"schedule": [{
        "matchupPeriodId": 1,
        "home": {"teamId": 1, "totalPoints": 0.0,
                 "rosterForCurrentScoringPeriod": {"entries": [bad, bad]}},
        "away": {"teamId": 2, "totalPoints": 0.0,
                 "rosterForCurrentScoringPeriod": {"entries": []}},
    }]}
    http = FakeHttp({"/leagues/222": {**ESPN_META, **schedule}})
    ep = ESPNProvider(registry, 2026, client=http, cookies={"espn_s2": "x", "SWID": "{ABC}"})
    st = ep.load_matchup(LeagueConfig(platform="espn", league_id="222",
                                      my_team_id="1", season=2026), 1)
    assert st.my_starters == []


def test_full_espn_payload_still_works_after_the_sparse_fix(registry):
    """The rich shape must keep resolving exactly as before."""
    ep = espn_provider(registry)
    st = ep.load_matchup(LeagueConfig(platform="espn", league_id="222",
                                      my_team_id="1", season=2026), 5)
    assert {e.canonical.name for e in st.my_starters} == {"Saquon Barkley", "A.J. Brown"}


def test_pre_draft_sleeper_league_explains_itself_clearly(registry):
    """A league that hasn't drafted isn't an error - it just has nothing yet."""
    league = dict(SLEEPER_LEAGUE, status="pre_draft")
    http = FakeHttp({"/league/111/rosters": SLEEPER_ROSTERS, "/league/111/users": SLEEPER_USERS,
                     "/league/111/matchups/1": [], "/league/111": league,
                     "/projections/nfl": SLEEPER_PROJ})
    st = SleeperProvider(registry, 2026, client=http).load_matchup(
        LeagueConfig(platform="sleeper", league_id="111", my_team_id="1", season=2026), 1)
    assert "hasn't drafted yet" in st.error
    assert "No week 1 matchup" not in st.error


def test_drafting_sleeper_league_says_so(registry):
    league = dict(SLEEPER_LEAGUE, status="drafting")
    http = FakeHttp({"/league/111/rosters": SLEEPER_ROSTERS, "/league/111/users": SLEEPER_USERS,
                     "/league/111/matchups/1": [], "/league/111": league,
                     "/projections/nfl": SLEEPER_PROJ})
    st = SleeperProvider(registry, 2026, client=http).load_matchup(
        LeagueConfig(platform="sleeper", league_id="111", my_team_id="1", season=2026), 1)
    assert "drafting right now" in st.error


def test_roster_views_include_mboxscore():
    """Regression guard: mMatchupScore returns only team TOTALS. Without
    mBoxscore, ESPN rosters come back as anonymous stat lines with no player id,
    every player resolves to the same placeholder, and cross-league analysis
    silently merges unrelated players."""
    from app.providers.espn import ROSTER_VIEWS

    assert "mBoxscore" in ROSTER_VIEWS
    assert "mRoster" in ROSTER_VIEWS


def test_requested_views_reach_the_wire(registry):
    ep = espn_provider(registry)
    ep.load_matchup(LeagueConfig(platform="espn", league_id="222",
                                 my_team_id="1", season=2026), 5)
    assert ep.http.calls, "no request was made"
