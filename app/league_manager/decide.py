"""Research + decision-making for one league, via the OpenAI API.

One call does both jobs: the model has the built-in ``web_search`` tool
available to pull current injury news, start/sit advice and waiver targets
from the open web, then reports its decisions by calling the
``submit_decisions`` function tool (forced-schema JSON, not string-parsed).

Model note: gpt-5-nano/gpt-5-mini do NOT support the web_search tool this
pipeline depends on for grounding (confirmed against OpenAI's docs, Sep
2026) - using either would silently turn "decide" into "guess from training
data" for a real fantasy roster. gpt-4.1-mini is the cheapest model that
still supports web_search, at ~$0.40/$1.60 per 1M tokens.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from openai import OpenAI

from .state import LeagueManagerState

log = logging.getLogger(__name__)

#: See the module docstring - nano/mini gpt-5 models can't use web_search.
DEFAULT_MODEL = "gpt-4.1-mini"
MAX_TURNS = 6

SUBMIT_TOOL = {
    "type": "function",
    "name": "submit_decisions",
    "description": "Report this week's roster decisions for the league.",
    "strict": True,
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary", "lineup_changes", "waiver_claims", "trade_proposals", "notes"],
        "properties": {
            "summary": {
                "type": "string",
                "description": "2-4 sentence plain-English summary for a Telegram message: "
                               "what you're doing this week and why.",
            },
            "lineup_changes": {
                "type": "array",
                "description": "Empty array if the current starting lineup is already optimal.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["slot", "start_player_id", "start_player_name",
                                 "bench_player_id", "bench_player_name", "reasoning"],
                    "properties": {
                        "slot": {"type": "string"},
                        "start_player_id": {"type": "string"},
                        "start_player_name": {"type": "string"},
                        "bench_player_id": {"type": "string"},
                        "bench_player_name": {"type": "string"},
                        "reasoning": {"type": "string"},
                    },
                },
            },
            "waiver_claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["add_player_id", "add_player_name", "drop_player_id",
                                 "drop_player_name", "faab_bid", "reasoning"],
                    "properties": {
                        "add_player_id": {"type": "string"},
                        "add_player_name": {"type": "string"},
                        "drop_player_id": {
                            "type": "string",
                            "description": "Empty string if adding without a corresponding drop "
                                           "(only valid when under the roster limit).",
                        },
                        "drop_player_name": {"type": "string"},
                        "faab_bid": {
                            "type": "integer",
                            "description": "Dollars out of the league's FAAB budget, 0 if this "
                                           "is a free rolling-waiver claim instead.",
                        },
                        "reasoning": {"type": "string"},
                    },
                },
            },
            "trade_proposals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["target_roster_id", "target_team_name", "give", "get", "reasoning"],
                    "properties": {
                        "target_roster_id": {"type": "string"},
                        "target_team_name": {"type": "string"},
                        "give": {"type": "array", "items": {"type": "string"},
                                 "description": "Player names you're offering."},
                        "get": {"type": "array", "items": {"type": "string"},
                                "description": "Player names you're asking for."},
                        "reasoning": {"type": "string"},
                    },
                },
            },
            "notes": {
                "type": "string",
                "description": "Anything worth flagging that isn't an action: injury risk on a "
                               "starter, a bye week coming up, low confidence in a call, etc. "
                               "Empty string if nothing.",
            },
        },
    },
}


@dataclass
class Decision:
    summary: str
    lineup_changes: list[dict[str, Any]] = field(default_factory=list)
    waiver_claims: list[dict[str, Any]] = field(default_factory=list)
    trade_proposals: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""
    searches_used: int = 0

    @property
    def has_actions(self) -> bool:
        return bool(self.lineup_changes or self.waiver_claims or self.trade_proposals)


def _format_roster(label: str, roster) -> str:
    lines = [f"{label}: {roster.owner_name} ({roster.record})" if roster.record
             else f"{label}: {roster.owner_name}"]
    lines.append("  Starters: " + "; ".join(p.line() for p in roster.starters))
    lines.append("  Bench: " + ("; ".join(p.line() for p in roster.bench) or "(empty)"))
    return "\n".join(lines)


def _build_prompt(state: LeagueManagerState) -> str:
    parts = [
        f"League: {state.league_name} ({state.scoring_format}, {state.season} week {state.week})",
        f"Roster slots: {', '.join(state.roster_positions)}",
        f"Waivers: {state.waiver_type}"
        + (f", ${state.waiver_budget_total} FAAB budget" if state.waiver_type == "faab" else ""),
        "",
        _format_roster("YOUR TEAM", state.my_roster),
    ]
    if state.opponent:
        parts += ["", _format_roster("THIS WEEK'S OPPONENT", state.opponent)]
    if state.other_rosters:
        parts += ["", "OTHER TEAMS (trade context):"]
        parts += [_format_roster(f"  Team {r.roster_id}", r) for r in state.other_rosters]
    if state.recent_transactions:
        parts += ["", "RECENT LEAGUE TRANSACTIONS:", *state.recent_transactions]
    parts += [
        "",
        f"AVAILABLE FREE AGENTS ({len(state.free_agents)}, most relevant first):",
        "; ".join(p.line() for p in state.free_agents),
    ]
    return "\n".join(parts)


SYSTEM_PROMPT = """\
You are managing a real fantasy football team on Sleeper for the user, with \
full autonomy to set lineups, submit waiver claims and propose trades - no \
one reviews your decisions before they go out, so be genuinely careful and \
conservative rather than aggressive for its own sake.

