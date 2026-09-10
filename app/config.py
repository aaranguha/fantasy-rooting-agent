"""Configuration: non-secret league setup in JSON, secrets in the environment.

Rule enforced throughout: espn_s2 / SWID / bot tokens / ntfy topics live ONLY in
environment variables (loaded from a gitignored .env).  config.json holds league
ids, buy-ins and preferences and is *also* gitignored for good measure.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .models import League, Platform, ScoringSettings

log = logging.getLogger(__name__)

DEFAULT_TZ = "America/Los_Angeles"
DEFAULT_MINUTES_BEFORE = 15


def agent_home() -> Path:
    """Directory holding config.json, the SQLite db and the cache."""
    env = os.getenv("FANTASY_AGENT_HOME")
    path = Path(env).expanduser() if env else Path.home() / ".fantasy-agent"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_env() -> None:
    """Load .env from the project dir first, then ~/.fantasy-agent/.env."""
    load_dotenv(Path.cwd() / ".env")
    load_dotenv(agent_home() / ".env")


def config_path() -> Path:
    return agent_home() / "config.json"


def db_path() -> Path:
    return agent_home() / "agent.sqlite"


def cache_dir() -> Path:
    d = agent_home() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------


@dataclass
class LeagueConfig:
    """What we persist per league.  Live data is always re-fetched."""

    platform: str
    league_id: str
    name: str = ""
    buy_in_usd: float = 0.0
    importance_multiplier: float = 1.0
    my_team_id: str = ""
    season: int = 0
    notes: str = ""
    #: Set > 0 for leagues with a last-place punishment. 1.0 = normal dread,
    #: 1.5 = genuinely humiliating. Drives the downside half of the season
    #: multiplier, which ramps as the regular season runs out.
    loser_punishment: float = 0.0
    #: How many teams eat that punishment (last place only, by default).
    punished_places: int = 1
    #: Scale this league's stake by the standings each week.
    dynamic_importance: bool = True
    #: A league you still want mentioned but never want crowding your notifications.
    #: Its players still get full analysis (`player`, `game`, the dashboard) - only
    #: the push condenses them to a footer line instead of a full block.
    low_priority: bool = False

    @property
    def effective_weight(self) -> float:
        """The STATIC weight. The live one includes the season multiplier and
        lives on the League object once standings have loaded."""
        base = self.buy_in_usd if self.buy_in_usd > 0 else 10.0
        return round(base * self.importance_multiplier, 2)

    def to_league(self) -> League:
        return League(
            id=self.league_id,
            name=self.name or self.league_id,
            platform=Platform(self.platform),
            buy_in_usd=self.buy_in_usd,
            importance_multiplier=self.importance_multiplier,
            season=self.season,
            my_team_id=self.my_team_id or None,
            scoring=ScoringSettings(),
            notes=self.notes,
            low_priority=self.low_priority,
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LeagueConfig":
        return cls(
            platform=d["platform"],
            league_id=str(d["league_id"]),
            name=d.get("name", ""),
            buy_in_usd=float(d.get("buy_in_usd", 0) or 0),
            importance_multiplier=float(d.get("importance_multiplier", 1.0) or 1.0),
            my_team_id=str(d.get("my_team_id", "") or ""),
            season=int(d.get("season", 0) or 0),
            notes=d.get("notes", ""),
            loser_punishment=float(d.get("loser_punishment", 0) or 0),
            punished_places=int(d.get("punished_places", 1) or 1),
            dynamic_importance=bool(d.get("dynamic_importance", True)),
            low_priority=bool(d.get("low_priority", False)),
        )


@dataclass
class AppConfig:
    leagues: list[LeagueConfig] = field(default_factory=list)
    sleeper_username: str = ""
    sleeper_user_id: str = ""
    timezone: str = DEFAULT_TZ
    minutes_before: int = DEFAULT_MINUTES_BEFORE
    notifier: str = "console"
    season: int = 0
    include_slots: list[str] = field(
        default_factory=lambda: ["TNF", "SNF", "MNF", "PRIMETIME", "HOLIDAY", "INTERNATIONAL"]
    )
    #: Send a "here's what's on tonight" digest at this local time on any day
    #: that has a primetime game. HH:MM, 24-hour. Set morning_summary=False to
    #: disable it entirely; the T-minus-kickoff push is unaffected either way.
    morning_summary: bool = True
    morning_summary_time: str = "09:00"
    #: Separate time for the Sunday early+late slate digest - Sunday has its
    #: own condensed morning coverage (see sunday_slate.py) rather than a
    #: per-game SNF preview, so it gets its own configurable time.
    sunday_morning_time: str = "09:23"
    #: Watch Bluesky for a sudden spike in chatter about one of your players
    #: (injury / big play / trade-type news) and push a "here's why he's
    #: trending" note. Uses the free unauthenticated search API.
    bluesky_buzz: bool = True

    # -- derived ------------------------------------------------------------
    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except Exception:  # pragma: no cover - bad tz in config
            log.warning("Unknown timezone %r, falling back to %s", self.timezone, DEFAULT_TZ)
            return ZoneInfo(DEFAULT_TZ)

    @property
    def total_buy_in(self) -> float:
        return sum(l.buy_in_usd for l in self.leagues)

    def espn_leagues(self) -> list[LeagueConfig]:
        return [l for l in self.leagues if l.platform == Platform.ESPN.value]

    def sleeper_leagues(self) -> list[LeagueConfig]:
        return [l for l in self.leagues if l.platform == Platform.SLEEPER.value]

    # -- persistence --------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "sleeper_username": self.sleeper_username,
            "sleeper_user_id": self.sleeper_user_id,
            "timezone": self.timezone,
            "minutes_before": self.minutes_before,
            "notifier": self.notifier,
            "season": self.season,
            "include_slots": self.include_slots,
            "morning_summary": self.morning_summary,
            "morning_summary_time": self.morning_summary_time,
            "sunday_morning_time": self.sunday_morning_time,
            "bluesky_buzz": self.bluesky_buzz,
            "leagues": [l.__dict__ for l in self.leagues],
        }

    def save(self, path: Optional[Path] = None) -> Path:
        p = path or config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2))
        tmp.replace(p)
        os.chmod(p, 0o600)
        return p

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "AppConfig":
        p = path or config_path()
        if not p.exists():
            return cls(
                timezone=os.getenv("TIMEZONE", DEFAULT_TZ),
                minutes_before=int(os.getenv("NOTIFICATION_MINUTES_BEFORE", DEFAULT_MINUTES_BEFORE)),
                notifier=os.getenv("NOTIFIER", "console"),
            )
        d = json.loads(p.read_text())
        cfg = cls(
            sleeper_username=d.get("sleeper_username", ""),
            sleeper_user_id=d.get("sleeper_user_id", ""),
            timezone=d.get("timezone", DEFAULT_TZ),
            minutes_before=int(d.get("minutes_before", DEFAULT_MINUTES_BEFORE)),
            notifier=d.get("notifier", "console"),
            season=int(d.get("season", 0) or 0),
            leagues=[LeagueConfig.from_dict(x) for x in d.get("leagues", [])],
        )
        if d.get("include_slots"):
            cfg.include_slots = d["include_slots"]
        cfg.morning_summary = bool(d.get("morning_summary", True))
        cfg.morning_summary_time = d.get("morning_summary_time", "09:00")
        cfg.sunday_morning_time = d.get("sunday_morning_time", "09:23")
        cfg.bluesky_buzz = bool(d.get("bluesky_buzz", True))
        # Environment always wins for runtime knobs, so launchd can override.
        cfg.timezone = os.getenv("TIMEZONE") or cfg.timezone
        if os.getenv("NOTIFICATION_MINUTES_BEFORE"):
            cfg.minutes_before = int(os.environ["NOTIFICATION_MINUTES_BEFORE"])
        cfg.notifier = os.getenv("NOTIFIER") or cfg.notifier
        if os.getenv("MORNING_SUMMARY_TIME"):
            cfg.morning_summary_time = os.environ["MORNING_SUMMARY_TIME"]
        if os.getenv("SUNDAY_MORNING_TIME"):
            cfg.sunday_morning_time = os.environ["SUNDAY_MORNING_TIME"]
        if os.getenv("MORNING_SUMMARY") is not None:
            cfg.morning_summary = os.environ["MORNING_SUMMARY"].strip().lower() not in (
                "0", "false", "no", "")
        if os.getenv("BLUESKY_BUZZ") is not None:
            cfg.bluesky_buzz = os.environ["BLUESKY_BUZZ"].strip().lower() not in (
                "0", "false", "no", "")
        return cfg

    @property
    def is_configured(self) -> bool:
        return bool(self.leagues)


# ---------------------------------------------------------------------------
# Secrets (read-only helpers - never written to disk by this app)
# ---------------------------------------------------------------------------


def espn_cookies() -> dict[str, str]:
    """ESPN auth cookies from the environment.  Empty dict = public leagues only."""
    s2 = os.getenv("ESPN_S2", "").strip()
    swid = os.getenv("ESPN_SWID", "").strip()
    if not s2 or not swid:
        return {}
    if not swid.startswith("{"):
        swid = "{" + swid.strip("{}") + "}"
    return {"espn_s2": s2, "SWID": swid}


def setup_logging(level: Optional[str] = None) -> None:
    logging.basicConfig(
        level=getattr(logging, (level or os.getenv("LOG_LEVEL", "INFO")).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
