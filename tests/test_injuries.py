"""In-game injury tracking: classification, the debounced state machine, the
ESPN summary parser, and the scheduler tick that ties them together."""

from __future__ import annotations

import pytest

from app.analysis import GameGuide
from app.config import AppConfig
from app.injuries import (
    InjuryPhase, InjuryReport, InjuryUpdate, advance, classify, injury_message,
)
from app.models import CanonicalPlayer, League, Platform, ScoringSettings
from app.models import PlayerGameState as GS
from app.models import Side
from app.notifiers.base import Notifier
from app.scheduler import Scheduler

from .factories import make_game


AJB = CanonicalPlayer(key="p:ajb", name="A.J. Brown", position="WR",
                      nfl_team="PHI", espn_id=4047646)
FOE = CanonicalPlayer(key="p:foe", name="Jalen Carter", position="DT",
                      nfl_team="PHI", espn_id=4426502)


# -- classification --------------------------------------------------------

@pytest.mark.parametrize("status,note,phase", [
    ("Questionable", "Questionable to return", InjuryPhase.IN_QUESTION),
    ("", "being evaluated for a concussion", InjuryPhase.IN_QUESTION),
    ("Doubtful", "Doubtful to return", InjuryPhase.IN_QUESTION),
    ("Out", "", InjuryPhase.OUT),
    ("Questionable", "ruled out for the game", InjuryPhase.OUT),
    ("", "will not return (ankle)", InjuryPhase.OUT),
    ("Questionable", "", InjuryPhase.HEALTHY),          # bare pre-game tag = he's playing
    ("Active", "", InjuryPhase.HEALTHY),
])
def test_classify(status, note, phase):
    assert classify(status, note) == phase


# -- state machine -------------------------------------------------------

def _run(states, phase, *, detail="Ankle", note="Questionable to return", players=None):
    players = players or {"p:ajb": AJB}
    reports = {} if phase is None else {
        "p:ajb": InjuryReport(espn_id="1", name="A.J. Brown", team="PHI",
                              phase=phase, detail=detail, note=note)}
    return advance(states, reports, players=players)


def test_first_sighting_is_silent_even_if_already_hurt():
    states, updates = _run({}, InjuryPhase.IN_QUESTION)
    assert updates == []
    assert states["p:ajb"]["phase"] == "in_question"
    assert states["p:ajb"]["seen"] is True


def test_hurt_needs_two_consecutive_polls_before_it_fires():
    states, _ = _run({}, InjuryPhase.HEALTHY)          # seed healthy
    states, u1 = _run(states, InjuryPhase.IN_QUESTION)  # first sighting of hurt
    assert u1 == []
    states, u2 = _run(states, InjuryPhase.IN_QUESTION)  # confirmed
    assert len(u2) == 1 and u2[0].now == InjuryPhase.IN_QUESTION
    assert "HURT" in u2[0].headline() and "Ankle" in u2[0].headline()


def test_a_one_poll_flicker_does_not_fire():
    states, _ = _run({}, InjuryPhase.HEALTHY)
    states, _ = _run(states, InjuryPhase.IN_QUESTION)   # flicker
    states, u = _run(states, InjuryPhase.HEALTHY)       # back to normal next poll
    assert u == []


def test_ruled_out_fires_immediately_no_debounce():
    states, _ = _run({}, InjuryPhase.HEALTHY)
    states, u = _run(states, InjuryPhase.OUT, note="Ruled out - hamstring")
    assert len(u) == 1 and u[0].now == InjuryPhase.OUT
    assert "RULED OUT" in u[0].headline()


def test_full_arc_hurt_then_back_then_out():
    states, _ = _run({}, InjuryPhase.HEALTHY)
    states, _ = _run(states, InjuryPhase.IN_QUESTION)
    states, hurt = _run(states, InjuryPhase.IN_QUESTION)
    assert hurt and hurt[0].now == InjuryPhase.IN_QUESTION

    states, _ = _run(states, InjuryPhase.HEALTHY)
    states, back = _run(states, InjuryPhase.HEALTHY)
    assert back and back[0].now == InjuryPhase.HEALTHY and "BACK" in back[0].headline()

    states, _ = _run(states, InjuryPhase.IN_QUESTION)
    states, hurt2 = _run(states, InjuryPhase.IN_QUESTION)
    assert hurt2
    states, out = _run(states, InjuryPhase.OUT)
    assert out and out[0].now == InjuryPhase.OUT


def test_return_to_health_is_silent_if_we_never_flagged_him():
    states, _ = _run({}, InjuryPhase.HEALTHY)
    states, _ = _run(states, InjuryPhase.HEALTHY)
    states, u = _run(states, InjuryPhase.HEALTHY)
    assert u == []


# -- rendering ----------------------------------------------------------

def _rooting_stub(owned=(), faced=()):
    def _lines(names):
        return [type("L", (), {"league": type("Lg", (), {"name": n})()})() for n in names]
    return type("R", (), {"owned_lines": _lines(owned), "faced_lines": _lines(faced)})()


