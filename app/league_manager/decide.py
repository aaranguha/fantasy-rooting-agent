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
#: OpenAI's web_search tool has no per-request search cap (unlike Anthropic's
#: max_uses) - the real bound is MAX_TURNS and each turn's max_output_tokens.
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
                "description": "ONE short sentence for a Telegram message - the headline, not a "
                               "report. E.g. 'Nacua's questionable, everything else is set' or "
                               "'Claiming the Bears' new WR1 off waivers.' Max ~15 words.",
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
                        "reasoning": {"type": "string",
                                     "description": "One short clause, ~10 words max - this goes "
                                                    "inline in a text message, not a paragraph."},
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
                        "reasoning": {"type": "string",
                                     "description": "One short clause, ~10 words max - this goes "
                                                    "inline in a text message, not a paragraph."},
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
                        "reasoning": {"type": "string",
                                     "description": "One short clause, ~15 words max - why this "
                                                    "is fair to both sides."},
                    },
                },
            },
            "notes": {
                "type": "string",
                "description": "Only something genuinely worth flagging that isn't already an "
                               "action - a real injury risk, a bye week, low confidence. Empty "
                               "string in most weeks; when used, one short sentence, not a list.",
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
    if state.trending_adds:
        parts += ["", "TRENDING ADDS RIGHT NOW (across all of Sleeper, last 24h - "
                       "real signal for breakouts, not a search guess):",
                  "; ".join(p.line() for p in state.trending_adds)]
    if state.trending_drops:
        parts += ["", "TRENDING DROPS RIGHT NOW (across all of Sleeper, last 24h):",
                  "; ".join(p.line() for p in state.trending_drops)]
    parts += [
        "",
        f"AVAILABLE FREE AGENTS ({len(state.free_agents)}, most relevant first):",
        "; ".join(p.line() for p in state.free_agents),
    ]
    return "\n".join(parts)


SYSTEM_PROMPT = """\
You are the research engine for a real fantasy football team on Sleeper. \
Nothing you decide executes automatically - the user reads your \
recommendation and acts on it themselves - but be as rigorous as if it did: \
sloppy or unfounded advice wastes their week and, for trade proposals, their \
credibility with a real person in the league.

RESEARCH METHOD (do this before every call, not just when something looks off):

1. Injury status: search for the player's current official designation and \
   the most recent practice report / beat-writer update. Prefer the last \
   2-3 days.
2. Expert consensus, weighted by track record, not volume: search \
   "FantasyPros expert accuracy rankings" and prefer analysts who rank well \
   there over whoever is simply easiest to find. FantasyPros' own consensus \
   rankings already aggregate many well-regarded analysts (including plenty \
   who post primarily on X/Twitter) - treat that consensus as a strong prior, \
   then look for what's changed since it was last updated (injury, role \
   change, matchup).
3. On X/Twitter specifically: you generally CANNOT browse timelines or read \
   individual tweets directly - X blocks most automated access. Don't claim \
   to have "checked Twitter" - instead search for articles, newsletters or \
   aggregator posts that cite or summarize what named analysts are saying, \
   and cite the article, not an imagined tweet.
4. TRENDING ADDS/DROPS in the prompt below are real platform-wide Sleeper \
   data (hundreds of thousands of managers), not a search result - treat a \
   player spiking there as a genuine signal worth investigating further, \
   not proof on its own.
5. Cross-reference at least two independent sources before a waiver add or \
   lineup swap that isn't obvious; for a trade proposal, verify the target \
   player's value from the other manager's likely perspective too, so the \
   offer is genuinely fair, not just fair to you.

Do not invent injury statuses, snap counts, target share, or expert opinions \
you haven't actually found - if you can't find something, say so in `notes` \
rather than guessing. This applies even in a week where you conclude no \
roster moves are needed: use web_search at least once, every run, to confirm \
current injury statuses for your questionable/bench-worthy players before \
concluding the lineup is already right. The injury_status field already in \
this prompt comes from Sleeper's own feed and can lag actual news by hours -
treat it as a starting point to verify, not a substitute for checking.

Only propose a lineup change, waiver claim or trade when you have a real, \
searched-and-verified reason - it is completely fine to submit empty arrays \
for a week where the existing lineup is already right and no move is worth \
making.

When you are done researching, call submit_decisions exactly once with your \
final answer."""


def _find_function_call(output: list, name: str):
    return next((item for item in output if getattr(item, "type", None) == "function_call"
                 and item.name == name), None)


def _count_searches(output: list) -> int:
    return sum(1 for item in output if getattr(item, "type", None) == "web_search_call")


def decide(state: LeagueManagerState, *, model: Optional[str] = None) -> Decision:
    client = OpenAI()
    model = model or os.getenv("LEAGUE_MANAGER_MODEL") or DEFAULT_MODEL
    tools = [{"type": "web_search"}, SUBMIT_TOOL]

    searches_used = 0
    demanded_search = False
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
        # A model that answers from training-data priors instead of actually
        # searching is exactly the failure mode this whole pipeline exists to
        # avoid (confirmed live: it will confidently report injury statuses
        # without ever calling web_search unless pushed). Force at least one
        # real search before accepting a submission, once.
        if submit_call is not None and searches_used == 0 and not demanded_search:
            demanded_search = True
            previous_response_id = response.id
            next_input = [{"role": "user", "content":
                          "You called submit_decisions without using web_search at all. "
                          "Use it at least once now to confirm current injury statuses and "
                          "expert consensus for the players in your decision, then call "
                          "submit_decisions again with whatever you find (same answer is fine "
                          "if research confirms it - the point is confirming, not changing)."}]
            continue
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
