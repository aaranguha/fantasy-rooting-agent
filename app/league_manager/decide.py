"""Research + decision-making for one league, via the Claude API.

One call does both jobs: Claude has the ``web_search`` server tool available
to pull current injury news, start/sit advice and waiver targets from the
open web, then reports its decisions by calling the ``submit_decisions``
tool (forced JSON via a schema, not string-parsed).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

import anthropic

from .state import LeagueManagerState

log = logging.getLogger(__name__)

#: Opus 5 is the default per house policy; export
#: LEAGUE_MANAGER_MODEL=claude-sonnet-5 to cut cost ~2.5x if the weekly
#: research bill matters more than the last bit of judgment.
DEFAULT_MODEL = "claude-opus-5"
MAX_SEARCHES_PER_RUN = 12

SUBMIT_TOOL = {
    "name": "submit_decisions",
    "description": "Report this week's roster decisions for the league.",
    "input_schema": {
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


def decide(state: LeagueManagerState, *, model: Optional[str] = None) -> Decision:
    client = anthropic.Anthropic()
    model = model or os.getenv("LEAGUE_MANAGER_MODEL", DEFAULT_MODEL)

    messages: list[dict[str, Any]] = [{"role": "user", "content": _build_prompt(state)}]
    tools = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": MAX_SEARCHES_PER_RUN},
        SUBMIT_TOOL,
    ]

    searches_used = 0
    # Bounded agentic loop: Claude researches via the server-run web_search
    # tool (no client action needed for that one) until it calls
    # submit_decisions, or we run out of turns and force the issue.
    for turn in range(6):
        response = client.messages.create(
            model=model,
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
        )

        for block in response.content:
            if block.type == "web_search_tool_result":
                searches_used += 1

        submit_block = next(
            (b for b in response.content if b.type == "tool_use" and b.name == "submit_decisions"),
            None,
        )
        if submit_block is not None:
            data = submit_block.input
            return Decision(
                summary=data.get("summary", ""),
                lineup_changes=data.get("lineup_changes") or [],
                waiver_claims=data.get("waiver_claims") or [],
                trade_proposals=data.get("trade_proposals") or [],
                notes=data.get("notes", ""),
                searches_used=searches_used,
            )

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "end_turn":
            # It stopped without calling submit_decisions - nudge it.
            messages.append({"role": "user", "content": "Call submit_decisions now with your final answer."})
        # stop_reason == "tool_use" for web_search is handled server-side already;
        # nothing else for the client to do before the next turn.

    log.warning("league_manager: model never called submit_decisions after 6 turns for %s",
                state.league_id)
    return Decision(summary="No decision reached - the model didn't finish in time. "
                             "Check the run log.", searches_used=searches_used)
