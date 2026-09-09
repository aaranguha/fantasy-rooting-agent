"""Live in-game updates: detect a big play, then say what to want NOW.

The point is that a touchdown rewrites the arithmetic. An update that repeated
the pre-game instruction would be worthless.
"""

from __future__ import annotations

import pytest

from app.analysis import Analyzer, GameGuide
from app.config import AppConfig
from app.db.database import Database
from app.live import (
    DEFAULT_THRESHOLD, LiveEvent, Snapshot, detect_events, live_message,
    live_title, take_snapshot,
)
from app.models import PlayerGameState as GS, SlotType

from .factories import filler, finished, make_game, make_league, make_state, player


class Ctx:
    def __init__(self, states, games):
        self.ok_states = self.states = states
        self.games = games
        self.season, self.week = 2026, 1
        self.errors = []


def analyzer():
    return Analyzer(AppConfig(season=2026), registry=None, nfl=object())


def guide_for(states, game) -> GameGuide:
    return analyzer().analyze_game(Ctx(states, [game]), game)


def bump(state, player_key, points):
    """Simulate a score: add points to that player's live total."""
    for e in state.all_starters():
        if e.canonical.key == player_key:
            e.current_points += points
            e.game_state = GS.IN_PROGRESS
            e.game_fraction_remaining = 0.5


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def test_first_look_at_a_game_sets_a_baseline_and_never_alerts():
    ajb = player("A.J. Brown", "WR", "PHI")
    st = make_state(make_league("Turf Wars", 50),
                    my_players=[(ajb, 16.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                    opp_players=filler("t", 9, 14))
    game = make_game("DAL", "PHI")
    events = detect_events(Snapshot(), guide_for([st], game), [st], game)
    assert events == [], "an empty snapshot must not replay the whole game"


def test_a_touchdown_is_detected_and_small_yardage_is_not():
    ajb = player("A.J. Brown", "WR", "PHI")
    st = make_state(make_league("Turf Wars", 50),
                    my_players=[(ajb, 16.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                    opp_players=filler("t", 9, 14))
    game = make_game("DAL", "PHI")
    before = take_snapshot([st], game)

    bump(st, ajb.key, 1.5)                    # a short catch
    assert detect_events(before, guide_for([st], game), [st], game) == []

    bump(st, ajb.key, 6.7)                    # ...then a touchdown
    events = detect_events(before, guide_for([st], game), [st], game)
    assert len(events) == 1
    assert events[0].delta == pytest.approx(8.2, abs=0.01)


def test_players_not_in_this_game_are_ignored():
    other = player("Elsewhere Guy", "RB", "KC")
    st = make_state(make_league("Turf Wars", 50),
                    my_players=[(other, 16.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                    opp_players=filler("t", 9, 14))
    game = make_game("DAL", "PHI")
    before = take_snapshot([st], game)
    bump(st, other.key, 20.0)
    assert detect_events(before, guide_for([st], game), [st], game) == []


def test_threshold_is_configurable():
    ajb = player("A.J. Brown", "WR", "PHI")
    st = make_state(make_league("Turf Wars", 50),
                    my_players=[(ajb, 16.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                    opp_players=filler("t", 9, 14))
    game = make_game("DAL", "PHI")
    before = take_snapshot([st], game)
    bump(st, ajb.key, 2.5)
    assert detect_events(before, guide_for([st], game), [st], game) == []
    assert detect_events(before, guide_for([st], game), [st], game, threshold=2.0)


# ---------------------------------------------------------------------------
# The two examples asked for
# ---------------------------------------------------------------------------

def test_our_player_scoring_reads_as_good_news_and_says_keep_going():
    """'AJ BROWN BIG PLAY LET'S GO!' + keep going off."""
    ajb = player("A.J. Brown", "WR", "PHI")
    states = []
    for name, money in (("Turf Wars", 50), ("Fantasy Football", 35)):
        states.append(make_state(make_league(name, money),
                                 my_players=[(ajb, 16.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                                 opp_players=filler("t", 9, 14)))
    game = make_game("DAL", "PHI")
    before = take_snapshot(states, game)
    for st in states:
        bump(st, ajb.key, 6.7)

    events = detect_events(before, guide_for(states, game), states, game)
    assert len(events) == 1
    ev = events[0]
    assert ev.ours and not ev.theirs

    reaction = ev.reaction()
    assert "A.J. BROWN" in reaction and "BIG PLAY" in reaction and "+6.7" in reaction
    assert "🔥" in reaction

    instruction = ev.instruction()
    assert "KEEP GOING" in instruction.upper()


def test_opponent_player_scoring_reads_as_bad_news_with_a_new_ceiling():
    """'UH OH... Stevenson scored. Need him under N the rest of the way.'"""
    rham = player("Rhamondre Stevenson", "RB", "NE")
    st = make_state(make_league("Fantasy Football", 35),
                    my_players=finished("m", 9, 14.0),
                    opp_players=[(rham, 12.0, 0, GS.NOT_STARTED)] + finished("t", 8, 13.0))
    game = make_game("NE", "SEA")
    before = take_snapshot([st], game)
    bump(st, rham.key, 7.0)

    events = detect_events(before, guide_for([st], game), [st], game)
    assert len(events) == 1
    ev = events[0]
    assert ev.theirs

    reaction = ev.reaction()
    assert "UH OH" in reaction and "STEVENSON" in reaction and "+7.0" in reaction

    instruction = ev.instruction()
    assert "under" in instruction.lower()
    assert "rest of the way" in instruction


def test_the_ceiling_shrinks_by_what_he_just_scored():
    """The whole point: after a TD we can afford LESS than before."""
    from app.thresholds import compute_threshold

    rham = player("Rhamondre Stevenson", "RB", "NE")
    st = make_state(make_league("Fantasy Football", 35),
                    my_players=finished("m", 9, 14.0),
                    opp_players=[(rham, 12.0, 0, GS.NOT_STARTED)] + finished("t", 8, 13.0))
    exp = st.opp_starters[0]
    before_allow = compute_threshold(st, exp).remaining_required

    bump(st, rham.key, 7.0)
    after_allow = compute_threshold(st, exp).remaining_required

    assert after_allow < before_allow
    assert before_allow - after_allow == pytest.approx(7.0, abs=0.6)


def test_a_player_past_the_line_says_we_need_help_elsewhere():
    rham = player("Rhamondre Stevenson", "RB", "NE")
    st = make_state(make_league("Fantasy Football", 35),
                    my_players=finished("m", 9, 12.0),
                    opp_players=[(rham, 10.0, 0, GS.NOT_STARTED)] + finished("t", 8, 13.5))
    game = make_game("NE", "SEA")
    before = take_snapshot([st], game)
    bump(st, rham.key, 25.0)

    events = detect_events(before, guide_for([st], game), [st], game)
    assert events
    assert "help elsewhere" in events[0].instruction()


def test_conflicted_player_gets_a_mixed_reaction_and_a_range():
    guy = player("Christian McCaffrey", "RB", "SF")
    own = make_state(make_league("Dynasties", 40),
                     my_players=[(guy, 20.0, 0, GS.NOT_STARTED)] + filler("m", 8, 13),
                     opp_players=filler("t", 9, 14))
    face = make_state(make_league("Turf Wars", 50),
                      my_players=filler("m", 9, 14),
                      opp_players=[(guy, 20.0, 0, GS.NOT_STARTED)] + filler("t", 8, 13))
    game = make_game("SF", "LAR")
    before = take_snapshot([own, face], game)
    for st in (own, face):
        bump(st, guy.key, 6.0)

    events = detect_events(before, guide_for([own, face], game), [own, face], game)
    assert events
    ev = events[0]
    assert ev.mixed
    assert "mixed bag" in ev.reaction()


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------

def test_live_message_shows_the_win_probability_move():
    ajb = player("A.J. Brown", "WR", "PHI")
    st = make_state(make_league("Turf Wars", 50),
                    my_players=[(ajb, 16.0, 0, GS.NOT_STARTED)] + finished("m", 8, 13),
                    opp_players=finished("t", 9, 14))
    game = make_game("DAL", "PHI", slot=SlotType.SNF)
    before = take_snapshot([st], game)
    bump(st, ajb.key, 12.0)

    events = detect_events(before, guide_for([st], game), [st], game)
    body = live_message(events, before, game)
    assert "Turf Wars" in body and "→" in body or "↑" in body
    assert "%" in body

    title = live_title(game, events)
    assert "SNF" in title and "A.J. Brown" in title


def test_live_message_caps_how_many_players_it_lists():
    from app.live import MAX_EVENTS

    game = make_game("DAL", "PHI")
    lg = make_league("Turf Wars", 50)
    people = [player(f"Guy {i}", "WR", "PHI") for i in range(6)]
    st = make_state(lg, my_players=[(p, 14.0, 0, GS.NOT_STARTED) for p in people] + filler("m", 4, 13),
                    opp_players=filler("t", 9, 14))
    before = take_snapshot([st], game)
    for p in people:
        bump(st, p.key, 8.0)
    events = detect_events(before, guide_for([st], game), [st], game)
    assert len(events) >= MAX_EVENTS
    body = live_message(events, before, game)
    assert body.count("➡️") <= MAX_EVENTS


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_snapshots_round_trip_through_sqlite(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    snap = Snapshot(points={"L1|p1": 12.5}, win_prob={"L1": 0.62})
    assert db.get_snapshot("G1", 2026, 1) is None
    db.save_snapshot("G1", snap.to_dict(), 2026, 1)
    back = Snapshot.from_dict(db.get_snapshot("G1", 2026, 1))
    assert back.points["L1|p1"] == 12.5
    assert back.win_prob["L1"] == 0.62
    assert not back.is_empty


def test_snapshot_from_none_is_empty():
    assert Snapshot.from_dict(None).is_empty
    assert Snapshot.from_dict({}).is_empty


def test_a_failed_live_send_does_not_advance_the_snapshot(tmp_path):
    """Otherwise the play is lost forever instead of retried next poll."""
    from app.notifiers.base import Notifier, NotificationError
    from app.scheduler import Scheduler

    class Dead(Notifier):
        name = "dead"
        def __init__(self, **kw): super().__init__(retries=1, **kw)
        def _send(self, title, body): raise NotificationError("down")

    ajb = player("A.J. Brown", "WR", "PHI")
    game = make_game("DAL", "PHI", state=GS.IN_PROGRESS)
    st = make_state(make_league("Turf Wars", 50),
                    my_players=[(ajb, 16.0, 0, GS.NOT_STARTED)] + finished("m", 8, 13),
                    opp_players=finished("t", 9, 14))

    class FakeAn:
        def __init__(self):
            self.nfl = object()
        def resolve_week(self, week=None): return 2026, 1, 2
        def load_week(self, week=None, refresh=True): return Ctx([st], [game])
        def primetime(self, ctx): return [game]
        def analyze_game(self, ctx, g): return guide_for([st], g)

    db = Database(tmp_path / "t.sqlite")
    db.save_snapshot(game.id, take_snapshot([st], game).to_dict(), 2026, 1)
    bump(st, ajb.key, 9.0)

    sched = Scheduler(AppConfig(season=2026), analyzer=FakeAn(), notifier=Dead(), db=db)
    assert sched.live_tick() == []
    stored = Snapshot.from_dict(db.get_snapshot(game.id, 2026, 1))
    assert stored.points[f"{st.league.id}|{ajb.key}"] == 0.0, "snapshot advanced despite failure"


def test_a_successful_live_send_advances_the_snapshot(tmp_path):
    from app.notifiers.console import ConsoleNotifier
    from app.scheduler import Scheduler
    import io

    ajb = player("A.J. Brown", "WR", "PHI")
    game = make_game("DAL", "PHI", state=GS.IN_PROGRESS)
    st = make_state(make_league("Turf Wars", 50),
                    my_players=[(ajb, 16.0, 0, GS.NOT_STARTED)] + finished("m", 8, 13),
                    opp_players=finished("t", 9, 14))

    class FakeAn:
        def __init__(self): self.nfl = object()
        def resolve_week(self, week=None): return 2026, 1, 2
        def load_week(self, week=None, refresh=True): return Ctx([st], [game])
        def primetime(self, ctx): return [game]
        def analyze_game(self, ctx, g): return guide_for([st], g)

    db = Database(tmp_path / "t.sqlite")
    db.save_snapshot(game.id, take_snapshot([st], game).to_dict(), 2026, 1)
    bump(st, ajb.key, 9.0)

    notifier = ConsoleNotifier(stream=io.StringIO())
    sched = Scheduler(AppConfig(season=2026), analyzer=FakeAn(), notifier=notifier, db=db)
    results = sched.live_tick()
    assert len(results) == 1 and results[0][1] == 1
    stored = Snapshot.from_dict(db.get_snapshot(game.id, 2026, 1))
    assert stored.points[f"{st.league.id}|{ajb.key}"] == 9.0

    # Same state again: nothing new happened, so no second alert.
    assert sched.live_tick() == []
    assert len(notifier.sent) == 1


def test_games_that_are_not_in_progress_are_skipped(tmp_path):
    from app.notifiers.console import ConsoleNotifier
    from app.scheduler import Scheduler
    import io

    ajb = player("A.J. Brown", "WR", "PHI")
    game = make_game("DAL", "PHI", state=GS.NOT_STARTED)
    st = make_state(make_league("Turf Wars", 50),
                    my_players=[(ajb, 16.0, 0, GS.NOT_STARTED)] + finished("m", 8, 13),
                    opp_players=finished("t", 9, 14))

    class FakeAn:
        def __init__(self): self.nfl = object()
        def resolve_week(self, week=None): return 2026, 1, 2
        def load_week(self, week=None, refresh=True): return Ctx([st], [game])
        def primetime(self, ctx): return [game]
        def analyze_game(self, ctx, g): return guide_for([st], g)

    notifier = ConsoleNotifier(stream=io.StringIO())
    sched = Scheduler(AppConfig(season=2026), analyzer=FakeAn(),
                      notifier=notifier, db=Database(tmp_path / "t.sqlite"))
    assert sched.live_tick() == []
    assert notifier.sent == []
