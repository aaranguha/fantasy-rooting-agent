"""Bluesky buzz detection: post parsing, collision filtering, classification,
spike logic, and the scheduler tick that turns a spike into one push."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.analysis import GameGuide
from app.bluesky import (
    BuzzCategory, BuzzResult, Post, assess, classify, gather, _parse_post,
    _relevant, update_baseline,
)
from app.config import AppConfig
from app.models import CanonicalPlayer, League, Platform, ScoringSettings
from app.models import PlayerGameState as GS
from app.models import Side
from app.notifiers.base import Notifier
from app.scheduler import Scheduler

from .factories import make_game


NACUA = CanonicalPlayer(key="p:nacua", name="Puka Nacua", position="WR", nfl_team="LAR")


def _raw(text, *, mins_ago=5, likes=0, reposts=0, name="Some Fan", handle="fan.bsky.social"):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=mins_ago)).isoformat()
    return {"uri": "at://x", "author": {"handle": handle, "displayName": name},
            "record": {"text": text, "createdAt": ts},
            "likeCount": likes, "repostCount": reposts, "replyCount": 0}


# -- parsing + relevance -------------------------------------------------

def test_parse_post_reads_engagement_and_time():
    p = _parse_post(_raw("Nacua with a huge catch", likes=10, reposts=3))
    assert p and p.likes == 10 and p.reposts == 3
    assert p.engagement == 10 + 2 * 3

def test_relevance_filter_drops_non_football_collisions():
    football = _parse_post(_raw("Puka Nacua leaves the game with an ankle injury"))
    music = _parse_post(_raw("puka shells and nacua necklace vibes at the beach"))
    assert _relevant(football, NACUA) is True
    assert _relevant(music, NACUA) is False


# -- classification ---------------------------------------------------

def test_classify_injury_beats_the_rest():
    posts = [_parse_post(_raw(t)) for t in [
        "Nacua is being helped off the field, looks like a knee injury",
        "Rams fans holding their breath as Nacua limps to the blue tent",
        "No word yet but Nacua did not return",
    ]]
    assert classify(posts) == BuzzCategory.INJURY

def test_classify_big_play():
    posts = [_parse_post(_raw(t)) for t in [
        "NACUA 60 YARD TOUCHDOWN ARE YOU KIDDING", "what a catch by Nacua, unreal",
        "Nacua to the house!!"]]
    assert classify(posts) == BuzzCategory.BIG_PLAY

def test_classify_news():
    posts = [_parse_post(_raw(t)) for t in [
        "Sources: the Rams have benched Puka Nacua for the second half",
        "per sources Nacua was a healthy scratch"]]
    assert classify(posts) == BuzzCategory.NEWS

def test_classify_returns_unclear_when_no_signal_or_a_tie():
    assert classify([_parse_post(_raw("Nacua is so good man"))]) == BuzzCategory.UNCLEAR
    tie = [_parse_post(_raw("Nacua touchdown but also looked like he tweaked his ankle"))]
    assert classify(tie) == BuzzCategory.UNCLEAR


# -- spike logic ----------------------------------------------------

def _result(count, baseline, reporters=0):
    posts = [_parse_post(_raw("Nacua injury update", name="Ian Rapoport")) for _ in range(reporters)]
    r = BuzzResult(player=NACUA, posts=posts, category=BuzzCategory.INJURY,
                   count=count, baseline=baseline, reporter_posts=posts)
    return r

def test_spike_needs_both_the_floor_and_the_multiple():
    assert _result(8, 1).is_spike is False          # over 3x but under the floor
    assert _result(20, 12).is_spike is False        # over floor but under 3x of 12=36
    assert _result(40, 5).is_spike is True

def test_a_reporter_post_is_a_spike_on_its_own():
    assert _result(4, 100, reporters=2).is_spike is True

def test_update_baseline_moves_toward_the_observation():
    b = update_baseline(0.0, 0, 10)
    assert b == 10.0
    b2 = update_baseline(10.0, 5, 2)
    assert 2 < b2 < 10


# -- gather (window + fake client) ---------------------------------

class FakeClient:
    def __init__(self, raws):
        self.raws = raws
        self.queries = []

    def search(self, query, *, limit=100):
        self.queries.append(query)
        return self.raws

def test_gather_filters_by_window_and_relevance():
    raws = [
        _raw("Puka Nacua touchdown!", mins_ago=3),
        _raw("Puka Nacua highlight from earlier", mins_ago=90),   # outside 20m window
        _raw("random puka nacua beach post", mins_ago=1),          # not football
    ]
    posts = gather(FakeClient(raws), NACUA)
    assert len(posts) == 1 and "touchdown" in posts[0].text


# -- scheduler tick -------------------------------------------------

class Recorder(Notifier):
    name = "rec"

    def __init__(self):
        super().__init__(retries=1)
        self.sent = []

    def _send(self, title, body):
        self.sent.append((title, body))
        return "ok"


class _Ctx:
    def __init__(self, states, games):
        self.ok_states = self.states = states
        self.games = games
        self.season, self.week = 2026, 5
        self.errors = []


class _Analyzer:
    def __init__(self, ctx):
        self.ctx = ctx
        self.nfl = None

    def resolve_week(self, week=None):
        return 2026, 5, 2

    def load_week(self, week=None, refresh=True):
        return self.ctx

    def analyze_game(self, ctx, game):
        return GameGuide(game=game, players=[], leverages=[])


def _state_with(player_obj):
    from app.models import FantasyPlayerExposure, LineupStatus, MatchupState
    lg = League(id="L1", name="Turf Wars", platform=Platform.SLEEPER, buy_in_usd=50,
                season=2026, scoring=ScoringSettings())
    st = MatchupState(league=lg, week=5)
    st.my_starters.append(FantasyPlayerExposure(
        canonical=player_obj, league=lg, side=Side.MINE,
        lineup_status=LineupStatus.STARTER, projected_points=14.0))
    return st


@pytest.fixture()
def sched(tmp_path, monkeypatch):
    from app.db.database import Database
    monkeypatch.setenv("BLUESKY_HANDLE", "x.bsky.social")
    monkeypatch.setenv("BLUESKY_APP_PASSWORD", "app-pass")
    game = make_game("DAL", "LAR", state=GS.IN_PROGRESS)
    ctx = _Ctx([_state_with(NACUA)], [game])
    cfg = AppConfig(season=2026)
    cfg.leagues = []
    rec = Recorder()
    s = Scheduler(cfg, analyzer=_Analyzer(ctx), notifier=rec,
                  db=Database(tmp_path / "t.sqlite"))
    return s, rec


def test_buzz_tick_sends_on_a_confirmed_spike(sched, monkeypatch):
    s, rec = sched
    spike = BuzzResult(player=NACUA, posts=[_parse_post(_raw("Nacua carted off"))],
                       category=BuzzCategory.INJURY, count=45, baseline=3.0,
                       reporter_posts=[])
    spike.top_post = spike.posts[0]
    monkeypatch.setattr("app.scheduler.assess", lambda *a, **k: spike)
    monkeypatch.setattr("app.scheduler.MIN_SAMPLES", 0)

    out = s.buzz_tick()
    assert len(out) == 1 and out[0][1] == BuzzCategory.INJURY
    assert "blowing up on Bluesky" in rec.sent[0][1]

    # Immediate re-run: cooldown suppresses a second identical alert.
    assert s.buzz_tick() == []


def test_buzz_tick_is_a_noop_without_credentials(tmp_path, monkeypatch):
    from app.db.database import Database
    monkeypatch.delenv("BLUESKY_HANDLE", raising=False)
    monkeypatch.delenv("BLUESKY_APP_PASSWORD", raising=False)
    game = make_game("DAL", "LAR", state=GS.IN_PROGRESS)
    cfg = AppConfig(season=2026)
    cfg.leagues = []
    s = Scheduler(cfg, analyzer=_Analyzer(_Ctx([_state_with(NACUA)], [game])),
                  notifier=Recorder(), db=Database(tmp_path / "t.sqlite"))
    assert s.buzz_tick() == []
