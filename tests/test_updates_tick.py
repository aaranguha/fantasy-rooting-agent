"""updates_tick(): live score-jumps, injury transitions and Bluesky buzz merge
into AT MOST one push per game per tick, instead of piling up separately."""

from __future__ import annotations

from app.analysis import Analyzer as RealAnalyzer
from app.bluesky import BuzzCategory, BuzzResult
from app.config import AppConfig
from app.db.database import Database
from app.injuries import InjuryPhase, InjuryReport, PlayerInjuryState
from app.live import take_snapshot
from app.models import PlayerGameState as GS
from app.notifiers.base import Notifier, NotificationError
from app.playerids import PlayerRegistry
from app.scheduler import Scheduler

from .factories import finished, make_game, make_league, make_state, player


class Ctx:
    def __init__(self, states, games, registry=None):
        self.ok_states = self.states = states
        self.games = games
        self.registry = registry or PlayerRegistry()
        self.season, self.week = 2026, 5
        self.errors = []


class Recorder(Notifier):
    name = "rec"

    def __init__(self, *, fail=False):
        super().__init__(retries=1)
        self.sent = []
        self.fail = fail

    def _send(self, title, body):
        if self.fail:
            raise NotificationError("down")
        self.sent.append((title, body))
        return "ok"


class FakeNFL:
    def __init__(self, reports):
        self.reports = reports

    def game_injuries(self, game_id, **k):
        return self.reports


class FakeAnalyzer:
    """Forwards analysis to the real Analyzer; fakes only the network edges."""

    def __init__(self, ctx, reports=()):
        self.ctx = ctx
        self.nfl = FakeNFL(list(reports))
        self._real = RealAnalyzer(AppConfig(season=2026), registry=None, nfl=object())

    def resolve_week(self, week=None):
        return 2026, 5, 2

    def load_week(self, week=None, refresh=True):
        return self.ctx

    def primetime(self, ctx):
        return list(ctx.games)

    def analyze_game(self, ctx, game):
        return self._real.analyze_game(ctx, game)


def _setup(tmp_path, *, notifier=None):
    ajb = player("A.J. Brown", "WR", "PHI")
    hurt = player("Big Guy", "RB", "PHI")
    lg = make_league("Turf Wars", 50)
    game = make_game("DAL", "PHI", state=GS.IN_PROGRESS)
    st = make_state(lg,
                    my_players=[(ajb, 16.0, 3.0, GS.IN_PROGRESS), (hurt, 10.0, 2.0, GS.IN_PROGRESS)]
                    + finished("m", 7, 12),
                    opp_players=finished("t", 9, 12))
    reg = PlayerRegistry()
    reg.add(ajb)
    reg.add(hurt)
    ctx = Ctx([st], [game], registry=reg)

    report = [InjuryReport(espn_id="", name="Big Guy", team="PHI",
                           phase=InjuryPhase.OUT, detail="Ankle", note="Ruled out")]
    analyzer = FakeAnalyzer(ctx, report)
    db = Database(tmp_path / "t.sqlite")
    rec = notifier or Recorder()
    cfg = AppConfig(season=2026)
    cfg.leagues = []
    sched = Scheduler(cfg, analyzer=analyzer, notifier=rec, db=db)

    # Seed the baselines a real first-poll would have recorded: a score
    # snapshot to diff against, and a "healthy, already seen" injury state so
    # the OUT report reads as a genuine transition, not a first sighting.
    db.save_snapshot(game.id, take_snapshot([st], game).to_dict(), 2026, 5)
    db.save_snapshot(f"{game.id}:inj",
                     {hurt.key: PlayerInjuryState(seen=True).to_dict()}, 2026, 5)
    st.my_starters[0].current_points = 12.0   # A.J. Brown +9: a real live event
    return sched, rec, db, game, ajb, hurt


def test_a_live_event_and_an_injury_in_the_same_game_send_one_combined_push(tmp_path):
    sched, rec, db, game, ajb, hurt = _setup(tmp_path)

    out = sched.updates_tick()
    assert len(out) == 1 and out[0][0].id == game.id
    assert len(rec.sent) == 1, "must be ONE push, not one per event type"
    title, body = rec.sent[0]
    assert game.matchup in title
    assert "A. J. BROWN" in body.upper() or "BROWN" in body.upper()
    assert "RULED OUT" in body and "BIG GUY" in body.upper()

    # Both halves actually persisted since the shared send succeeded.
    from app.injuries import PlayerInjuryState
    inj_state = db.get_snapshot(f"{game.id}:inj", 2026, 5)
    assert inj_state[hurt.key]["phase"] == "out"
    live_state = db.get_snapshot(game.id, 2026, 5)
    assert live_state["points"][f"{'L-Turf Wars'}|{ajb.key}"] == 12.0


def test_a_failed_combined_send_advances_neither_half(tmp_path):
    sched, rec, db, game, ajb, hurt = _setup(tmp_path, notifier=Recorder(fail=True))
    before_inj = db.get_snapshot(f"{game.id}:inj", 2026, 5)
    before_live = db.get_snapshot(game.id, 2026, 5)

    assert sched.updates_tick() == []
    assert db.get_snapshot(f"{game.id}:inj", 2026, 5) == before_inj
    assert db.get_snapshot(game.id, 2026, 5) == before_live


def test_a_pure_buzz_spike_still_gets_its_own_tight_push(tmp_path, monkeypatch):
    monkeypatch.setenv("BLUESKY_HANDLE", "x.bsky.social")
    monkeypatch.setenv("BLUESKY_APP_PASSWORD", "y")
    ajb = player("A.J. Brown", "WR", "PHI")
    lg = make_league("Turf Wars", 50)
    game = make_game("DAL", "PHI", state=GS.IN_PROGRESS)
    st = make_state(lg, my_players=[(ajb, 16.0, 3.0, GS.IN_PROGRESS)] + finished("m", 8, 12),
                    opp_players=finished("t", 9, 12))
    ctx = Ctx([st], [game])
    analyzer = FakeAnalyzer(ctx, [])
    db = Database(tmp_path / "t.sqlite")
    rec = Recorder()
    cfg = AppConfig(season=2026)
    cfg.leagues = []
    sched = Scheduler(cfg, analyzer=analyzer, notifier=rec, db=db)
    # Seed baselines so no spurious live/injury events fire this tick.
    db.save_snapshot(game.id, take_snapshot([st], game).to_dict(), 2026, 5)
    db.save_snapshot(f"{game.id}:inj", {}, 2026, 5)

    spike = BuzzResult(player=ajb, posts=[], category=BuzzCategory.BIG_PLAY,
                       count=40, baseline=2.0)
    spike.top_post = None
    monkeypatch.setattr("app.scheduler.assess", lambda *a, **k: spike)
    monkeypatch.setattr("app.scheduler.MIN_SAMPLES", 0)

    out = sched.updates_tick()
    assert len(out) == 1
    title, body = rec.sent[0]
    assert title == "📈 Bluesky: A. Brown"
