"""Canonical player identity across ESPN and Sleeper.

Strategy, in strict priority order (spec section 16 - do NOT rely on fuzzy names):

  1. Hard ID crosswalk.  Sleeper's player index carries an ``espn_id`` for most
     relevant players, which is an exact join key.
  2. Team defenses are keyed by NFL team abbreviation; ESPN encodes a D/ST as
     player id ``-16000 - proTeamId``, which we decode to the same abbreviation.
  3. Exact (normalized name, position, nfl_team) triple.
  4. Exact (normalized name, position) - only accepted when unambiguous.
  5. Exact normalized name - only accepted when it maps to exactly one player.

Anything that survives none of those becomes its own ESPN-local canonical
player, which is still correct (it just cannot be cross-matched), and is logged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .config import cache_dir
from .models import CanonicalPlayer, normalize_name, normalize_position, normalize_team
from .providers.base import HttpClient, ProviderError

log = logging.getLogger(__name__)

SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"
ESPN_PLAYERS_URL = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{year}/players"
ESPN_PROTEAMS_URL = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{year}"

PLAYERS_TTL = 12 * 3600  # the player universe barely moves during a week

# ESPN defaultPositionId -> position
ESPN_POSITIONS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}

FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DST"}


def espn_dst_team_id(espn_player_id: int) -> Optional[int]:
    """ESPN encodes the Eagles D/ST as -16021 etc.  Returns the proTeamId."""
    if espn_player_id is not None and espn_player_id <= -16000:
        return -espn_player_id - 16000
    return None


@dataclass
class PlayerRegistry:
    """In-memory index of every fantasy-relevant NFL player."""

    players: dict[str, CanonicalPlayer] = field(default_factory=dict)      # key -> player
    _by_espn: dict[int, str] = field(default_factory=dict)
    _by_sleeper: dict[str, str] = field(default_factory=dict)
    _by_name_pos_team: dict[tuple[str, str, str], list[str]] = field(default_factory=dict)
    _by_name_pos: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    _by_name: dict[str, list[str]] = field(default_factory=dict)
    espn_team_abbrev: dict[int, str] = field(default_factory=dict)
    unmatched: list[str] = field(default_factory=list)

    # -- building -----------------------------------------------------------
    def add(self, player: CanonicalPlayer) -> CanonicalPlayer:
        existing = self.players.get(player.key)
        if existing:
            return existing
        self.players[player.key] = player
        if player.espn_id is not None:
            self._by_espn.setdefault(player.espn_id, player.key)
        if player.sleeper_id:
            self._by_sleeper.setdefault(player.sleeper_id, player.key)
        nk = normalize_name(player.name)
        self._by_name_pos_team.setdefault((nk, player.position, player.nfl_team), []).append(player.key)
        self._by_name_pos.setdefault((nk, player.position), []).append(player.key)
        self._by_name.setdefault(nk, []).append(player.key)
        return player

    # -- lookups ------------------------------------------------------------
    def by_sleeper_id(self, sleeper_id: str) -> Optional[CanonicalPlayer]:
        key = self._by_sleeper.get(str(sleeper_id))
        return self.players.get(key) if key else None

    def by_espn_id(self, espn_id: int) -> Optional[CanonicalPlayer]:
        key = self._by_espn.get(int(espn_id))
        return self.players.get(key) if key else None

    def by_team_dst(self, team: str) -> Optional[CanonicalPlayer]:
        return self.players.get(f"dst:{normalize_team(team)}")

    def resolve(
        self,
        *,
        name: str = "",
        position: str = "",
        team: str = "",
        espn_id: Optional[int] = None,
        sleeper_id: Optional[str] = None,
    ) -> Optional[CanonicalPlayer]:
        """Best-effort canonical resolution using the priority ladder above."""
        pos = normalize_position(position)
        tm = normalize_team(team)

        if sleeper_id:
            hit = self.by_sleeper_id(sleeper_id)
            if hit:
                return hit
        if espn_id is not None:
            dst_team = espn_dst_team_id(int(espn_id))
            if dst_team is not None:
                hit = self.by_team_dst(self.espn_team_abbrev.get(dst_team, ""))
                if hit:
                    return hit
            hit = self.by_espn_id(espn_id)
            if hit:
                return hit
        if pos == "DST":
            hit = self.by_team_dst(tm) or self.by_team_dst(name.split()[0] if name else "")
            if hit:
                return hit

        nk = normalize_name(name)
        if not nk:
            return None
        for candidates in (
            self._by_name_pos_team.get((nk, pos, tm)),
            self._by_name_pos.get((nk, pos)),
            self._by_name.get(nk),
        ):
            if candidates and len(set(candidates)) == 1:
                return self.players[candidates[0]]
            if candidates and len(set(candidates)) > 1:
                # Ambiguous duplicate names: break the tie on team, then position.
                for key in candidates:
                    p = self.players[key]
                    if tm and p.nfl_team == tm and (not pos or p.position == pos):
                        return p
        return None

    def search(self, query: str, limit: int = 10) -> list[CanonicalPlayer]:
        """Loose search for the CLI (`fantasy-agent player "Saquon"`)."""
        q = normalize_name(query)
        if not q:
            return []
        exact = [self.players[k] for k in self._by_name.get(q, [])]
        if exact:
            return exact[:limit]
        out = [p for p in self.players.values() if q in normalize_name(p.name)]
        out.sort(key=lambda p: (p.position not in FANTASY_POSITIONS, len(p.name)))
        return out[:limit]

    def ensure_espn(self, espn_id: int, name: str, position: str, team: str) -> CanonicalPlayer:
        """Resolve, or mint an ESPN-local canonical player so nothing is dropped."""
        hit = self.resolve(name=name, position=position, team=team, espn_id=espn_id)
        if hit:
            return hit
        self.unmatched.append(f"espn:{espn_id} {name} ({position}-{team})")
        return self.add(
            CanonicalPlayer(
                key=f"espn:{espn_id}",
                name=name or f"ESPN {espn_id}",
                position=normalize_position(position),
                nfl_team=normalize_team(team),
                espn_id=int(espn_id),
            )
        )


# ---------------------------------------------------------------------------


def build_registry(season: int, *, client: Optional[HttpClient] = None,
                   include_espn: bool = True) -> PlayerRegistry:
    """Download (and cache) the Sleeper + ESPN player universes and index them."""
    http = client or HttpClient(cache_dir=cache_dir(), timeout=60.0)
    reg = PlayerRegistry()

    # --- Sleeper is the spine: it owns the espn_id crosswalk ---------------
    try:
        raw = http.get(SLEEPER_PLAYERS_URL, cache_ttl=PLAYERS_TTL)
    except ProviderError as exc:
        log.error("Could not load the Sleeper player index: %s", exc)
        raw = {}

    for pid, p in (raw or {}).items():
        pos = normalize_position(p.get("position") or "")
        fantasy_pos = {normalize_position(x) for x in (p.get("fantasy_positions") or [])}
        if pos not in FANTASY_POSITIONS and not (fantasy_pos & FANTASY_POSITIONS):
            continue
        if pos not in FANTASY_POSITIONS:
            pos = sorted(fantasy_pos & FANTASY_POSITIONS)[0]
        team = normalize_team(p.get("team") or "")
        if pos == "DST":
            team = team or normalize_team(pid)
            name = f"{p.get('first_name','')} {p.get('last_name','')}".strip() or f"{team} D/ST"
            key = f"dst:{team}"
        else:
            name = (p.get("full_name")
                    or f"{p.get('first_name','')} {p.get('last_name','')}").strip()
            key = f"sleeper:{pid}"
        if not name:
            continue
        espn_id = p.get("espn_id")
        reg.add(
            CanonicalPlayer(
                key=key,
                name=name,
                position=pos,
                nfl_team=team,
                sleeper_id=str(pid),
                espn_id=int(espn_id) if espn_id else None,
            )
        )

    # --- ESPN pro-team abbreviations (needed to decode D/ST ids) -----------
    if include_espn:
        try:
            teams = http.get(ESPN_PROTEAMS_URL.format(year=season),
                             params={"view": "proTeamSchedules_wl"}, cache_ttl=PLAYERS_TTL)
            for t in teams.get("settings", {}).get("proTeams", []):
                if t.get("abbrev"):
                    reg.espn_team_abbrev[int(t["id"])] = normalize_team(t["abbrev"])
        except ProviderError as exc:
            log.warning("ESPN pro-team map unavailable: %s", exc)

        # --- ESPN players fill gaps where Sleeper has no espn_id -----------
        try:
            espn_players = http.get(
                ESPN_PLAYERS_URL.format(year=season),
                params={"view": "players_wl"},
                headers={"x-fantasy-filter": '{"filterActive":{"value":true}}'},
                cache_ttl=PLAYERS_TTL,
            )
        except ProviderError as exc:
            log.warning("ESPN player index unavailable (name matching only): %s", exc)
            espn_players = []

        linked = 0
        for p in espn_players or []:
            pos = ESPN_POSITIONS.get(p.get("defaultPositionId"))
            if not pos:
                continue
            eid = int(p["id"])
            team = reg.espn_team_abbrev.get(p.get("proTeamId", 0), "")
            if pos == "DST":
                dt = espn_dst_team_id(eid)
                team = reg.espn_team_abbrev.get(dt, team) if dt is not None else team
            existing = reg.resolve(name=p.get("fullName", ""), position=pos, team=team, espn_id=eid)
            if existing is None:
                reg.add(CanonicalPlayer(key=f"espn:{eid}", name=p.get("fullName", ""),
                                        position=pos, nfl_team=team, espn_id=eid))
            elif existing.espn_id is None:
                # Back-fill the crosswalk so future ESPN lookups hit by id.
                reg._by_espn.setdefault(eid, existing.key)
                linked += 1
        log.debug("Linked %d ESPN ids by name/team/position", linked)

    log.info("Player registry: %d players (%d with an ESPN id)",
             len(reg.players), len(reg._by_espn))
    return reg