def test_update_message_names_the_leagues_and_side():
    ours = InjuryUpdate(player=AJB, was=InjuryPhase.HEALTHY, now=InjuryPhase.OUT,
                        detail="Ankle", note="Ruled out",
                        rooting=_rooting_stub(owned=["Turf Wars"]))
    body = injury_message([ours])
    assert "RULED OUT" in body and "down a starter in Turf Wars" in body

    theirs = InjuryUpdate(player=FOE, was=InjuryPhase.HEALTHY, now=InjuryPhase.IN_QUESTION,
                          detail="Knee", note="Questionable to return",
                          rooting=_rooting_stub(faced=["Gary Harris"]))
    body = injury_message([theirs])
    assert "against you in Gary Harris" in body and "keep you updated" in body


# -- ESPN summary parser ---------------------------------------------------

class FakeHttp:
    def __init__(self, payload):
        self.payload = payload

    def get(self, url, params=None, headers=None, cache_ttl=0, **kw):
        return self.payload


def test_game_injuries_parses_the_summary_feed():
    from app.providers.nfl import NFLScheduleProvider

    payload = {"injuries": [{
        "team": {"abbreviation": "PHI"},
        "injuries": [
            {"status": "Questionable", "athlete": {"id": "4047646", "displayName": "A.J. Brown"},
             "type": {"description": "Questionable to return"}, "details": {"type": "Hamstring"}},
            {"status": "Out", "athlete": {"id": "1", "displayName": "Backup Guy"},
             "type": {"description": "Out"}},
        ],
    }]}
    nfl = NFLScheduleProvider(client=FakeHttp(payload))
    reports = nfl.game_injuries("401")
    by_name = {r.name: r for r in reports}
    assert by_name["A.J. Brown"].phase == InjuryPhase.IN_QUESTION
    assert by_name["A.J. Brown"].detail == "Hamstring"
    assert by_name["Backup Guy"].phase == InjuryPhase.OUT


def test_game_injuries_survives_a_feed_outage():
    from app.providers.base import ProviderError
    from app.providers.nfl import NFLScheduleProvider

    class Boom:
        def get(self, *a, **k):
            raise ProviderError("503")

    assert NFLScheduleProvider(client=Boom()).game_injuries("401") == []


# -- scheduler integration ------------------------------------------------

class Recorder(Notifier):
    name = "rec"

    def __init__(self):
        super().__init__(retries=1)
        self.sent = []

    def _send(self, title, body):
        self.sent.append((title, body))
        return "ok"


class _Ctx:
    def __init__(self, states, games, registry):
        self.ok_states = self.states = states
        self.games = games
        self.registry = registry
        self.season, self.week = 2026, 5
        self.errors = []


class _Analyzer:
    def __init__(self, ctx, reports):
        self.ctx = ctx
        self._reports = reports
        self.nfl = type("N", (), {"game_injuries": staticmethod(lambda gid, **k: reports)})()

    def resolve_week(self, week=None):
        return 2026, 5, 2

    def load_week(self, week=None, refresh=True):
        return self.ctx

    def analyze_game(self, ctx, game):
        return GameGuide(game=game, players=[], leverages=[])


def _matchup_state(player_obj):
    from app.models import FantasyPlayerExposure, LineupStatus, MatchupState
    lg = League(id="L1", name="Turf Wars", platform=Platform.SLEEPER, buy_in_usd=50,
                season=2026, scoring=ScoringSettings())
    st = MatchupState(league=lg, week=5)
    st.my_starters.append(FantasyPlayerExposure(
        canonical=player_obj, league=lg, side=Side.MINE,
        lineup_status=LineupStatus.STARTER, projected_points=15.0))
    return st


def test_injury_tick_sends_one_message_on_a_confirmed_ruled_out(tmp_path):
    from app.db.database import Database
    from app.playerids import PlayerRegistry

    reg = PlayerRegistry()
    reg.add(AJB)
    game = make_game("DAL", "PHI", state=GS.IN_PROGRESS)
    ctx = _Ctx([_matchup_state(AJB)], [game], reg)
    report = [InjuryReport(espn_id="4047646", name="A.J. Brown", team="PHI",
                           phase=InjuryPhase.OUT, detail="Hamstring", note="Ruled out")]

    cfg = AppConfig(season=2026)
    cfg.leagues = []
    rec = Recorder()
    db = Database(tmp_path / "t.sqlite")
    sched = Scheduler(cfg, analyzer=_Analyzer(ctx, report), notifier=rec, db=db)

    # First tick: seeds silently (already OUT at first sight).
    assert sched.injury_tick() == []
    assert rec.sent == []

    # He's healthy-seeded now; flip to OUT -> immediate alert.
    sched.analyzer._reports[:] = [InjuryReport(
        espn_id="4047646", name="A.J. Brown", team="PHI", phase=InjuryPhase.OUT,
        detail="Hamstring", note="Ruled out")]
    # reseed as healthy to model "was fine, now out"
    db.save_snapshot(f"{game.id}:inj", {"p:ajb": {"phase": "healthy", "seen": True,
                                                  "notified_phase": "healthy"}}, 2026, 5)
    out = sched.injury_tick()
    assert len(out) == 1
    assert len(rec.sent) == 1
    assert "RULED OUT" in rec.sent[0][1]
