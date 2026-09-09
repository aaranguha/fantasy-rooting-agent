"""Sunday early/late slate coverage: one condensed digest in the morning
covering both windows, a second one 15 minutes before the late window
(recomputed on real early-window results), and SNF itself untouched."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.analysis import Analyzer, GameGuide
from app.config import AppConfig
from app.db.database import Database
from app.models import PlayerGameState as GS, SlotType
from app.notifiers.console import ConsoleNotifier
from app.rooting import analyze_player
from app.scheduler import Scheduler
from app.sunday_slate import (
    condensed_digest, earliest_kickoff, is_sunday, slate_games, slate_window,
)

from .factories import filler, finished, make_game, make_league, make_state, player
from .test_conflicts import curve_for

LA = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")

# A real Sunday, for unambiguous fixtures.
SUNDAY = datetime(2026, 9, 13)  # Sunday


def et_game(gid, hour, minute=0, *, slot=SlotType.REGULAR, away="AAA", home="BBB"):
    g = make_game(away, home, gid=gid, slot=slot)
    g.kickoff = SUNDAY.replace(hour=hour, minute=minute, tzinfo=ET)
    return g


def analyzer():
    return Analyzer(AppConfig(season=2026), registry=None, nfl=object())


class Ctx:
    def __init__(self, states, games):
        self.ok_states = self.states = states
        self.games = games
        self.season, self.week = 2026, 2
        self.errors = []


def guide_for(states, game) -> GameGuide:
    return analyzer().analyze_game(Ctx(states, [game]), game)


# ---------------------------------------------------------------------------
# Window classification
# ---------------------------------------------------------------------------

def test_early_window_is_before_3pm_et():
    g = et_game("a", 13, 0)  # 1:00 ET
    assert slate_window(g) == "early"


def test_late_window_is_3pm_et_or_after():
    g = et_game("a", 16, 25)  # 4:25 ET
    assert slate_window(g) == "late"
    edge = et_game("b", 15, 0)  # exactly 3:00 ET
    assert slate_window(edge) == "late"


def test_primetime_games_are_not_slate_games():
    snf = et_game("snf", 20, 20, slot=SlotType.SNF)
    mnf = et_game("mnf", 20, 15, slot=SlotType.MNF)
    assert slate_window(snf) is None
    assert slate_window(mnf) is None


def test_a_non_sunday_regular_game_is_not_a_slate_game():
    g = et_game("a", 13, 0)
    g.kickoff = g.kickoff.replace(year=2026, month=9, day=10)  # a Thursday
    assert slate_window(g) is None


def test_slate_games_filters_by_window_and_date():
    early = et_game("early", 13, 0)
    late = et_game("late", 16, 5)
    games = [early, late]
    assert [g.id for g in slate_games(games, window="early")] == ["early"]
    assert [g.id for g in slate_games(games, window="late")] == ["late"]
    assert {g.id for g in slate_games(games)} == {"early", "late"}


def test_earliest_kickoff_picks_the_soonest():
    a = et_game("a", 16, 25)
    b = et_game("b", 16, 5)
    assert earliest_kickoff([a, b]) == b.kickoff


def test_is_sunday_checks_local_weekday():
    assert is_sunday(LA, now=SUNDAY.replace(hour=8, tzinfo=LA))
    monday = SUNDAY + timedelta(days=1)
    assert not is_sunday(LA, now=monday.replace(hour=8, tzinfo=LA))


# ---------------------------------------------------------------------------
# The condensed digest itself: strong signals only, names only
# ---------------------------------------------------------------------------

def _rooting_for(name, pos, team, *, own_money=0, face_money=0, own_proj=15.0,
                 face_proj=15.0, opp_finished=True):
    """Build a real PlayerRooting via the actual engine, for a given
    ownership shape, so the digest is tested against the real emoji logic
    rather than a hand-picked category."""
    p = player(name, pos, team)
    states = []
    if own_money:
        lg = make_league(f"{name}-own", own_money)
        states.append(make_state(lg, my_players=[(p, own_proj, 0, GS.NOT_STARTED)]
                                 + finished("m", 8, 13),
                                 opp_players=finished("t", 9, 13.5)))
    if face_money:
        lg = make_league(f"{name}-face", face_money)
        states.append(make_state(lg, my_players=finished("m", 9, 13.5),
                                 opp_players=[(p, face_proj, 0, GS.NOT_STARTED)]
                                 + finished("t", 8, 13)))
    return analyze_player(curve_for(p, states))


def test_digest_shows_only_the_four_strong_signal_colors():
    rocket = _rooting_for("Rocket Guy", "WR", "AAA", own_money=50)         # 🚀
    green = _rooting_for("Green Guy", "RB", "AAA", own_money=60, face_money=20,
                         own_proj=20, face_proj=8)                          # likely 🟢
    toss_up = _rooting_for("Toss Up Guy", "WR", "AAA", own_money=30, face_money=30)  # 🟡
    fade = _rooting_for("Fade Guy", "WR", "BBB", face_money=50)            # 🔴

    game = et_game("g1", 13, 0)
    guide = GameGuide(game=game, players=[rocket, green, toss_up, fade], leverages=[])
    body = condensed_digest([guide])

    assert "Rocket Guy" in body and "\U0001f680" in body
    assert "Fade Guy" in body and "\U0001f534" in body
    # The toss-up is deliberately excluded from a QUICK digest.
    assert "Toss Up Guy" not in body


def test_digest_groups_by_bucket_with_headers():
    rocket = _rooting_for("Rocket Guy", "WR", "AAA", own_money=50)
    fade = _rooting_for("Fade Guy", "WR", "BBB", face_money=50)
    game = et_game("g1", 13, 0)
    guide = GameGuide(game=game, players=[rocket, fade], leverages=[])
    body = condensed_digest([guide])

    assert "GO OFF" in body
    assert "FADE" in body
    # GO OFF section appears before FADE, matching the requested order.
    assert body.index("GO OFF") < body.index("FADE")


def test_public_enemy_keeps_the_skull_within_the_fade_bucket():
    from app.rooting import mark_public_enemy

    villain = _rooting_for("Villain", "QB", "BBB", face_money=100)
    mark_public_enemy([villain])
    assert villain.public_enemy

    game = et_game("g1", 13, 0)
    guide = GameGuide(game=game, players=[villain], leverages=[])
    body = condensed_digest([guide])
    assert "☠️" in body and "Villain" in body
    assert "FADE" in body   # grouped with plain root-against, per "🔴/☠️"


def test_digest_merges_players_across_multiple_games():
    rocket = _rooting_for("Rocket Guy", "WR", "AAA", own_money=50)
    fade = _rooting_for("Fade Guy", "WR", "CCC", face_money=50)
    g1 = et_game("g1", 13, 0)
    g2 = et_game("g2", 13, 0, away="CCC", home="DDD")
    guides = [
        GameGuide(game=g1, players=[rocket], leverages=[]),
        GameGuide(game=g2, players=[fade], leverages=[]),
    ]
    body = condensed_digest(guides)
    assert "Rocket Guy" in body and "Fade Guy" in body


def test_a_quiet_slate_says_so_kindly():
    game = et_game("g1", 13, 0)
    assert "quiet" in condensed_digest([GameGuide(game=game, players=[], leverages=[])]).lower()


def test_digest_caps_players_per_bucket_and_notes_the_overflow():
    from app.sunday_slate import MAX_PER_BUCKET

    players = [_rooting_for(f"Rocket {i}", "WR", "AAA", own_money=50)
              for i in range(MAX_PER_BUCKET + 3)]
    game = et_game("g1", 13, 0)
    body = condensed_digest([GameGuide(game=game, players=players, leverages=[])])
    assert "+3 more" in body


# ---------------------------------------------------------------------------
# Scheduler: morning_tick no longer previews SNF
# ---------------------------------------------------------------------------

def test_morning_tick_no_longer_sends_a_standalone_snf_preview():
    from app.notifiers.base import Notifier

    class FakeAnalyzer:
        def __init__(self, games):
            self.nfl = type("N", (), {"games": staticmethod(lambda *a, **k: games)})()
        def resolve_week(self, week=None): return 2026, 2, 2
        def load_week(self, week=None, refresh=True): return None
        def analyze_game(self, ctx, g): return GameGuide(game=g, players=[], leverages=[])

    snf = et_game("snf", 20, 20, slot=SlotType.SNF)
    from app.config import AppConfig as Cfg

    cfg = Cfg(season=2026, timezone="America/Los_Angeles", morning_summary_time="09:00")
    notifier = ConsoleNotifier()
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([snf]), notifier=notifier, db=Database(":memory:")
                      if False else Database(__import__("pathlib").Path("/tmp/_never_used.sqlite")))
    now = SUNDAY.replace(hour=9, tzinfo=LA)
    results = sched.morning_tick(now=now, force=True)
    assert results == [], "SNF must not get its own per-game morning preview anymore"


# ---------------------------------------------------------------------------
# Scheduler: sunday_morning_tick and sunday_second_slate_tick
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path / "t.sqlite")


class FakeAnalyzer:
    def __init__(self, games, guide_builder=None):
        self.nfl = type("N", (), {"games": staticmethod(lambda *a, **k: games)})()
        self.guide_builder = guide_builder or (
            lambda ctx, g: GameGuide(game=g, players=[], leverages=[]))

    def resolve_week(self, week=None):
        return 2026, 2, 2

    def load_week(self, week=None, refresh=True):
        return {"week": week}

    def analyze_game(self, ctx, g):
        return self.guide_builder(ctx, g)


def cfg_with(**kw) -> AppConfig:
    c = AppConfig(season=2026, timezone="America/Los_Angeles")
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_sunday_morning_tick_only_fires_on_sunday(db):
    early = et_game("early", 13, 0)
    cfg = cfg_with(sunday_morning_time="08:00")
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([early]), notifier=ConsoleNotifier(), db=db)
    monday = (SUNDAY + timedelta(days=1)).replace(hour=8, tzinfo=LA)
    assert sched.sunday_morning_tick(now=monday) is None


def test_sunday_morning_tick_covers_both_windows(db):
    ajb = player("A.J. Brown", "WR", "AAA")
    lg = make_league("Turf Wars", 50)
    st = make_state(lg, my_players=[(ajb, 16.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                    opp_players=filler("t", 9, 14))
    early = et_game("early", 13, 0)
    late = et_game("late", 16, 5, away="CCC", home="DDD")

    def build(ctx, g):
        return analyzer().analyze_game(Ctx([st], [g]), g)

    cfg = cfg_with(sunday_morning_time="08:00")
    notifier = ConsoleNotifier()
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([early, late], build), notifier=notifier, db=db)
    now = SUNDAY.replace(hour=8, tzinfo=LA)
    ok, detail = sched.sunday_morning_tick(now=now)
    assert ok
    assert len(notifier.sent) == 1


def test_sunday_morning_tick_dedupes_by_date(db):
    early = et_game("early", 13, 0)
    cfg = cfg_with(sunday_morning_time="08:00")
    notifier = ConsoleNotifier()
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([early]), notifier=notifier, db=db)
    now = SUNDAY.replace(hour=8, tzinfo=LA)
    sched.sunday_morning_tick(now=now)
    assert sched.sunday_morning_tick(now=now + timedelta(hours=1)) is None
    assert len(notifier.sent) == 1


def test_sunday_second_slate_fires_15_min_before_late_window(db):
    late = et_game("late", 16, 5)   # 4:05 ET kickoff
    cfg = cfg_with(minutes_before=15)
    notifier = ConsoleNotifier()
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([late]), notifier=notifier, db=db)

    too_early = SUNDAY.replace(hour=15, minute=30, tzinfo=ET)
    assert sched.sunday_second_slate_tick(now=too_early) is None

    on_time = SUNDAY.replace(hour=15, minute=50, tzinfo=ET)   # 3:50 ET = 15 min before 4:05
    ok, detail = sched.sunday_second_slate_tick(now=on_time)
    assert ok
    assert len(notifier.sent) == 1


def test_sunday_second_slate_only_covers_late_window_games(db):
    """The update should be scoped to late-window players, not repeat the
    early-window ones the morning digest already covered."""
    early_player = player("Early Guy", "WR", "AAA")
    late_player = player("Late Guy", "WR", "CCC")
    lg = make_league("Turf Wars", 50)
    early_state = make_state(lg,
                             my_players=[(early_player, 16.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                             opp_players=filler("t", 9, 14))
    late_state = make_state(lg,
                            my_players=[(late_player, 16.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                            opp_players=filler("t", 9, 14))

    early = et_game("early", 13, 0)
    late = et_game("late", 16, 5, away="CCC", home="DDD")

    def build(ctx, g):
        st = early_state if g.id == "early" else late_state
        return analyzer().analyze_game(Ctx([st], [g]), g)

    cfg = cfg_with(minutes_before=15)
    notifier = ConsoleNotifier()
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([early, late], build), notifier=notifier, db=db)
    on_time = SUNDAY.replace(hour=15, minute=50, tzinfo=ET)
    sched.sunday_second_slate_tick(now=on_time)

    body = notifier.sent[0][1]
    assert "Late Guy" in body
    assert "Early Guy" not in body


def test_sunday_second_slate_dedupes_by_date(db):
    late = et_game("late", 16, 5)
    cfg = cfg_with(minutes_before=15)
    notifier = ConsoleNotifier()
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([late]), notifier=notifier, db=db)
    on_time = SUNDAY.replace(hour=15, minute=50, tzinfo=ET)
    sched.sunday_second_slate_tick(now=on_time)
    assert sched.sunday_second_slate_tick(now=on_time + timedelta(minutes=2)) is None
    assert len(notifier.sent) == 1


def test_sunday_second_slate_does_nothing_with_no_late_games(db):
    early_only = et_game("early", 13, 0)
    cfg = cfg_with(minutes_before=15)
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([early_only]), notifier=ConsoleNotifier(), db=db)
    now = SUNDAY.replace(hour=15, minute=50, tzinfo=ET)
    assert sched.sunday_second_slate_tick(now=now) is None


def test_sunday_morning_and_second_slate_and_kickoff_keys_never_collide(db):
    """Three independent dedupe keys for one Sunday: morning digest, second
    slate update, and the eventual real SNF kickoff push."""
    early = et_game("early", 13, 0)
    cfg = cfg_with(sunday_morning_time="08:00")
    notifier = ConsoleNotifier()
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([early]), notifier=notifier, db=db)
    now = SUNDAY.replace(hour=8, tzinfo=LA)
    sched.sunday_morning_tick(now=now)

    date_str = f"{SUNDAY:%Y-%m-%d}"
    assert db.was_sent(f"sunday-morning:{date_str}", 0, 0)
    assert not db.was_sent(f"sunday-second-slate:{date_str}", 0, 0)
    assert not db.was_sent("snf-game-id", 2026, 2)


def test_sunday_morning_time_is_independent_of_the_weekday_morning_time(db):
    """Setting morning_summary_time (for TNF/MNF) must not affect when the
    Sunday digest fires - they're separate knobs."""
    early = et_game("early", 13, 0)
    cfg = cfg_with(morning_summary_time="08:00", sunday_morning_time="09:23")
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([early]), notifier=ConsoleNotifier(), db=db)

    at_the_weekday_time = SUNDAY.replace(hour=8, minute=0, tzinfo=LA)
    assert sched.sunday_morning_tick(now=at_the_weekday_time) is None, \
        "must not fire at morning_summary_time - only at sunday_morning_time"

    at_its_own_time = SUNDAY.replace(hour=9, minute=23, tzinfo=LA)
    ok, _ = sched.sunday_morning_tick(now=at_its_own_time)
    assert ok


def test_sunday_morning_time_defaults_to_9_23_am():
    from app.config import AppConfig
    assert AppConfig().sunday_morning_time == "09:23"


def test_sunday_morning_time_round_trips_through_config_json(tmp_path):
    from app.config import AppConfig

    cfg = AppConfig(season=2026, sunday_morning_time="09:23")
    path = cfg.save(tmp_path / "config.json")
    loaded = AppConfig.load(path)
    assert loaded.sunday_morning_time == "09:23"
