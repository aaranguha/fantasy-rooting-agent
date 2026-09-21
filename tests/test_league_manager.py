"""League manager: decision parsing, message formatting, and roster
resolution - all pure/mocked, no real Sleeper or Anthropic calls."""

from __future__ import annotations

from types import SimpleNamespace

from app.league_manager.decide import Decision, decide
from app.league_manager.notify import format_message
from app.league_manager.state import PlayerRef, RosterView, _resolve, _roster_view, _trending


# ---------------------------------------------------------------------------
# state.py helpers
# ---------------------------------------------------------------------------

DUMP = {
    "100": {"full_name": "Puka Nacua", "position": "WR", "team": "LAR", "injury_status": ""},
    "200": {"full_name": "Kyren Williams", "position": "RB", "team": "LAR", "injury_status": "Q"},
}


def test_resolve_known_player():
    p = _resolve("100", DUMP)
    assert p.name == "Puka Nacua" and p.position == "WR" and p.injury_status == ""


def test_resolve_unknown_player_falls_back_to_placeholder():
    p = _resolve("999", DUMP)
    assert p.id == "999" and "999" in p.name


def test_resolve_empty_slot():
    p = _resolve("0", DUMP)
    assert p.name == "(empty)"


def test_trending_resolves_and_filters_to_fantasy_positions():
    dump = {**DUMP, "300": {"full_name": "Some Kicker Coach", "position": "COACH", "team": "LAR"}}

    class FakeHttp:
        def get(self, path, **kw):
            return [{"player_id": "100", "count": 50000},
                    {"player_id": "300", "count": 40000},  # non-fantasy position, dropped
                    {"player_id": "200", "count": 9000}]

    refs = _trending(FakeHttp(), "add", dump)
    assert [r.id for r in refs] == ["100", "200"]
    assert refs[0].trend_count == 50000
    assert "50,000/24h" in refs[0].line()


def test_trending_returns_empty_on_provider_error():
    from app.providers.base import ProviderError

    class FailingHttp:
        def get(self, path, **kw):
            raise ProviderError("boom")

    assert _trending(FailingHttp(), "add", DUMP) == []


def test_roster_view_splits_starters_and_bench_and_flags_me():
    raw = {"roster_id": 1, "owner_id": "u1", "starters": ["100"], "players": ["100", "200"],
           "settings": {"wins": 2, "losses": 1}}
    users = {"u1": {"display_name": "Me"}}
    rv = _roster_view(raw, users, my_user_id="u1", dump=DUMP)
    assert rv.is_me is True
    assert [p.id for p in rv.starters] == ["100"]
    assert [p.id for p in rv.bench] == ["200"]
    assert rv.record == "2-1"


# ---------------------------------------------------------------------------
# decide.py - the agentic loop against a stubbed OpenAI client
# ---------------------------------------------------------------------------

def _fake_state():
    me = RosterView(roster_id="1", owner_name="Me", is_me=True,
                    starters=[PlayerRef("100", "Puka Nacua", "WR", "LAR")],
                    bench=[PlayerRef("200", "Kyren Williams", "RB", "LAR", "Q")])
    return SimpleNamespace(
        league_id="L1", league_name="I MEAN WE COULDD", season=2026, week=3,
        scoring_format="redraft", waiver_type="faab", waiver_budget_total=100,
        roster_positions=["QB", "RB", "WR"], my_roster=me, opponent=None,
        other_rosters=[], free_agents=[], recent_transactions=[],
        trending_adds=[], trending_drops=[],
    )


class _FakeFunctionCall:
    type = "function_call"
    name = "submit_decisions"

    def __init__(self, data):
        import json as _json
        self.arguments = _json.dumps(data)


class _FakeSearchCall:
    type = "web_search_call"


class _FakeResponse:
    def __init__(self, output, resp_id="resp_1"):
        self.output = output
        self.id = resp_id


def test_decide_returns_decision_once_model_calls_submit_tool(monkeypatch):
    payload = {
        "summary": "Starting Puka, no moves this week.",
        "lineup_changes": [], "waiver_claims": [], "trade_proposals": [], "notes": "",
    }

    calls = []

    class FakeResponses:
        def create(self, **kwargs):
            calls.append(kwargs)
            return _FakeResponse([_FakeSearchCall(), _FakeFunctionCall(payload)])

    class FakeClient:
        def __init__(self, *a, **kw):
            self.responses = FakeResponses()

    monkeypatch.setattr("app.league_manager.decide.OpenAI", FakeClient)

    result = decide(_fake_state())
    assert isinstance(result, Decision)
    assert result.summary == "Starting Puka, no moves this week."
    assert result.has_actions is False
    assert result.searches_used == 1
    assert calls[0]["model"] == "gpt-4.1-mini"


def test_decide_nudges_model_that_stops_without_submitting(monkeypatch):
    payload = {"summary": "ok", "lineup_changes": [], "waiver_claims": [],
               "trade_proposals": [], "notes": ""}
    responses = [
        _FakeResponse([SimpleNamespace(type="text", text="thinking out loud")]),
        _FakeResponse([_FakeSearchCall(), _FakeFunctionCall(payload)]),
    ]

    class FakeResponses:
        def create(self, **kwargs):
            return responses.pop(0)

    class FakeClient:
        def __init__(self, *a, **kw):
            self.responses = FakeResponses()

    monkeypatch.setattr("app.league_manager.decide.OpenAI", FakeClient)

    result = decide(_fake_state())
    assert result.summary == "ok"


def test_decide_demands_a_real_search_before_accepting_zero_search_submission(monkeypatch):
    """The core fix: a model that calls submit_decisions without ever
    searching gets pushed to actually search, exactly once, before its
    answer is accepted - confirmed live to be a real failure mode."""
    payload = {"summary": "confirmed after checking", "lineup_changes": [],
               "waiver_claims": [], "trade_proposals": [], "notes": ""}
    responses = [
        _FakeResponse([_FakeFunctionCall(payload)]),                       # no search - rejected
        _FakeResponse([_FakeSearchCall(), _FakeFunctionCall(payload)]),    # now searched - accepted
    ]
    calls = []

    class FakeResponses:
        def create(self, **kwargs):
            calls.append(kwargs)
            return responses.pop(0)

    class FakeClient:
        def __init__(self, *a, **kw):
            self.responses = FakeResponses()

    monkeypatch.setattr("app.league_manager.decide.OpenAI", FakeClient)

    result = decide(_fake_state())
    assert result.summary == "confirmed after checking"
    assert result.searches_used == 1
    assert len(calls) == 2
    assert "without using web_search" in calls[1]["input"][0]["content"]


# ---------------------------------------------------------------------------
# notify.py - message formatting is pure text, easy to pin down
# ---------------------------------------------------------------------------

def test_format_message_lists_recommended_actions():
    d = Decision(summary="Bench the injured guy.",
                 waiver_claims=[{"add_player_name": "Free Agent", "drop_player_name": "",
                                "faab_bid": 5, "reasoning": "upside"}])
    title, body = format_message("I MEAN WE COULDD", d)
    assert "I MEAN WE COULDD" in title
    assert "Free Agent" in body
    assert "$5 FAAB" in body
    assert "tap them into the Sleeper app yourself" in body


def test_format_message_no_actions_says_so():
    d = Decision(summary="Everything looks right.")
    _, body = format_message("League", d)
    assert "No moves this check-in" in body
