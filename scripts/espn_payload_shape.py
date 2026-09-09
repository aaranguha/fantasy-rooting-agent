#!/usr/bin/env python3
"""Report the SHAPE of ESPN's league payload - keys and types only, no values.

    python scripts/espn_payload_shape.py <leagueId> [week]

Prints the structure of one roster entry so we can see where ESPN is putting the
player id in your league. Player *values* are never printed: only key names and
their JSON types, so the output is safe to paste anywhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import espn_cookies, load_env                      # noqa: E402
from app.providers.base import HttpClient                          # noqa: E402
from app.providers.espn import PATH, READ_HOST, ROSTER_VIEWS       # noqa: E402


def shape(obj, depth=0, max_depth=4):
    """Render keys and types, never values."""
    pad = "  " * depth
    if isinstance(obj, dict):
        if depth >= max_depth:
            return f"{{...{len(obj)} keys...}}"
        lines = []
        for k, v in list(obj.items())[:40]:
            lines.append(f"{pad}  {k}: {shape(v, depth + 1, max_depth)}")
        return "{\n" + "\n".join(lines) + f"\n{pad}}}"
    if isinstance(obj, list):
        if not obj:
            return "[] (empty)"
        return f"[{len(obj)} x {shape(obj[0], depth + 1, max_depth)}]"
    if obj is None:
        return "null"
    return type(obj).__name__


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    league_id, week = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 1

    load_env()
    cookies = espn_cookies()
    print(f"ESPN cookies present: {bool(cookies)}")

    http = HttpClient(cookies=cookies, timeout=30)
    url = READ_HOST + PATH.format(year=2026, league_id=league_id)
    data = http.get(url, params={"view": ROSTER_VIEWS, "scoringPeriodId": week})

    print(f"\ntop-level keys: {sorted(data.keys())}")
    sched = data.get("schedule") or []
    print(f"schedule entries: {len(sched)}")
    if not sched:
        return 1

    match = next((m for m in sched if m.get("matchupPeriodId") == week), sched[0])
    print(f"\nmatchup keys: {sorted(match.keys())}")
    side = match.get("home") or {}
    print(f"home keys: {sorted(side.keys())}")

    for key in ("rosterForCurrentScoringPeriod", "rosterForMatchupPeriod"):
        roster = side.get(key)
        if not roster:
            print(f"\n{key}: ABSENT")
            continue
        entries = roster.get("entries") or []
        print(f"\n{key}: {len(entries)} entries")
        if entries:
            print("first entry SHAPE (keys and types only):")
            print(shape(entries[0], 0, 5))
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
