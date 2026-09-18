"""Bluesky buzz detection: post parsing, collision filtering, classification,
spike logic, and the scheduler tick that turns a spike into one push."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.analysis import GameGuide
from app.bluesky import (
    BuzzCategory, BuzzResult, Post, Sentiment, assess, classify, extract_relevant_sentence,
    gather, sentiment_for, _parse_post, _relevant, update_baseline,
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

def test_classify_a_mocked_td_in_a_blowout_is_bad_play_not_big_play():
    # Real case: Javonte Williams scores garbage-time TD while his team is
    # getting blown out - action keywords fire, but the post is mocking him,
    # not celebrating. Should not get a celebratory 🔥.
    posts = [_parse_post(_raw(
        "javonte williams taunting as he scores a td in a game the cowboys "
        "are still down late in the 4th quarter is hilariously bad"))]
    assert classify(posts) == BuzzCategory.BAD_PLAY

def test_classify_sarcastic_fire_emoji_alone_is_not_big_play():
    # Real case: a post used 🔥 sarcastically about a terrible outing. A bare
    # fire emoji is too ambiguous (sincere hype vs. sarcasm) to trust on its
    # own, so it shouldn't be a BIG_PLAY trigger by itself anymore.
    posts = [_parse_post(_raw("🔥 Stunningly horrific Baker Mayfield start today"))]
    assert classify(posts) != BuzzCategory.BIG_PLAY

def test_classify_genuine_big_play_with_fire_emoji_still_counts():
    posts = [_parse_post(_raw("NACUA 60 YARD TOUCHDOWN ARE YOU KIDDING 🔥🔥🔥"))]
    assert classify(posts) == BuzzCategory.BIG_PLAY


# -- sentiment (good/bad/neutral, separate from category) --------------

def test_sentiment_injury_is_always_bad():
    assert sentiment_for(BuzzCategory.INJURY, []) == Sentiment.BAD

def test_sentiment_big_play_is_good_bad_play_is_bad():
    assert sentiment_for(BuzzCategory.BIG_PLAY, []) == Sentiment.GOOD
    assert sentiment_for(BuzzCategory.BAD_PLAY, []) == Sentiment.BAD

def test_sentiment_unclear_is_neutral():
    assert sentiment_for(BuzzCategory.UNCLEAR, []) == Sentiment.NEUTRAL

def test_sentiment_news_leans_bad_for_a_suspension():
    posts = [_parse_post(_raw("Sources: the team has suspended Nacua for one game"))]
    assert sentiment_for(BuzzCategory.NEWS, posts) == Sentiment.BAD

def test_sentiment_news_leans_good_for_a_contract_extension():
    posts = [_parse_post(_raw("Breaking: Nacua signed a contract extension"))]
    assert sentiment_for(BuzzCategory.NEWS, posts) == Sentiment.GOOD

def test_sentiment_news_is_neutral_for_a_bare_trade_report():
    # "traded"/"acquired" alone don't say whether it's a good or bad move for
    # the player - no strong signal either way should stay neutral.
    posts = [_parse_post(_raw("Sources: Nacua has been traded"))]
    assert sentiment_for(BuzzCategory.NEWS, posts) == Sentiment.NEUTRAL

def test_headline_and_summary_line_use_sentiment_emoji_not_category_emoji():
    p = CanonicalPlayer(key="p:nacua", name="Puka Nacua", position="WR", nfl_team="LAR")
    injury_post = _parse_post(_raw("Nacua carted off with a knee injury"))
    r = BuzzResult(player=p, posts=[injury_post], category=BuzzCategory.INJURY,
                   count=20, baseline=2.0)
    r.top_post = injury_post
    assert r.sentiment == Sentiment.BAD
    assert r.headline().startswith("🔴 ")
    assert r.summary_line().startswith("🔴 ")


# -- extracting just the relevant clause ---------------------------

def test_extract_pulls_only_the_players_clause_out_of_a_multi_topic_post():
    text = ("Mike Evans 3 first downs on first drive. Kittle and CMC are healthy. "
            "Bosa trucks McClendon on first snap. the offseason did not happen")
    evans = CanonicalPlayer(key="p:evans", name="Mike Evans", position="WR", nfl_team="TB")
    assert extract_relevant_sentence(text, evans) == "Mike Evans 3 first downs on first drive."

def test_extract_falls_back_to_the_first_clause_if_no_direct_mention():
    text = "Huge day so far - already three catches and a score"
    p = CanonicalPlayer(key="p:x", name="Some Guy", position="WR", nfl_team="TB")
    assert extract_relevant_sentence(text, p) == text

def test_summary_line_is_just_the_clause_when_the_name_already_leads_it():
    p = CanonicalPlayer(key="p:nacua", name="Puka Nacua", position="WR", nfl_team="LAR")
    post = _parse_post(_raw("Puka Nacua 41-yard catch", name="NFL Daily News"))
    r = BuzzResult(player=p, posts=[post], category=BuzzCategory.BIG_PLAY,
                  count=13, baseline=3.0)
    r.top_post = post
    assert r.summary_line() == "🟢 Puka Nacua 41-yard catch"

def test_summary_line_prefixes_the_name_when_the_clause_omits_it():
    p = CanonicalPlayer(key="p:nacua", name="Puka Nacua", position="WR", nfl_team="LAR")
    post = _parse_post(_raw("41-yard catch to open the second half"))
    r = BuzzResult(player=p, posts=[post], category=BuzzCategory.BIG_PLAY,
                  count=13, baseline=3.0)
    r.top_post = post
    assert r.summary_line() == "🟢 Puka Nacua: 41-yard catch to open the second half"


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
    title, body = rec.sent[0]
    assert title == "📈 Bluesky: P. Nacua"
    assert body == "🔴 Nacua carted off"

    # Immediate re-run: cooldown suppresses a second identical alert.
    assert s.buzz_tick() == []


KYREN = CanonicalPlayer(key="p:kyren", name="Kyren Williams", position="RB", nfl_team="LAR")


def test_buzz_tick_combines_multiple_spikes_into_one_tight_push(tmp_path, monkeypatch):
    from app.db.database import Database

    monkeypatch.setenv("BLUESKY_HANDLE", "x.bsky.social")
    monkeypatch.setenv("BLUESKY_APP_PASSWORD", "app-pass")
    game = make_game("DAL", "LAR", state=GS.IN_PROGRESS)
    ctx = _Ctx([_state_with(NACUA), _state_with(KYREN)], [game])
    cfg = AppConfig(season=2026)
    cfg.leagues = []
    rec = Recorder()
    s = Scheduler(cfg, analyzer=_Analyzer(ctx), notifier=rec,
                  db=Database(tmp_path / "t.sqlite"))

    results = {
        NACUA.key: BuzzResult(
            player=NACUA, posts=[_parse_post(_raw("Puka Nacua 41-yard catch", name="NFL Daily News"))],
            category=BuzzCategory.BIG_PLAY, count=13, baseline=3.0),
        KYREN.key: BuzzResult(
            player=KYREN, posts=[_parse_post(_raw("Kyren Williams rushes in for a Rams touchdown"))],
            category=BuzzCategory.BIG_PLAY, count=14, baseline=2.0),
    }
    for r in results.values():
        r.top_post = r.posts[0]
    monkeypatch.setattr("app.scheduler.assess",
                        lambda client, player, baseline, now=None: results[player.key])
    monkeypatch.setattr("app.scheduler.MIN_SAMPLES", 0)

    out = s.buzz_tick()
    assert len(out) == 2
    title, body = rec.sent[0]
    assert title == "📈 Bluesky: P. Nacua + K. Williams"
    assert body == ("🟢 Puka Nacua 41-yard catch\n"
                    "🟢 Kyren Williams rushes in for a Rams touchdown")


def _state_with_opponent(player_obj):
    from app.models import FantasyPlayerExposure, LineupStatus, MatchupState
    lg = League(id="L1", name="Turf Wars", platform=Platform.SLEEPER, buy_in_usd=50,
                season=2026, scoring=ScoringSettings())
    st = MatchupState(league=lg, week=5)
    st.opp_starters.append(FantasyPlayerExposure(
        canonical=player_obj, league=lg, side=Side.OPPONENT,
        lineup_status=LineupStatus.STARTER, projected_points=14.0))
    return st


def test_buzz_tick_never_checks_an_opponents_starter(tmp_path, monkeypatch):
    """We only care about our own players - an opponent's starter blowing up
    on Bluesky isn't something we act on, so it should never even be checked."""
    from app.db.database import Database

    monkeypatch.setenv("BLUESKY_HANDLE", "x.bsky.social")
    monkeypatch.setenv("BLUESKY_APP_PASSWORD", "app-pass")
    game = make_game("DAL", "LAR", state=GS.IN_PROGRESS)
    ctx = _Ctx([_state_with_opponent(NACUA)], [game])
    cfg = AppConfig(season=2026)
    cfg.leagues = []
    rec = Recorder()
    s = Scheduler(cfg, analyzer=_Analyzer(ctx), notifier=rec,
                  db=Database(tmp_path / "t.sqlite"))

    called = []
    monkeypatch.setattr("app.scheduler.assess",
                        lambda *a, **k: called.append(1) or (_ for _ in ()).throw(
                            AssertionError("should never be called for an opponent-only player")))
    monkeypatch.setattr("app.scheduler.MIN_SAMPLES", 0)

    assert s.buzz_tick() == []
    assert called == []


