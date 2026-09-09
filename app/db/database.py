"""SQLite persistence: notification de-duplication and a send log.

The only thing that truly must survive a restart is "did I already tell him about
this game?"  A game is recorded ONLY after a successful send, so a crash mid-send
results in a retry rather than a silently skipped notification.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from ..config import db_path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sent_notifications (
    game_id     TEXT NOT NULL,
    season      INTEGER NOT NULL DEFAULT 0,
    week        INTEGER NOT NULL DEFAULT 0,
    slot        TEXT NOT NULL DEFAULT '',
    matchup     TEXT NOT NULL DEFAULT '',
    kickoff_utc TEXT NOT NULL DEFAULT '',
    sent_at_utc TEXT NOT NULL,
    provider    TEXT NOT NULL DEFAULT '',
    body_hash   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (game_id, season, week)
);
CREATE TABLE IF NOT EXISTS send_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc      TEXT NOT NULL,
    game_id     TEXT NOT NULL DEFAULT '',
    provider    TEXT NOT NULL DEFAULT '',
    ok          INTEGER NOT NULL DEFAULT 0,
    attempts    INTEGER NOT NULL DEFAULT 0,
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_send_log_ts ON send_log(ts_utc);
CREATE TABLE IF NOT EXISTS live_snapshots (
    game_id     TEXT NOT NULL,
    season      INTEGER NOT NULL DEFAULT 0,
    week        INTEGER NOT NULL DEFAULT 0,
    payload     TEXT NOT NULL,
    updated_utc TEXT NOT NULL,
    PRIMARY KEY (game_id, season, week)
);
CREATE TABLE IF NOT EXISTS morning_pushes (
    game_id     TEXT NOT NULL,
    season      INTEGER NOT NULL DEFAULT 0,
    week        INTEGER NOT NULL DEFAULT 0,
    provider    TEXT NOT NULL DEFAULT '',
    message_id  TEXT NOT NULL DEFAULT '',
    sent_at_utc TEXT NOT NULL,
    PRIMARY KEY (game_id, season, week)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(str(self.path), timeout=15)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    # -- de-duplication -----------------------------------------------------
    def was_sent(self, game_id: str, season: int = 0, week: int = 0) -> bool:
        with self.connect() as con:
            row = con.execute(
                "SELECT 1 FROM sent_notifications WHERE game_id=? AND season=? AND week=?",
                (str(game_id), season, week),
            ).fetchone()
        return row is not None

    def mark_sent(self, *, game_id: str, season: int = 0, week: int = 0, slot: str = "",
                  matchup: str = "", kickoff_utc: str = "", provider: str = "",
                  body: str = "") -> None:
        """Record a *successful* send.  Idempotent."""
        with self.connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO sent_notifications "
                "(game_id, season, week, slot, matchup, kickoff_utc, sent_at_utc, provider, body_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (str(game_id), season, week, slot, matchup, kickoff_utc, _now(), provider,
                 hashlib.sha256(body.encode()).hexdigest()[:16]),
            )

    def unmark(self, game_id: str, season: int = 0, week: int = 0) -> None:
        with self.connect() as con:
            con.execute(
                "DELETE FROM sent_notifications WHERE game_id=? AND season=? AND week=?",
                (str(game_id), season, week),
            )

    # -- live snapshots -----------------------------------------------------
    def get_snapshot(self, game_id: str, season: int = 0, week: int = 0) -> Optional[dict]:
        with self.connect() as con:
            row = con.execute(
                "SELECT payload FROM live_snapshots WHERE game_id=? AND season=? AND week=?",
                (str(game_id), season, week),
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["payload"])
        except (ValueError, TypeError):  # pragma: no cover - corrupt row
            return None

    def save_snapshot(self, game_id: str, payload: dict,
                      season: int = 0, week: int = 0) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO live_snapshots "
                "(game_id, season, week, payload, updated_utc) VALUES (?,?,?,?,?)",
                (str(game_id), season, week, json.dumps(payload), _now()),
            )

    def clear_snapshots(self, season: int = 0, week: int = 0) -> int:
        with self.connect() as con:
            cur = con.execute(
                "DELETE FROM live_snapshots WHERE season=? AND week=?", (season, week))
            return cur.rowcount

    # -- morning-push tracking (so the kickoff push can swap it out) --------
    def save_morning_push(self, game_id: str, season: int, week: int,
                          provider: str, message_id: Optional[str]) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO morning_pushes "
                "(game_id, season, week, provider, message_id, sent_at_utc) "
                "VALUES (?,?,?,?,?,?)",
                (str(game_id), season, week, provider, message_id or "", _now()),
            )

    def get_morning_push(self, game_id: str, season: int = 0,
                         week: int = 0) -> Optional[dict[str, Any]]:
        with self.connect() as con:
            row = con.execute(
                "SELECT provider, message_id FROM morning_pushes "
                "WHERE game_id=? AND season=? AND week=?",
                (str(game_id), season, week),
            ).fetchone()
        return dict(row) if row else None

    def clear_morning_push(self, game_id: str, season: int = 0, week: int = 0) -> None:
        with self.connect() as con:
            con.execute(
                "DELETE FROM morning_pushes WHERE game_id=? AND season=? AND week=?",
                (str(game_id), season, week),
            )

    # -- logging ------------------------------------------------------------
    def log_send(self, game_id: str, provider: str, ok: bool, attempts: int, detail: str) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT INTO send_log (ts_utc, game_id, provider, ok, attempts, detail) "
                "VALUES (?,?,?,?,?,?)",
                (_now(), str(game_id), provider, int(ok), attempts, detail[:500]),
            )

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM send_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def sent_this_week(self, season: int, week: int) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM sent_notifications WHERE season=? AND week=? ORDER BY kickoff_utc",
                (season, week),
            ).fetchall()
        return [dict(r) for r in rows]

    def reset_week(self, season: int, week: int) -> int:
        with self.connect() as con:
            cur = con.execute(
                "DELETE FROM sent_notifications WHERE season=? AND week=?", (season, week)
            )
            return cur.rowcount
