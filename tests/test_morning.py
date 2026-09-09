"""Gameday-morning preview: the SAME push as the kickoff notification, sent
hours earlier, then swapped out (deleted) once the real one lands."""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.config import AppConfig
from app.db.database import Database
from app.formatting import phone_message, phone_title
from app.morning import games_today, is_due, parse_hhmm
from app.models import PlayerGameState as GS
from app.notifiers.base import Notifier, NotificationError, NotificationResult
from app.notifiers.console import ConsoleNotifier
from app.scheduler import Scheduler

from .factories import filler, make_game, make_league, make_state, player

LA = ZoneInfo("America/Los_Angeles")


class FakeAnalyzer:
    def __init__(self, games, guide_builder=None):
        self.nfl = type("N", (), {"games": staticmethod(lambda *a, **k: games)})()
        self.guide_builder = guide_builder or (lambda ctx, g: _empty_guide(g))

    def resolve_week(self, week=None):
        return 2026, 1, 2

    def load_week(self, week=None, refresh=True):
        return {"week": week, "refresh": refresh}

    def analyze_game(self, ctx, g):
        return self.guide_builder(ctx, g)

    def primetime(self, ctx):
        return []


def _empty_guide(g):
    from app.analysis import GameGuide
    return GameGuide(game=g, players=[], leverages=[])


def cfg_with(**kw) -> AppConfig:
    c = AppConfig(season=2026, timezone="America/Los_Angeles")
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def et_game(gid: str, kickoff_et: datetime) -> "NFLGame":
    # MNF, not SNF (make_game's default): these tests exercise the generic
    # per-game morning-preview mechanism, which now deliberately skips SNF
    # (SNF gets its own Sunday-slate digest - see test_sunday_slate.py).
    from app.models import SlotType

    g = make_game("NE", "SEA", gid=gid, slot=SlotType.MNF)
    g.kickoff = kickoff_et.replace(tzinfo=ZoneInfo("America/New_York"))
    return g


class FakeTelegram(Notifier):
    """A Telegram-shaped fake that hands back a message_id and can delete it."""

    name = "telegram"
    supports_delete = True

    def __init__(self, **kw):
        super().__init__(**kw)
        self.sent: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self._next_id = 1000

    def _send(self, title, body):
        self._next_id += 1
        self.sent.append((title, body))
        return "sent via Telegram bot", str(self._next_id)

    def delete(self, message_id: str) -> bool:
        self.deleted.append(message_id)
        return True


@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path / "t.sqlite")


# ---------------------------------------------------------------------------
# Time-of-day window (unchanged plumbing)
# ---------------------------------------------------------------------------

def test_parse_hhmm_reads_the_configured_time():
    assert parse_hhmm("09:00") == dtime(9, 0)
    assert parse_hhmm("07:30") == dtime(7, 30)


def test_parse_hhmm_falls_back_to_nine_on_garbage():
    assert parse_hhmm("not a time") == dtime(9, 0)


def test_is_due_exactly_at_and_after_the_target_time():
    target = dtime(9, 0)
    assert is_due(target, LA, now=datetime(2026, 9, 10, 9, 0, tzinfo=LA))
    assert is_due(target, LA, now=datetime(2026, 9, 10, 11, 30, tzinfo=LA))


def test_is_due_is_false_before_the_target_time():
    assert not is_due(dtime(9, 0), LA, now=datetime(2026, 9, 10, 8, 59, tzinfo=LA))


def test_is_due_expires_after_the_grace_window():
    assert not is_due(dtime(9, 0), LA,
                      now=datetime(2026, 9, 10, 9, 0, tzinfo=LA) + timedelta(hours=4))


def test_games_today_filters_by_local_calendar_date():
    wed = et_game("wed", datetime(2026, 9, 9, 20, 20))
    thu = et_game("thu", datetime(2026, 9, 10, 20, 15))
    today = games_today([wed, thu], LA, on=datetime(2026, 9, 9, tzinfo=LA).date())
    assert [g.id for g in today] == ["wed"]


