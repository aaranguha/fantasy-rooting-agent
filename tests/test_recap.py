"""Post-game recap: reuses the same rooting/leverage engine on final data to
summarize how a finished TNF/SNF/MNF game actually went."""

from __future__ import annotations

from app.analysis import Analyzer
from app.config import AppConfig
from app.db.database import Database
from app.models import PlayerGameState as GS, SlotType
from app.notifiers.base import Notifier
from app.recap import recap_body, recap_title, verdict_for
from app.scheduler import Scheduler

from .factories import finished, make_game, make_league, make_state, player


class Ctx:
    def __init__(self, states, games):
        self.ok_states = self.states = states
        self.games = games
        self.season, self.week = 2026, 5
        self.errors = []


def analyzer():
    return Analyzer(AppConfig(season=2026), registry=None, nfl=object())


def _final_guide():
    smasher = player("Puka Nacua", "WR", "LAR")
    buster = player("Christian McCaffrey", "RB", "SF")
    lg = make_league("Turf Wars", 50)
    st = make_state(lg,
                    my_players=[(smasher, 7.0, 22.4, GS.FINAL), (buster, 20.0, 3.0, GS.FINAL)]
                    + finished("m", 6, 12),
                    opp_players=finished("t", 8, 14))
    game = make_game("SF", "LAR", state=GS.FINAL)
    game.away_score, game.home_score = 17, 24
    return analyzer().analyze_game(Ctx([st], [game]), game), smasher, buster


# -- verdicts ---------------------------------------------------------------

def test_verdict_flags_a_smash_and_a_bust():
    guide, *_ = _final_guide()
    by_name = {p.player.name: p for p in guide.players}
    v_smash = verdict_for(by_name["Puka Nacua"])
    v_bust = verdict_for(by_name["Christian McCaffrey"])
    assert v_smash.tag == "smash" and v_smash.actual == 22.4
    assert v_bust.tag == "bust" and v_bust.actual == 3.0


def test_verdict_is_none_for_an_in_line_performance():
    p = player("Steady Eddie", "RB", "SF")
    lg = make_league("Turf Wars", 50)
    st = make_state(lg, my_players=[(p, 14.0, 15.0, GS.FINAL)] + finished("m", 7, 12),
                    opp_players=finished("t", 8, 14))
    game = make_game("SF", "LAR", state=GS.FINAL)
    guide = analyzer().analyze_game(Ctx([st], [game]), game)
    rooting = next(x for x in guide.players if x.player.name == "Steady Eddie")
    assert verdict_for(rooting) is None


# -- rendering ---------------------------------------------------------------

def test_recap_title_names_the_final_score():
    game = make_game("SF", "LAR", state=GS.FINAL)
    game.away_score, game.home_score = 17, 24
    title = recap_title(game)
    assert "SF @ LAR" in title and "17-24" in title


def test_recap_body_reports_smashes_busts_and_the_biggest_swing():
    guide, *_ = _final_guide()
    body = recap_body(guide)
    assert "Biggest swing" in body
    assert "Puka Nacua" in body and "SMASHED" in body
    assert "Christian McCaffrey" in body and "BUSTED" in body
    assert "LEAGUES NOW" in body


def test_recap_body_has_no_smash_or_bust_section_for_an_ordinary_night():
    p = player("Nobody Special", "RB", "SF")
    lg = make_league("Turf Wars", 50)
    st = make_state(lg, my_players=[(p, 12.0, 13.0, GS.FINAL)] + finished("m", 7, 12),
                    opp_players=finished("t", 8, 14))
    game = make_game("SF", "LAR", state=GS.FINAL)
    guide = analyzer().analyze_game(Ctx([st], [game]), game)
    body = recap_body(guide)
    assert "SMASHED" not in body and "BUSTED" not in body


# -- scheduler tick -----------------------------------------------------------

class FakeNFL:
    def __init__(self, games):
        self.games_list = games

    def games(self, season, week, stype, cache_ttl=60):
        return self.games_list


class FakeAnalyzer:
    def __init__(self, games, guide):
        self.nfl = FakeNFL(games)
        self._guide = guide

    def resolve_week(self, week=None):
        return 2026, 5, 2

    def load_week(self, week=None, refresh=True):
        return None

    def analyze_game(self, ctx, game):
        return self._guide


class Recorder(Notifier):
    name = "rec"

    def __init__(self):
        super().__init__(retries=1)
        self.sent = []

    def _send(self, title, body):
        self.sent.append((title, body))
        return "ok"


def test_recap_tick_sends_once_for_a_final_primetime_game(tmp_path):
    guide, *_ = _final_guide()
    game = guide.game
    cfg = AppConfig(season=2026)
    cfg.leagues = []
    rec = Recorder()
    db = Database(tmp_path / "t.sqlite")
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([game], guide), notifier=rec, db=db)

    out = sched.recap_tick()
    assert len(out) == 1 and out[0][1] is True
    assert len(rec.sent) == 1

    # Second tick: already sent - dedupe suppresses a repeat.
    assert sched.recap_tick() == []
    assert len(rec.sent) == 1


def test_recap_tick_skips_games_still_in_progress_or_not_primetime(tmp_path):
    guide, *_ = _final_guide()
    not_final = make_game("DAL", "NYG", state=GS.IN_PROGRESS)
    regular = make_game("KC", "DEN", state=GS.FINAL, slot=SlotType.REGULAR)
    cfg = AppConfig(season=2026)
    cfg.leagues = []
    rec = Recorder()
    sched = Scheduler(cfg, analyzer=FakeAnalyzer([not_final, regular], guide),
                      notifier=rec, db=Database(tmp_path / "t.sqlite"))
    assert sched.recap_tick() == []
    assert rec.sent == []
