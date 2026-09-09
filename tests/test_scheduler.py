"""Scheduling, de-duplication, retries and the no-stale-data rule
(spec sections 19, 20)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.analysis import GameGuide
from app.config import AppConfig
from app.db.database import Database
from app.models import PlayerGameState as GS, SlotType
from app.notifiers.base import Notifier, NotificationError
from app.notifiers.console import ConsoleNotifier
from app.scheduler import Scheduler, due_games, next_fire_time

from .factories import filler, make_game, make_league, make_state, player


class FlakyNotifier(Notifier):
    name = "flaky"

    def __init__(self, fail_times: int, **kw):
        super().__init__(retries=3, **kw)
        self.fail_times = fail_times
        self.calls = 0

    def _send(self, title: str, body: str) -> str:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise NotificationError(f"transient failure {self.calls}")
        return "delivered"


class FakeAnalyzer:
    """Stands in for the real Analyzer; counts refreshes so we can prove that
    the guide is rebuilt at send time rather than reused."""

    def __init__(self, games, guide_builder):
        self.guide_builder = guide_builder
        self.loads = 0
        self.nfl = type("FakeNFL", (), {"games": staticmethod(lambda *a, **k: games)})()

    def resolve_week(self, week=None):
        return 2026, 5, 2

    def load_week(self, week=None, refresh=True):
        self.loads += 1
        return {"week": week, "refresh": refresh, "n": self.loads}

    def analyze_game(self, ctx, game):
        return self.guide_builder(ctx, game)


def make_fake(games, guide_builder):
    return FakeAnalyzer(games, guide_builder)


def empty_guide(ctx, game) -> GameGuide:
    return GameGuide(game=game, players=[], leverages=[])


@pytest.fixture()
def cfg(tmp_path: Path) -> AppConfig:
    c = AppConfig(minutes_before=15, notifier="console", season=2026)
    c.leagues = []
    return c


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.sqlite")


# ---------------------------------------------------------------------------
# Window maths
# ---------------------------------------------------------------------------

def test_game_is_due_exactly_inside_the_notification_window():
    game = make_game(minutes_out=15)
    now = datetime.now(timezone.utc)
    assert due_games([game], 15, now=now)
    assert not due_games([game], 15, now=now - timedelta(minutes=2))   # too early
    assert not due_games([game], 5, now=now)                           # window not open


def test_late_grace_still_fires_after_the_mac_wakes_up():
    """Laptop asleep through the window; we fire late rather than not at all."""
    game = make_game(minutes_out=6)   # window opened 9 minutes ago
    assert due_games([game], 15, now=datetime.now(timezone.utc))


def test_we_do_not_fire_after_kickoff_has_passed():
    game = make_game(minutes_out=-30)
    assert not due_games([game], 15, now=datetime.now(timezone.utc))


def test_next_fire_time_is_the_soonest_upcoming_window():
    a = make_game(minutes_out=200, gid="a")
    b = make_game(minutes_out=100, gid="b")
    nxt = next_fire_time([a, b], 15)
    assert nxt == b.kickoff - timedelta(minutes=15)


# ---------------------------------------------------------------------------
# De-duplication
# ---------------------------------------------------------------------------

def test_a_successful_notification_is_never_sent_twice(cfg, db):
    game = make_game(minutes_out=15)
    notifier = ConsoleNotifier()
    sched = Scheduler(cfg, analyzer=make_fake([game], empty_guide), notifier=notifier, db=db)

    ok, _ = sched.notify_game(game, week=5)
    assert ok and len(notifier.sent) == 1

    ok2, detail = sched.notify_game(game, week=5)
    assert not ok2 and "duplicate" in detail
    assert len(notifier.sent) == 1


def test_dedupe_survives_a_restart(cfg, db, tmp_path):
    game = make_game(minutes_out=15)
    Scheduler(cfg, analyzer=make_fake([game], empty_guide),
              notifier=ConsoleNotifier(), db=db).notify_game(game, week=5)

    fresh_db = Database(db.path)          # simulate the process restarting
    notifier = ConsoleNotifier()
    sched = Scheduler(cfg, analyzer=make_fake([game], empty_guide),
                      notifier=notifier, db=fresh_db)
    ok, detail = sched.notify_game(game, week=5)
    assert not ok and "duplicate" in detail
    assert notifier.sent == []


def test_force_overrides_dedupe(cfg, db):
    game = make_game(minutes_out=15)
    notifier = ConsoleNotifier()
    sched = Scheduler(cfg, analyzer=make_fake([game], empty_guide), notifier=notifier, db=db)
    sched.notify_game(game, week=5)
    ok, _ = sched.notify_game(game, force=True, week=5)
    assert ok and len(notifier.sent) == 2


def test_a_failed_send_is_not_recorded_so_the_next_tick_retries(cfg, db):
    game = make_game(minutes_out=15)
    dead = FlakyNotifier(fail_times=99)
    sched = Scheduler(cfg, analyzer=make_fake([game], empty_guide), notifier=dead, db=db)

    ok, _ = sched.notify_game(game, week=5)
    assert not ok
    assert not db.was_sent(game.id, 2026, 5)

    good = ConsoleNotifier()
    sched2 = Scheduler(cfg, analyzer=make_fake([game], empty_guide), notifier=good, db=db)
    ok2, _ = sched2.notify_game(game, week=5)
    assert ok2 and len(good.sent) == 1


def test_dry_run_never_records_a_send(cfg, db):
    game = make_game(minutes_out=15)
    sched = Scheduler(cfg, analyzer=make_fake([game], empty_guide),
                      notifier=ConsoleNotifier(dry_run=True), db=db, dry_run=True)
    ok, _ = sched.notify_game(game, week=5)
    assert ok
    assert not db.was_sent(game.id, 2026, 5)


def test_multiple_monday_games_each_get_their_own_notification(cfg, db):
    a = make_game("BUF", "MIA", slot=SlotType.MNF, minutes_out=15, gid="mnf1")
    b = make_game("SEA", "LAR", slot=SlotType.MNF, minutes_out=90, gid="mnf2")
    notifier = ConsoleNotifier()
    sched = Scheduler(cfg, analyzer=make_fake([a, b], empty_guide), notifier=notifier, db=db)
    sched.notify_game(a, week=5)
    sched.notify_game(b, week=5)
    assert len(notifier.sent) == 2
    assert db.was_sent("mnf1", 2026, 5) and db.was_sent("mnf2", 2026, 5)


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------

def test_notifier_retries_transient_failures_then_succeeds(monkeypatch):
    monkeypatch.setattr("app.notifiers.base.time.sleep", lambda *_: None)
    n = FlakyNotifier(fail_times=2)
    res = n.send("t", "b")
    assert res.ok and res.attempts == 3


def test_notifier_gives_up_after_the_retry_budget(monkeypatch):
    monkeypatch.setattr("app.notifiers.base.time.sleep", lambda *_: None)
    n = FlakyNotifier(fail_times=99)
    res = n.send("t", "b")
    assert not res.ok and res.attempts == 3 and "transient failure" in res.detail


def test_send_log_records_both_outcomes(cfg, db, monkeypatch):
    monkeypatch.setattr("app.notifiers.base.time.sleep", lambda *_: None)
    game = make_game(minutes_out=15)
    Scheduler(cfg, analyzer=make_fake([game], empty_guide),
              notifier=FlakyNotifier(fail_times=99), db=db).notify_game(game, week=5)
    Scheduler(cfg, analyzer=make_fake([game], empty_guide),
              notifier=ConsoleNotifier(), db=db).notify_game(game, week=5)
    rows = db.recent(5)
    assert [r["ok"] for r in rows] == [1, 0]


# ---------------------------------------------------------------------------
# No stale data
# ---------------------------------------------------------------------------

def test_lineup_changes_between_scheduling_and_kickoff_are_picked_up(cfg, db):
    """The guide must be built from a fresh load at send time, not precomputed."""
    game = make_game(minutes_out=15)
    lineups = {"star": "Benched Guy"}

    def builder(ctx, g):
        p = player(lineups["star"], "RB", "PHI")
        lg = make_league("Dynasty", 100)
        st = make_state(lg,
                        my_players=[(p, 18.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                        opp_players=filler("t", 9, 14))
        from app.analysis import Analyzer
        an = Analyzer(cfg)
        ctx2 = type("C", (), {"ok_states": [st], "games": [g]})()
        return an.analyze_game(ctx2, g)

    fake = make_fake([game], builder)
    sched = Scheduler(cfg, analyzer=fake, notifier=ConsoleNotifier(), db=db)

    guide_before = sched.build_guide(game, week=5)
    assert any("Benched Guy" in p.player.name for p in guide_before.players)

    lineups["star"] = "Late Swap Star"      # manager changes the lineup
    guide_after = sched.build_guide(game, week=5)
    assert any("Late Swap Star" in p.player.name for p in guide_after.players)
    assert not any("Benched Guy" in p.player.name for p in guide_after.players)
    assert fake.loads == 2, "each build must refresh, never reuse a cached guide"


def test_build_guide_always_requests_a_refresh(cfg, db):
    game = make_game(minutes_out=15)
    captured = {}

    def builder(ctx, g):
        captured.update(ctx if isinstance(ctx, dict) else {})
        return empty_guide(ctx, g)

    fake = make_fake([game], builder)
    Scheduler(cfg, analyzer=fake, notifier=ConsoleNotifier(), db=db).build_guide(game, week=5)
    assert captured.get("refresh") is True