# ---------------------------------------------------------------------------
# The core ask: the morning preview IS the kickoff message, just sent early
# ---------------------------------------------------------------------------

def test_morning_preview_uses_the_exact_same_render_as_the_kickoff_push(db):
    """Same title/body functions, same content - only the countdown differs
    because it's sent hours before kickoff instead of 15 minutes before."""
    ajb = player("A.J. Brown", "WR", "NE")
    lg = make_league("Turf Wars", 50)
    st = make_state(lg, my_players=[(ajb, 16.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                    opp_players=filler("t", 9, 14))
    game = et_game("g1", datetime(2026, 9, 9, 20, 20))

    def build(ctx, g):
        from app.analysis import Analyzer
        return Analyzer(cfg_with()).analyze_game(
            type("C", (), {"ok_states": [st], "games": [g]})(), g)

    cfg = cfg_with(morning_summary_time="09:00")
    notifier = FakeTelegram()
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([game], build), notifier=notifier, db=db)

    now = datetime(2026, 9, 9, 9, 0, tzinfo=LA)
    results = sched.morning_tick(now=now)
    assert len(results) == 1 and results[0][1] is True

    expected_body = phone_message(build(None, game))
    sent_title, sent_body = notifier.sent[0]
    assert sent_body == expected_body
    assert "A.J. Brown" in sent_title or "A.J. Brown" in sent_body
    # It's the SAME game, just far from kickoff (9am PT -> 8:20pm ET kickoff is
    # ~8h20m away) - the countdown reflects that, unlike a "in 15" kickoff push.
    assert "in 8h" in sent_title


def test_morning_preview_is_not_due_before_the_configured_time(db):
    cfg = cfg_with(morning_summary_time="09:00")
    game = et_game("g1", datetime(2026, 9, 9, 20, 20))
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([game]), notifier=FakeTelegram(), db=db)
    assert sched.morning_tick(now=datetime(2026, 9, 9, 8, 0, tzinfo=LA)) == []


def test_no_primetime_game_today_sends_nothing(db):
    cfg = cfg_with(morning_summary_time="09:00")
    tomorrow = et_game("g1", datetime(2026, 9, 10, 20, 15))
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([tomorrow]), notifier=FakeTelegram(), db=db)
    assert sched.morning_tick(now=datetime(2026, 9, 9, 9, 0, tzinfo=LA)) == []


def test_never_previews_the_same_game_twice(db):
    game = et_game("g1", datetime(2026, 9, 9, 20, 20))
    cfg = cfg_with(morning_summary_time="09:00")
    notifier = FakeTelegram()
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([game]), notifier=notifier, db=db)
    now = datetime(2026, 9, 9, 9, 0, tzinfo=LA)
    sched.morning_tick(now=now)
    later = now + timedelta(hours=2)
    assert sched.morning_tick(now=later) == []
    assert len(notifier.sent) == 1


def test_dedupe_survives_a_restart(db):
    game = et_game("g1", datetime(2026, 9, 9, 20, 20))
    cfg = cfg_with(morning_summary_time="09:00")
    now = datetime(2026, 9, 9, 9, 0, tzinfo=LA)
    Scheduler(cfg, analyzer=FakeAnalyzer([game]), notifier=FakeTelegram(), db=db) \
        .morning_tick(now=now)

    fresh = Database(db.path)
    notifier2 = FakeTelegram()
    sched2 = Scheduler(cfg, analyzer=FakeAnalyzer([game]), notifier=notifier2, db=fresh)
    assert sched2.morning_tick(now=now) == []
    assert notifier2.sent == []


def test_disabled_via_config_never_fires(db):
    cfg = cfg_with(morning_summary=False, morning_summary_time="09:00")
    game = et_game("g1", datetime(2026, 9, 9, 20, 20))
    notifier = FakeTelegram()
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([game]), notifier=notifier, db=db)
    assert sched.morning_tick(now=datetime(2026, 9, 9, 9, 0, tzinfo=LA)) == []
    assert notifier.sent == []


