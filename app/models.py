"""Normalized domain model shared by every provider, analyzer and notifier.

ESPN and Sleeper speak very different dialects.  Everything in this file is
platform-agnostic: providers translate *into* these objects and nothing
downstream ever needs to know where the data came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # avoids a circular import at runtime
    from .standings import SeasonOutlook

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Platform(str, Enum):
    ESPN = "espn"
    SLEEPER = "sleeper"


class Side(str, Enum):
    """Which side of *my* matchup a player exposure sits on."""

    MINE = "mine"
    OPPONENT = "opponent"


class LineupStatus(str, Enum):
    STARTER = "starter"
    BENCH = "bench"
    IR = "ir"
    TAXI = "taxi"


class PlayerGameState(str, Enum):
    """State of the NFL game a fantasy player is involved in."""

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    FINAL = "final"
    BYE_OR_UNKNOWN = "unknown"


class Confidence(str, Enum):
    """How much we trust a computed points threshold (spec section 7)."""

    EXACT = "EXACT"
    HIGH = "HIGH CONFIDENCE"
    PROJECTION = "PROJECTION-BASED"
    LOW = "LOW CONFIDENCE"


class SlotType(str, Enum):
    TNF = "TNF"
    SNF = "SNF"
    MNF = "MNF"
    PRIMETIME = "PRIMETIME"          # standalone national game, non TNF/SNF/MNF
    INTERNATIONAL = "INTERNATIONAL"  # standalone early London/Germany window
    HOLIDAY = "HOLIDAY"              # Thanksgiving / Christmas standalone
    REGULAR = "REGULAR"


class RootingCategory(str, Enum):
    ROOT_HARD_FOR = "ROOT HARD FOR"
    ROOT_FOR = "ROOT FOR"
    SLIGHT_ROOT_FOR = "SLIGHT ROOT FOR"
    CONFLICTED = "CONFLICTED / IDEAL RANGE"
    SLIGHT_ROOT_AGAINST = "SLIGHT ROOT AGAINST"
    ROOT_AGAINST = "ROOT AGAINST"
    PUBLIC_ENEMY = "PUBLIC ENEMY #1"
    NEUTRAL = "DOESN'T MATTER"

    @property
    def emoji(self) -> str:
        return {
            RootingCategory.ROOT_HARD_FOR: "\U0001f525",       # fire
            RootingCategory.ROOT_FOR: "\U0001f7e2",            # green circle
            RootingCategory.SLIGHT_ROOT_FOR: "\U0001f610",     # neutral face
            RootingCategory.CONFLICTED: "⚖️",        # scales
            RootingCategory.SLIGHT_ROOT_AGAINST: "\U0001f610",
            RootingCategory.ROOT_AGAINST: "\U0001f534",        # red circle
            RootingCategory.PUBLIC_ENEMY: "☠️",      # skull
            RootingCategory.NEUTRAL: "⚪",                 # white circle
        }[self]

    @property
    def is_positive(self) -> bool:
        return self in (
            RootingCategory.ROOT_HARD_FOR,
            RootingCategory.ROOT_FOR,
            RootingCategory.SLIGHT_ROOT_FOR,
        )

    @property
    def is_negative(self) -> bool:
        return self in (
            RootingCategory.ROOT_AGAINST,
            RootingCategory.PUBLIC_ENEMY,
            RootingCategory.SLIGHT_ROOT_AGAINST,
        )


class DataTier(str, Enum):
    """Graceful-degradation tiers (spec section 25)."""

    BEST = "scores + projections + league scoring + weights"
    GOOD = "scores + projections + weights"
    PROJECTIONS_ONLY = "projections + weights"
    MINIMUM = "starter exposure + weights"


# ---------------------------------------------------------------------------
# Player identity
# ---------------------------------------------------------------------------

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
_PUNCT = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")

# Teams that have relocated / been renamed; normalize to the current abbrev.
TEAM_ALIASES = {
    "JAC": "JAX", "WAS": "WSH", "LA": "LAR", "SD": "LAC", "OAK": "LV",
    "STL": "LAR", "ARZ": "ARI", "BLT": "BAL", "CLV": "CLE", "HST": "HOU",
    "SL": "LAR", "LVR": "LV", "NWE": "NE", "NOR": "NO", "GNB": "GB",
    "KAN": "KC", "SFO": "SF", "TAM": "TB", "FA": "", "NONE": "",
}


def normalize_team(team: Optional[str]) -> str:
    if not team:
        return ""
    t = team.strip().upper()
    return TEAM_ALIASES.get(t, t)


def normalize_name(name: Optional[str]) -> str:
    """Aggressive name key: lowercase, strip punctuation and generational suffix.

    "A.J. Brown" -> "ajbrown";  "Marvin Harrison Jr." -> "marvinharrison";
    "Ja'Marr Chase" -> "jamarrchase".
    """
    if not name:
        return ""
    n = name.lower().replace("&", " and ")
    n = _PUNCT.sub(" ", n)
    parts = [p for p in _WS.split(n.strip()) if p]
    while len(parts) > 1 and parts[-1] in _SUFFIXES:
        parts.pop()
    return "".join(parts)


def normalize_position(pos: Optional[str]) -> str:
    """Collapse platform-specific position spellings."""
    if not pos:
        return ""
    p = pos.strip().upper()
    if p in ("DST", "D/ST", "DEF", "D"):
        return "DST"
    if p in ("PK", "K"):
        return "K"
    if p in ("FB",):
        return "RB"
    return p


@dataclass(frozen=True)
class CanonicalPlayer:
    """One real NFL human (or team defense), independent of platform."""

    key: str                 # stable canonical id, e.g. "sleeper:4034" or "dst:PHI"
    name: str
    position: str
    nfl_team: str
    sleeper_id: Optional[str] = None
    espn_id: Optional[int] = None

    @property
    def short_name(self) -> str:
        """'Saquon Barkley' -> 'S. Barkley'; DSTs keep their team name."""
        if self.position == "DST":
            return self.name
        bits = self.name.split()
        if len(bits) < 2:
            return self.name
        return f"{bits[0][0]}. {' '.join(bits[1:])}"

    @property
    def name_key(self) -> str:
        return normalize_name(self.name)

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.name} ({self.position}-{self.nfl_team})"


# ---------------------------------------------------------------------------
# Leagues
# ---------------------------------------------------------------------------


@dataclass
class ScoringSettings:
    """Just enough of a league's scoring rules to (a) score raw stat lines and
    (b) tell the user when two leagues are not directly comparable."""

    raw: dict[str, float] = field(default_factory=dict)
    ppr: float = 1.0                  # points per reception
    pass_td: float = 4.0
    te_premium: float = 0.0
    label: str = "Unknown"

    @property
    def format_name(self) -> str:
        if self.ppr >= 0.99:
            base = "PPR"
        elif self.ppr >= 0.4:
            base = "Half-PPR"
        elif self.ppr <= 0.01:
            base = "Standard"
        else:
            base = f"{self.ppr:g}-PPR"
        extras = []
        if abs(self.pass_td - 4.0) > 0.01:
            extras.append(f"{self.pass_td:g}pt pass TD")
        if self.te_premium > 0.01:
            extras.append(f"TE+{self.te_premium:g}")
        return base + (" (" + ", ".join(extras) + ")" if extras else "")

    def comparable_to(self, other: "ScoringSettings") -> bool:
        """True when a raw point total means materially the same thing in both."""
        return (
            abs(self.ppr - other.ppr) < 0.25
            and abs(self.pass_td - other.pass_td) < 1.0
            and abs(self.te_premium - other.te_premium) < 0.5
        )


@dataclass
class League:
    id: str
    name: str
    platform: Platform
    buy_in_usd: float = 0.0
    importance_multiplier: float = 1.0
    season: int = 0
    scoring: ScoringSettings = field(default_factory=ScoringSettings)
    my_team_id: Optional[str] = None
    my_team_name: str = ""
    # Season context: how much this league still matters *this week*.
    season_multiplier: float = 1.0
    season_note: str = ""
    outlook: Optional["SeasonOutlook"] = None
    # ESPN-only extras
    espn_season: Optional[int] = None
    notes: str = ""
    #: A league whose players you still want mentioned, but never want driving a
    #: verdict or crowding a push. Weight math is untouched (still fully honest
    #: buy-in x multiplier x season); this only tells rendering to demote it.
    low_priority: bool = False

    @property
    def base_stake(self) -> float:
        """A $0 buy-in is worth a nominal $10 so its multiplier still means something."""
        return self.buy_in_usd if self.buy_in_usd > 0 else 10.0

    @property
    def static_weight(self) -> float:
        """buy_in x your manual multiplier - the part that never changes."""
        return round(self.base_stake * self.importance_multiplier, 2)

    @property
    def effective_weight(self) -> float:
        """buy_in x manual multiplier x season multiplier.

        Deliberately transparent (spec section 2): three numbers you can read off
        `fantasy-agent leagues`, no hidden score.  The season multiplier is 1.0
        until standings are loaded, so this reduces to the static weight.
        """
        return round(self.static_weight * self.season_multiplier, 2)

    @property
    def label(self) -> str:
        return f"${self.buy_in_usd:g} {self.name}" if self.buy_in_usd else self.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "platform": self.platform.value,
            "buy_in_usd": self.buy_in_usd,
            "importance_multiplier": self.importance_multiplier,
            "season": self.season,
            "my_team_id": self.my_team_id,
            "my_team_name": self.my_team_name,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Exposures + matchup state
# ---------------------------------------------------------------------------


@dataclass
class FantasyPlayerExposure:
    """One player, in one league, on one side of my matchup."""

    canonical: CanonicalPlayer
    league: League
    side: Side
    lineup_status: LineupStatus
    slot: str = ""                       # league slot label, e.g. "RB", "FLEX"
    current_points: float = 0.0          # points scored so far, league scoring
    projected_points: float = 0.0        # projected FINAL total, league scoring
    game_state: PlayerGameState = PlayerGameState.BYE_OR_UNKNOWN
    game_fraction_remaining: float = 1.0  # 1.0 = kickoff pending, 0.0 = final
    has_projection: bool = True

    @property
    def is_starter(self) -> bool:
        return self.lineup_status == LineupStatus.STARTER

    @property
    def expected_final(self) -> float:
        """Best estimate of this player's FINAL score for the week.

        Finished -> what they actually scored.  Not started -> the projection.
        In progress -> points banked plus the projected share of the game left.
        """
        if self.game_state == PlayerGameState.FINAL:
            return self.current_points
        if self.game_state == PlayerGameState.IN_PROGRESS:
            return self.current_points + self.projected_points * self.game_fraction_remaining
        return max(self.current_points, self.projected_points)

    @property
    def unresolved_points(self) -> float:
        """How much of this player's expected score is still unknown.

        Used to grade threshold confidence: a matchup whose only unresolved
        player is our target yields an EXACT threshold.
        """
        if self.game_state == PlayerGameState.FINAL:
            return 0.0
        if self.game_state == PlayerGameState.IN_PROGRESS:
            return self.projected_points * self.game_fraction_remaining
        return self.projected_points


@dataclass
class MatchupState:
    """My head-to-head matchup in a single league, for a single week."""

    league: League
    week: int
    opponent_name: str = "Opponent"
    my_starters: list[FantasyPlayerExposure] = field(default_factory=list)
    opp_starters: list[FantasyPlayerExposure] = field(default_factory=list)
    my_bench: list[FantasyPlayerExposure] = field(default_factory=list)
    opp_bench: list[FantasyPlayerExposure] = field(default_factory=list)
    reported_score_mine: Optional[float] = None      # platform's own live score
    reported_score_opponent: Optional[float] = None
    error: Optional[str] = None
    tier: DataTier = DataTier.BEST

    # -- live scores --------------------------------------------------------
    @property
    def current_score_mine(self) -> float:
        if self.reported_score_mine is not None:
            return self.reported_score_mine
        return sum(p.current_points for p in self.my_starters)

    @property
    def current_score_opponent(self) -> float:
        if self.reported_score_opponent is not None:
            return self.reported_score_opponent
        return sum(p.current_points for p in self.opp_starters)

    # -- projected finals ---------------------------------------------------
    @property
    def projected_final_mine(self) -> float:
        return sum(p.expected_final for p in self.my_starters)

    @property
    def projected_final_opponent(self) -> float:
        return sum(p.expected_final for p in self.opp_starters)

    @property
    def projected_margin(self) -> float:
        return self.projected_final_mine - self.projected_final_opponent

    # -- remaining ----------------------------------------------------------
    @property
    def remaining_players_mine(self) -> list[FantasyPlayerExposure]:
        return [p for p in self.my_starters if p.game_state != PlayerGameState.FINAL]

    @property
    def remaining_players_opponent(self) -> list[FantasyPlayerExposure]:
        return [p for p in self.opp_starters if p.game_state != PlayerGameState.FINAL]

    @property
    def unresolved_points_total(self) -> float:
        return sum(p.unresolved_points for p in self.my_starters + self.opp_starters)

    def all_starters(self) -> list[FantasyPlayerExposure]:
        return self.my_starters + self.opp_starters

    def find(self, player_key: str) -> list[FantasyPlayerExposure]:
        return [e for e in self.all_starters() if e.canonical.key == player_key]


# ---------------------------------------------------------------------------
# NFL schedule
# ---------------------------------------------------------------------------


@dataclass
class NFLGame:
    id: str
    away: str
    home: str
    kickoff: datetime               # timezone-aware, UTC
    slot: SlotType = SlotType.REGULAR
    broadcast: str = ""
    week: int = 0
    season: int = 0
    season_type: int = 2
    state: PlayerGameState = PlayerGameState.NOT_STARTED
    period: int = 0
    clock: str = ""
    away_score: int = 0
    home_score: int = 0
    name: str = ""

    @property
    def matchup(self) -> str:
        return f"{self.away} @ {self.home}"

    @property
    def teams(self) -> set[str]:
        return {self.away, self.home}

    @property
    def is_primetime(self) -> bool:
        return self.slot != SlotType.REGULAR

    @property
    def fraction_remaining(self) -> float:
        """Rough share of the game's fantasy production still to come."""
        if self.state == PlayerGameState.FINAL:
            return 0.0
        if self.state == PlayerGameState.NOT_STARTED:
            return 1.0
        # 4 quarters of 15 minutes; overtime counts as ~0.
        elapsed = max(0, min(4, self.period - 1)) * 15.0
        secs = _clock_seconds(self.clock)
        if secs is not None and 1 <= self.period <= 4:
            elapsed += 15.0 - secs / 60.0
        return max(0.0, min(1.0, 1.0 - elapsed / 60.0))


def _clock_seconds(clock: str) -> Optional[float]:
    if not clock or ":" not in clock:
        return None
    try:
        mm, ss = clock.split(":")[:2]
        return int(mm) * 60 + int(ss)
    except ValueError:
        return None