Use the web_search tool to ground every call in current information: injury \
designations, beat-reporter practice reports, published start/sit rankings \
and waiver-wire targets from mainstream fantasy analysts (e.g. FantasyPros \
consensus rankings, ESPN, The Athletic, Rotoworld/Rotowire, NFL.com beat \
writers). Prefer information from the last few days. Do not invent injury \
statuses, snap counts or expert opinions you haven't actually found - if you \
can't find something, say so in `notes` rather than guessing.

Only propose a lineup change, waiver claim or trade when you have a real, \
searched-and-verified reason - it is completely fine to submit empty arrays \
for a week where the existing lineup is already right and no move is worth \
making. A trade proposal goes to a real person in this league, so only \
propose one that is genuinely fair to both sides; do not lowball.

When you are done researching, call submit_decisions exactly once with your \
final answer."""


def _find_function_call(output: list, name: str):
    return next((item for item in output if getattr(item, "type", None) == "function_call"
                 and item.name == name), None)


def _count_searches(output: list) -> int:
    return sum(1 for item in output if getattr(item, "type", None) == "web_search_call")


def decide(state: LeagueManagerState, *, model: Optional[str] = None) -> Decision:
    client = OpenAI()
    model = model or os.getenv("LEAGUE_MANAGER_MODEL", DEFAULT_MODEL)
    tools = [{"type": "web_search"}, SUBMIT_TOOL]

    searches_used = 0
    previous_response_id: Optional[str] = None
    next_input = [{"role": "user", "content": _build_prompt(state)}]

    for turn in range(MAX_TURNS):
        response = client.responses.create(
            model=model,
            instructions=SYSTEM_PROMPT,
            input=next_input,
            tools=tools,
            max_output_tokens=8000,
            previous_response_id=previous_response_id,
        )
        searches_used += _count_searches(response.output)

        submit_call = _find_function_call(response.output, "submit_decisions")
        if submit_call is not None:
            data = json.loads(submit_call.arguments)
            return Decision(
                summary=data.get("summary", ""),
                lineup_changes=data.get("lineup_changes") or [],
                waiver_claims=data.get("waiver_claims") or [],
                trade_proposals=data.get("trade_proposals") or [],
                notes=data.get("notes", ""),
                searches_used=searches_used,
            )

        previous_response_id = response.id
        next_input = [{"role": "user", "content": "Call submit_decisions now with your final answer."}]

    log.warning("league_manager: model never called submit_decisions after %d turns for %s",
                MAX_TURNS, state.league_id)
    return Decision(summary="No decision reached - the model didn't finish in time. "
                             "Check the run log.", searches_used=searches_used)