def test_force_ignores_time_and_dedupe(db):
    game = et_game("g1", datetime(2026, 9, 9, 20, 20))
    cfg = cfg_with(morning_summary_time="09:00")
    notifier = FakeTelegram()
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([game]), notifier=notifier, db=db)
    the_middle_of_the_night = datetime(2026, 9, 9, 3, 0, tzinfo=LA)
    results = sched.morning_tick(now=the_middle_of_the_night, force=True)
    assert len(results) == 1 and results[0][1] is True


def test_a_failed_send_is_not_recorded_so_it_is_retried(db):
    class Dead(Notifier):
        name = "dead"
        def __init__(self, **kw): super().__init__(retries=1, **kw)
        def _send(self, title, body): raise NotificationError("down")

    game = et_game("g1", datetime(2026, 9, 9, 20, 20))
    cfg = cfg_with(morning_summary_time="09:00")
    now = datetime(2026, 9, 9, 9, 0, tzinfo=LA)

    sched = Scheduler(cfg, analyzer=FakeAnalyzer([game]), notifier=Dead(), db=db)
    results = sched.morning_tick(now=now)
    assert results == [(game, False, "NotificationError: down")]
    assert not db.was_sent("morning:g1", 2026, 1)

    good = FakeTelegram()
    sched2 = Scheduler(cfg, analyzer=FakeAnalyzer([game]), notifier=good, db=db)
    results2 = sched2.morning_tick(now=now)
    assert results2[0][1] is True and len(good.sent) == 1


def test_dry_run_never_records_a_send(db):
    game = et_game("g1", datetime(2026, 9, 9, 20, 20))
    cfg = cfg_with(morning_summary_time="09:00")
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([game]),
                      notifier=ConsoleNotifier(dry_run=True), db=db, dry_run=True)
    now = datetime(2026, 9, 9, 9, 0, tzinfo=LA)
    results = sched.morning_tick(now=now)
    assert results[0][1] is True
    assert not db.was_sent("morning:g1", 2026, 1)
    assert db.get_morning_push("g1", 2026, 1) is None


def test_multiple_games_today_each_get_their_own_preview_and_dedupe_key(db):
    a = et_game("a", datetime(2026, 9, 9, 19, 15))
    b = et_game("b", datetime(2026, 9, 9, 22, 30))
    cfg = cfg_with(morning_summary_time="09:00")
    notifier = FakeTelegram()
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([a, b]), notifier=notifier, db=db)
    results = sched.morning_tick(now=datetime(2026, 9, 9, 9, 0, tzinfo=LA))
    assert {g.id for g, ok, _ in results} == {"a", "b"}
    assert db.was_sent("morning:a", 2026, 1) and db.was_sent("morning:b", 2026, 1)


# ---------------------------------------------------------------------------
# The delete-and-resend behaviour
# ---------------------------------------------------------------------------

def test_morning_message_id_is_stored_for_later_deletion(db):
    game = et_game("g1", datetime(2026, 9, 9, 20, 20))
    cfg = cfg_with(morning_summary_time="09:00")
    notifier = FakeTelegram()
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([game]), notifier=notifier, db=db)
    sched.morning_tick(now=datetime(2026, 9, 9, 9, 0, tzinfo=LA))

    stored = db.get_morning_push("g1", 2026, 1)
    assert stored is not None
    assert stored["provider"] == "telegram"
    assert stored["message_id"] == notifier.sent and True or stored["message_id"]


def test_kickoff_push_deletes_the_morning_preview_after_a_successful_resend(db):
    game = et_game("g1", datetime(2026, 9, 9, 20, 20))
    game.week = 1
    lg = make_league("Turf Wars", 50)
    st = make_state(lg, my_players=filler("m", 9, 13), opp_players=filler("t", 9, 14))

    def build(ctx, g):
        from app.analysis import Analyzer
        return Analyzer(cfg_with()).analyze_game(
            type("C", (), {"ok_states": [st], "games": [g]})(), g)

    cfg = cfg_with(morning_summary_time="09:00")
    notifier = FakeTelegram()
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([game], build), notifier=notifier, db=db)

    sched.morning_tick(now=datetime(2026, 9, 9, 9, 0, tzinfo=LA))
    morning_id = db.get_morning_push("g1", 2026, 1)["message_id"]
    assert morning_id and morning_id not in notifier.deleted

    ok, _ = sched.notify_game(game, week=1)
    assert ok
    assert morning_id in notifier.deleted
    assert db.get_morning_push("g1", 2026, 1) is None, "must be cleared after deletion"


