"""A league flagged low_priority (spec: "mention it, don't make it a priority")
must still be fully analyzed everywhere, but never crowd the push with a full
player block - only a condensed footer line."""

from __future__ import annotations

from app.analysis import Analyzer
from app.config import AppConfig
from app.formatting import phone_message, long_report
from app.models import PlayerGameState as GS

from .factories import filler, make_game, make_league, make_state, player


class Ctx:
    def __init__(self, states, games):
        self.ok_states = self.states = states
        self.games = games
        self.season, self.week = 2026, 5
        self.errors = []


def analyzer():
    return Analyzer(AppConfig(season=2026), registry=None, nfl=object())


def test_low_priority_league_still_gets_full_analysis():
    """The weight math, thresholds and win probabilities are untouched - only
    rendering treats it differently."""
    p = player("Bench Warmer", "WR", "PHI")
    lg = make_league("I Mean We Couldd", 0, low_priority=True)
    st = make_state(lg, my_players=[(p, 14.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                    opp_players=filler("t", 9, 14))
    game = make_game("DAL", "PHI")
    guide = analyzer().analyze_game(Ctx([st], [game]), game)

    assert guide.players, "a low-priority league must still be analyzed"
    rooting = guide.players[0]
    assert rooting.lines[0].threshold.value > 0, "thresholds still compute normally"


def test_low_priority_only_exposure_is_demoted_to_background():
    p = player("Bench Warmer", "WR", "PHI")
    lg = make_league("I Mean We Couldd", 0, low_priority=True)
    st = make_state(lg, my_players=[(p, 14.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                    opp_players=filler("t", 9, 14))
    game = make_game("DAL", "PHI")
    guide = analyzer().analyze_game(Ctx([st], [game]), game)

    assert guide.foreground_relevant == []
    assert len(guide.background_relevant) == 1
    assert guide.background_relevant[0].player.name == "Bench Warmer"


def test_a_player_who_also_matters_in_a_normal_league_stays_foreground():
    """Owned in BOTH a real league and a low-priority one: he must still get his
    full block - the low-priority flag never hides a genuinely important player."""
    p = player("A.J. Brown", "WR", "PHI")
    real = make_league("Turf Wars", 50)
    quiet = make_league("I Mean We Couldd", 0, low_priority=True)
    s1 = make_state(real, my_players=[(p, 16.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                    opp_players=filler("t", 9, 14))
    s2 = make_state(quiet, my_players=[(p, 16.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                    opp_players=filler("t", 9, 14))
    game = make_game("DAL", "PHI")
    guide = analyzer().analyze_game(Ctx([s1, s2], [game]), game)

    assert len(guide.foreground_relevant) == 1
    assert guide.foreground_relevant[0].player.name == "A.J. Brown"
    assert guide.background_relevant == []


def test_push_condenses_low_priority_players_into_one_footer_line():
    p1 = player("Bench Warmer", "WR", "PHI")
    p2 = player("Also Quiet", "RB", "PHI")
    lg = make_league("I Mean We Couldd", 0, low_priority=True)
    st = make_state(lg,
                    my_players=[(p1, 14.0, 0, GS.NOT_STARTED), (p2, 12.0, 0, GS.NOT_STARTED)]
                    + filler("m", 7, 13),
                    opp_players=filler("t", 9, 14))
    game = make_game("DAL", "PHI")
    guide = analyzer().analyze_game(Ctx([st], [game]), game)

    body = phone_message(guide)
    assert "🔕" in body and "low priority" in body
    assert "I Mean We Couldd" in body
    assert "Bench Warmer" in body and "Also Quiet" in body
    # Condensed: one footer line, not one full block per player.
    assert body.count("🔕") == 1


def test_footer_labels_which_side_each_low_priority_player_is_on():
    """The exact case reported: one guy ours, one guy against, in the same
    low-priority league - the footer must say who's on which side, not just
    list both names together with no indication of team."""
    ours = player("Brock Purdy", "QB", "SF")
    theirs = player("Davante Adams", "WR", "LAR")
    lg = make_league("I MEAN WE COULDD", 0, low_priority=True)
    st = make_state(lg,
                    my_players=[(ours, 18.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                    opp_players=[(theirs, 14.0, 0, GS.NOT_STARTED)] + filler("t", 8, 14))
    game = make_game("SF", "LAR")
    guide = analyzer().analyze_game(Ctx([st], [game]), game)

    body = phone_message(guide)
    footer = body[body.index("🔕"):]
    ours_line = next(l for l in footer.splitlines() if l.strip().startswith("ours:"))
    vs_line = next(l for l in footer.splitlines() if l.strip().startswith("vs us:"))
    assert "Brock Purdy" in ours_line and "Davante Adams" not in ours_line
    assert "Davante Adams" in vs_line and "Brock Purdy" not in vs_line


def test_footer_only_shows_the_side_line_that_applies():
    """All background players owned (nobody faced): no empty 'vs us:' line."""
    p = player("Brock Purdy", "QB", "SF")
    lg = make_league("I MEAN WE COULDD", 0, low_priority=True)
    st = make_state(lg, my_players=[(p, 18.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                    opp_players=filler("t", 9, 14))
    game = make_game("SF", "LAR")
    guide = analyzer().analyze_game(Ctx([st], [game]), game)
    body = phone_message(guide)
    footer = body[body.index("🔕"):]
    assert "ours:" in footer and "vs us:" not in footer


def test_push_never_gives_a_low_priority_player_a_full_block():
    """The footer still labels sides (ours/vs us), but never gives him his own
    verdict headline or threshold - that's what "full block" means here."""
    p = player("Bench Warmer", "WR", "PHI")
    lg = make_league("I Mean We Couldd", 0, low_priority=True)
    st = make_state(lg, my_players=[(p, 14.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                    opp_players=filler("t", 9, 14))
    game = make_game("DAL", "PHI")
    guide = analyzer().analyze_game(Ctx([st], [game]), game)
    body = phone_message(guide)
    # A full block carries the player's own emoji-verdict headline and a
    # NEED/UNDER/WANT threshold. Neither appears for a background-only player.
    assert f"· NEED" not in body and f"· UNDER" not in body and f"· WANT" not in body
    assert "Bench Warmer" not in body.split("🔕")[0], \
        "he must not appear before the footer as a full block"


def test_low_priority_players_do_not_count_against_the_five_player_cap():
    """Five real players should ALL still get full blocks even if a sixth,
    low-priority-only player is also relevant this game."""
    real_lg = make_league("Turf Wars", 50)
    quiet_lg = make_league("I Mean We Couldd", 0, low_priority=True)
    real_players = [player(f"Real {i}", "WR", "PHI") for i in range(5)]
    quiet_player = player("Quiet Guy", "RB", "PHI")

    st_real = make_state(real_lg,
                         my_players=[(p, 15.0, 0, GS.NOT_STARTED) for p in real_players],
                         opp_players=filler("t", 9, 14))
    st_quiet = make_state(quiet_lg,
                          my_players=[(quiet_player, 14.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                          opp_players=filler("t", 9, 14))
    game = make_game("DAL", "PHI")
    guide = analyzer().analyze_game(Ctx([st_real, st_quiet], [game]), game)

    body = phone_message(guide)
    for p in real_players:
        assert p.name in body, f"{p.name} was crowded out by the low-priority league"
    assert "Quiet Guy" in body  # still mentioned, just in the footer
    assert "🔕" in body


def test_all_leagues_low_priority_and_none_relevant_reads_as_neutral():
    game = make_game("DAL", "PHI")
    guide = analyzer().analyze_game(Ctx([], [game]), game)
    assert "neutral" in phone_message(guide).lower()


def test_long_report_tags_the_league_as_low_priority():
    p = player("Bench Warmer", "WR", "PHI")
    lg = make_league("I Mean We Couldd", 0, low_priority=True)
    st = make_state(lg, my_players=[(p, 14.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                    opp_players=filler("t", 9, 14))
    game = make_game("DAL", "PHI")
    guide = analyzer().analyze_game(Ctx([st], [game]), game)
    assert "[low priority]" in long_report(guide)


# ---------------------------------------------------------------------------
# Config plumbing
# ---------------------------------------------------------------------------

def test_league_config_round_trips_low_priority():
    from app.config import LeagueConfig

    lc = LeagueConfig(platform="sleeper", league_id="1", low_priority=True)
    d = lc.__dict__
    back = LeagueConfig.from_dict(d)
    assert back.low_priority is True
    assert back.to_league().low_priority is True


def test_league_config_defaults_to_normal_priority():
    from app.config import LeagueConfig

    lc = LeagueConfig.from_dict({"platform": "sleeper", "league_id": "1"})
    assert lc.low_priority is False


def test_providers_thread_low_priority_onto_the_league_object(monkeypatch):
    """Regression: low_priority must survive the trip through the provider's own
    League(...) construction, not just LeagueConfig."""
    from app.config import LeagueConfig
    from app.playerids import PlayerRegistry
    from app.providers.sleeper import SleeperProvider

    class FakeHttp:
        def get(self, url, **kw):
            if "/rosters" in url:
                return [{"roster_id": 1, "owner_id": "me", "players": []}]
            if "/users" in url:
                return [{"user_id": "me", "display_name": "Me"}]
            if "/matchups" in url:
                return []
            return {"name": "Quiet League", "scoring_settings": {}}

    sp = SleeperProvider(PlayerRegistry(), 2026, client=FakeHttp())
    cfg = LeagueConfig(platform="sleeper", league_id="111", my_team_id="1",
                       season=2026, low_priority=True)
    state = sp.load_matchup(cfg, 5)
    assert state.league.low_priority is True