def test_buzz_tick_suppresses_on_field_categories_once_the_game_is_final(tmp_path, monkeypatch):
    """A big (or bad) play is fully covered by the post-game recap already;
    re-announcing it as 'trending' after the game ends is just a duplicate.
    Injury/news chatter in that same post-final window is still let through,
    since it can be genuinely new information the recap doesn't have."""
    from app.db.database import Database

    monkeypatch.setenv("BLUESKY_HANDLE", "x.bsky.social")
    monkeypatch.setenv("BLUESKY_APP_PASSWORD", "app-pass")
    game = make_game("DAL", "LAR", state=GS.FINAL)  # final, not live
    ctx = _Ctx([_state_with(NACUA)], [game])
    cfg = AppConfig(season=2026)
    cfg.leagues = []
    rec = Recorder()
    s = Scheduler(cfg, analyzer=_Analyzer(ctx), notifier=rec,
                  db=Database(tmp_path / "t.sqlite"))

    big_play = BuzzResult(
        player=NACUA, posts=[_parse_post(_raw("Puka Nacua 41-yard catch"))],
        category=BuzzCategory.BIG_PLAY, count=13, baseline=3.0)
    big_play.top_post = big_play.posts[0]
    monkeypatch.setattr("app.scheduler.assess", lambda *a, **k: big_play)
    monkeypatch.setattr("app.scheduler.MIN_SAMPLES", 0)
    assert s.buzz_tick() == []

    injury = BuzzResult(
        player=NACUA, posts=[_parse_post(_raw("Nacua carted off"))],
        category=BuzzCategory.INJURY, count=45, baseline=3.0)
    injury.top_post = injury.posts[0]
    monkeypatch.setattr("app.scheduler.assess", lambda *a, **k: injury)
    out = s.buzz_tick()
    assert len(out) == 1 and out[0][1] == BuzzCategory.INJURY


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