def test_the_new_kickoff_message_is_never_deleted_by_mistake(db):
    """Only the OLD morning message_id gets deleted - never the one just sent."""
    game = et_game("g1", datetime(2026, 9, 9, 20, 20))
    game.week = 1
    lg = make_league("Turf Wars", 50)
    st = make_state(lg, my_players=filler("m", 9, 13), opp_players=filler("t", 9, 14))

    def build(ctx, g):
        from app.analysis import Analyzer
        return Analyzer(cfg_with()).analyze_game(
            type("C", (), {"ok_states": [st], "games": [g]})(), g)

    cfg = cfg_with(morning_summary_time="09:00")
    notifier = FakeTelegram()
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([game], build), notifier=notifier, db=db)
    sched.morning_tick(now=datetime(2026, 9, 9, 9, 0, tzinfo=LA))
    morning_id = db.get_morning_push("g1", 2026, 1)["message_id"]

    sched.notify_game(game, week=1)
    kickoff_id = notifier.sent[-1]  # the (title, body) tuple, not an id directly
    assert morning_id in notifier.deleted
    assert len(notifier.deleted) == 1, "only the stale preview should be deleted"


def test_a_failed_kickoff_resend_leaves_the_morning_preview_intact(db):
    """Safety property: if the real push fails to send, the earlier preview must
    NOT be deleted first - you'd otherwise be left with nothing."""
    class FlakyThenNever(Notifier):
        name = "telegram"
        supports_delete = True

        def __init__(self, **kw):
            super().__init__(retries=1, **kw)
            self.deleted = []
            self.calls = 0

        def _send(self, title, body):
            self.calls += 1
            if self.calls == 1:
                return "sent via Telegram bot", "morning-id-1"
            raise NotificationError("network blip")

        def delete(self, message_id):
            self.deleted.append(message_id)
            return True

    game = et_game("g1", datetime(2026, 9, 9, 20, 20))
    game.week = 1
    lg = make_league("Turf Wars", 50)
    st = make_state(lg, my_players=filler("m", 9, 13), opp_players=filler("t", 9, 14))

    def build(ctx, g):
        from app.analysis import Analyzer
        return Analyzer(cfg_with()).analyze_game(
            type("C", (), {"ok_states": [st], "games": [g]})(), g)

    cfg = cfg_with(morning_summary_time="09:00")
    notifier = FlakyThenNever()
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([game], build), notifier=notifier, db=db)

    sched.morning_tick(now=datetime(2026, 9, 9, 9, 0, tzinfo=LA))
    assert db.get_morning_push("g1", 2026, 1)["message_id"] == "morning-id-1"

    ok, _ = sched.notify_game(game, week=1)
    assert not ok
    assert notifier.deleted == [], "must not delete the old message when the resend failed"
    assert db.get_morning_push("g1", 2026, 1) is not None, "the fallback must survive"


def test_a_provider_that_cannot_delete_just_leaves_the_old_message_in_place(db):
    """ntfy/iMessage/console/Twilio: delete() no-ops. The kickoff send must
    still succeed and be recorded - deletion is a bonus, not a requirement."""
    game = et_game("g1", datetime(2026, 9, 9, 20, 20))
    game.week = 1
    lg = make_league("Turf Wars", 50)
    st = make_state(lg, my_players=filler("m", 9, 13), opp_players=filler("t", 9, 14))

    def build(ctx, g):
        from app.analysis import Analyzer
        return Analyzer(cfg_with()).analyze_game(
            type("C", (), {"ok_states": [st], "games": [g]})(), g)

    cfg = cfg_with(morning_summary_time="09:00")
    notifier = ConsoleNotifier()
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([game], build), notifier=notifier, db=db)
    sched.morning_tick(now=datetime(2026, 9, 9, 9, 0, tzinfo=LA))

    ok, _ = sched.notify_game(game, week=1)
    assert ok
    # ConsoleNotifier.delete() inherits the no-op base and returns False; that
    # must not be treated as an error.
    assert db.was_sent("g1", 2026, 1)


def test_no_morning_preview_means_kickoff_send_has_nothing_to_delete(db):
    """A game that never got a morning preview (e.g. it appeared mid-week after
    a flex) must send its kickoff push normally with no delete attempt."""
    game = et_game("g1", datetime(2026, 9, 9, 20, 20))
    game.week = 1
    lg = make_league("Turf Wars", 50)
    st = make_state(lg, my_players=filler("m", 9, 13), opp_players=filler("t", 9, 14))

    def build(ctx, g):
        from app.analysis import Analyzer
        return Analyzer(cfg_with()).analyze_game(
            type("C", (), {"ok_states": [st], "games": [g]})(), g)

    notifier = FakeTelegram()
    sched = Scheduler(cfg_with(), analyzer=FakeAnalyzer([game], build), notifier=notifier,
                      db=db)
    ok, _ = sched.notify_game(game, week=1)
    assert ok
    assert notifier.deleted == []


def test_morning_and_kickoff_dedupe_keys_never_collide(db):
    game = et_game("g1", datetime(2026, 9, 9, 20, 20))
    game.week = 1
    cfg = cfg_with(morning_summary_time="09:00")
    notifier = FakeTelegram()
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([game]), notifier=notifier, db=db)
    sched.morning_tick(now=datetime(2026, 9, 9, 9, 0, tzinfo=LA), force=True)
    assert db.was_sent("morning:g1", 2026, 1)
    assert not db.was_sent("g1", 2026, 1), \
        "the morning preview must not mark the real kickoff push as already sent"


# ---------------------------------------------------------------------------
# preview_game: force a specific game regardless of "today"
# ---------------------------------------------------------------------------

def test_preview_game_works_for_a_game_days_out(db):
    """The whole point: test tomorrow's game today without waiting."""
    game = et_game("g1", datetime(2026, 9, 10, 20, 20))   # days from "now"
    cfg = cfg_with(morning_summary_time="09:00")
    notifier = FakeTelegram()
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([game]), notifier=notifier, db=db)

    result_game, ok, detail = sched.preview_game(game, week=1)
    assert ok and len(notifier.sent) == 1
    assert db.get_morning_push("g1", 2026, 1) is not None


def test_preview_game_still_dedupes_against_a_later_morning_tick(db):
    """preview_game and morning_tick share the same dedupe key per game."""
    game = et_game("g1", datetime(2026, 9, 9, 20, 20))
    cfg = cfg_with(morning_summary_time="09:00")
    notifier = FakeTelegram()
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([game]), notifier=notifier, db=db)

    sched.preview_game(game, week=1)
    now = datetime(2026, 9, 9, 9, 0, tzinfo=LA)
    assert sched.morning_tick(now=now) == []   # already "sent" via preview_game
    assert len(notifier.sent) == 1


def test_preview_game_can_still_be_swapped_out_by_the_real_kickoff_push(db):
    game = et_game("g1", datetime(2026, 9, 9, 20, 20))
    game.week = 1
    lg = make_league("Turf Wars", 50)
    st = make_state(lg, my_players=filler("m", 9, 13), opp_players=filler("t", 9, 14))

    def build(ctx, g):
        from app.analysis import Analyzer
        return Analyzer(cfg_with()).analyze_game(
            type("C", (), {"ok_states": [st], "games": [g]})(), g)

    cfg = cfg_with(morning_summary_time="09:00")
    notifier = FakeTelegram()
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([game], build), notifier=notifier, db=db)

    sched.preview_game(game, week=1)
    morning_id = db.get_morning_push("g1", 2026, 1)["message_id"]

    ok, _ = sched.notify_game(game, week=1)
    assert ok and morning_id in notifier.deleted
