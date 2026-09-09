"""Timezone-aware scheduler.

The rule that matters (spec section 19): NOTHING is precomputed.  The daemon only
tracks *when* to fire.  When a game hits its notification window it re-fetches
ESPN, Sleeper, the NFL scoreboard, live scores, starters and projections, and only
then builds the message.  A guide computed on Tuesday is never sent on Thursday.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from .analysis import Analyzer, GameGuide
from .config import AppConfig
from .db.database import Database
from .formatting import phone_message, phone_title
from .live import (
    DEFAULT_THRESHOLD, Snapshot, detect_events, live_message, live_title, take_snapshot,
)
from .models import NFLGame, PlayerGameState
from .morning import games_today, is_due, parse_hhmm
from .notifiers import Notifier, autoselect
from .providers.base import ProviderError

log = logging.getLogger(__name__)

POLL_SECONDS = 30
#: How late we will still fire if the machine was asleep at the exact moment.
LATE_GRACE = timedelta(minutes=12)


@dataclass
class DueGame:
    game: NFLGame
    fire_at: datetime

    @property
    def minutes_until_kick(self) -> float:
        return (self.game.kickoff - datetime.now(timezone.utc)).total_seconds() / 60.0


def due_games(games: list[NFLGame], minutes_before: int, *,
              now: Optional[datetime] = None,
              grace: timedelta = LATE_GRACE) -> list[DueGame]:
    """Games whose notification window is open right now.

    Open means: we are past (kickoff - minutes_before) and not more than `grace`
    past it, and kickoff has not already happened.  The grace window is what
    saves you when the Mac was asleep for the previous ten minutes.
    """
    now = now or datetime.now(timezone.utc)
    out = []
    for g in games:
        fire_at = g.kickoff - timedelta(minutes=minutes_before)
        if fire_at <= now <= min(fire_at + grace, g.kickoff + timedelta(minutes=2)):
            out.append(DueGame(game=g, fire_at=fire_at))
    return sorted(out, key=lambda d: d.game.kickoff)


def next_fire_time(games: list[NFLGame], minutes_before: int,
                   now: Optional[datetime] = None) -> Optional[datetime]:
    now = now or datetime.now(timezone.utc)
    times = [g.kickoff - timedelta(minutes=minutes_before) for g in games]
    future = [t for t in times if t > now]
    return min(future) if future else None


class Scheduler:
    def __init__(self, cfg: AppConfig, *, analyzer: Optional[Analyzer] = None,
                 notifier: Optional[Notifier] = None, db: Optional[Database] = None,
                 dry_run: bool = False) -> None:
        self.cfg = cfg
        self.analyzer = analyzer or Analyzer(cfg)
        self.notifier = notifier or autoselect(cfg.notifier, dry_run=dry_run)
        self.db = db or Database()
        self.dry_run = dry_run

    # -- one game -----------------------------------------------------------
    def build_guide(self, game: NFLGame, week: Optional[int] = None) -> GameGuide:
        """Full refresh, right now.  Steps 1-11 of spec section 19."""
        ctx = self.analyzer.load_week(week or game.week, refresh=True)
        return self.analyzer.analyze_game(ctx, game)

    def notify_game(self, game: NFLGame, *, force: bool = False,
                    week: Optional[int] = None) -> tuple[bool, str]:
        """Refresh everything, render, send, and record.  Returns (sent, detail)."""
        season, wk = game.season, (week or game.week)
        if not force and self.db.was_sent(game.id, season, wk):
            return False, "already sent (duplicate suppressed)"

        guide = self.build_guide(game, week=wk)
        title = phone_title(guide)
        body = phone_message(guide)

        result = self.notifier.send(title, body)
        self.db.log_send(game.id, result.provider, result.ok, result.attempts, result.detail)
        if result.ok and not result.dry_run:
            self.db.mark_sent(game_id=game.id, season=season, week=wk, slot=game.slot.value,
                              matchup=game.matchup, kickoff_utc=game.kickoff.isoformat(),
                              provider=result.provider, body=body)
            self._retire_morning_push(game, season, wk)
        elif result.ok and result.dry_run:
            log.info("Dry run - not recording %s as sent", game.matchup)
        return result.ok, result.detail

    def _retire_morning_push(self, game: NFLGame, season: int, week: int) -> None:
        """Delete the gameday-morning preview now that the real kickoff push has
        landed - only after the new one is confirmed sent, never before, so a
        failed resend can't leave you with nothing.
        """
        morning = self.db.get_morning_push(game.id, season, week)
        if not morning or not morning.get("message_id"):
            return
        try:
            deleted = self.notifier.delete(morning["message_id"])
        except Exception as exc:  # noqa: BLE001 - deletion is best-effort
            log.debug("Could not delete morning preview for %s: %s", game.matchup, exc)
            deleted = False
        log.info("%s morning preview for %s",
                "Deleted" if deleted else "Left in place (provider can't delete)",
                game.matchup)
        self.db.clear_morning_push(game.id, season, week)

    # -- live in-game updates ----------------------------------------------
    def live_tick(self, *, threshold: float = DEFAULT_THRESHOLD,
                  week: Optional[int] = None) -> list[tuple[NFLGame, int]]:
        """Poll in-progress primetime games and alert on real scoring jumps.

        The snapshot is only advanced after a SUCCESSFUL send, so a failed push
        is retried on the next pass rather than lost.
        """
        season, wk, stype = self.analyzer.resolve_week(week)
        ctx = self.analyzer.load_week(wk, refresh=True)
        live = [g for g in self.analyzer.primetime(ctx)
                if g.state == PlayerGameState.IN_PROGRESS]

        out: list[tuple[NFLGame, int]] = []
        for game in live:
            guide = self.analyzer.analyze_game(ctx, game)
            before = Snapshot.from_dict(self.db.get_snapshot(game.id, season, wk))
            now = take_snapshot(ctx.ok_states, game)

            if before.is_empty:
                # First look at this game: record a baseline, never alert on it.
                self.db.save_snapshot(game.id, now.to_dict(), season, wk)
                log.info("Live baseline recorded for %s", game.matchup)
                continue

            events = [e for e in detect_events(before, guide, ctx.ok_states, game,
                                               threshold=threshold)
                      if e.rooting.matters]
            if not events:
                self.db.save_snapshot(game.id, now.to_dict(), season, wk)
                continue

            title = live_title(game, events)
            body = live_message(events, before, game)
            result = self.notifier.send(title, body)
            self.db.log_send(f"{game.id}:live", result.provider, result.ok,
                             result.attempts, result.detail)
            if result.ok:
                self.db.save_snapshot(game.id, now.to_dict(), season, wk)
                out.append((game, len(events)))
            else:
                log.warning("Live update failed for %s; will retry: %s",
                            game.matchup, result.detail)
        return out

    # -- gameday-morning preview ----------------------------------------------
    def morning_tick(self, *, now: Optional[datetime] = None,
                     force: bool = False) -> list[tuple[NFLGame, bool, str]]:
        """Send an early preview of today's primetime game(s), in the EXACT
        format of the real kickoff push - same title, same body.

        The only visible difference is the countdown in the title ("in 8h" at
        9am vs "in 15" at kickoff), since it's the same `phone_title` /
        `phone_message` call made hours earlier. When the real kickoff push
        later lands, `notify_game` deletes this one (Telegram only - see
        `Notifier.delete`), so you end up with one message per game, not two.

        Dedup is per game (key "morning:<game_id>"), not per day, since each
        game's preview is retired independently at its own kickoff.
        """
        if not self.cfg.morning_summary and not force:
            return []
        tz = self.cfg.tz
        target = parse_hhmm(self.cfg.morning_summary_time)
        if not force and not is_due(target, tz, now=now):
            return []

        try:
            season, week, stype = self.analyzer.resolve_week()
            games = self.analyzer.nfl.games(season, week, stype, cache_ttl=300)
        except ProviderError as exc:
            log.error("Could not load the NFL schedule for the morning preview: %s", exc)
            return []

        today = games_today(_primetime(games, self.cfg.include_slots), tz,
                            on=(now or datetime.now(tz)).astimezone(tz).date())
        if not today:
            return []

        ctx = self.analyzer.load_week(week, refresh=True)
        results: list[tuple[NFLGame, bool, str]] = []
        for game in today:
            r = self._send_morning_preview(game, season, week, force=force, now=now, ctx=ctx)
            if r is not None:
                results.append(r)
        return results

    def preview_game(self, game: NFLGame, *, week: Optional[int] = None,
                     force: bool = True, now: Optional[datetime] = None
                     ) -> tuple[NFLGame, bool, str]:
        """Send the morning-style preview for ONE game, regardless of whether it
        falls on "today" or the configured time has arrived.

        For testing: `fantasy-agent morning --game "SF LAR"` lets you see the
        preview (and its later delete-and-swap) for a game days out, without
        waiting for its actual gameday morning.
        """
        wk = week or game.week
        ctx = self.analyzer.load_week(wk, refresh=True)
        result = self._send_morning_preview(game, game.season, wk, force=force, now=now, ctx=ctx)
        return result or (game, False, "nothing sent")

    def _send_morning_preview(self, game: NFLGame, season: int, week: int, *,
                              force: bool, now: Optional[datetime], ctx) -> Optional[
                                  tuple[NFLGame, bool, str]]:
        key = f"morning:{game.id}"
        if not force and self.db.was_sent(key, season, week):
            return None

        guide = self.analyzer.analyze_game(ctx, game)
        title = phone_title(guide, now=now)
        body = phone_message(guide, now=now)

        result = self.notifier.send(title, body)
        self.db.log_send(key, result.provider, result.ok, result.attempts, result.detail)
        if result.ok and not result.dry_run:
            self.db.mark_sent(game_id=key, season=season, week=week, slot="MORNING",
                              matchup=game.matchup, kickoff_utc=game.kickoff.isoformat(),
                              provider=result.provider, body=body)
            self.db.save_morning_push(game.id, season, week,
                                      result.provider, result.message_id)
        return game, result.ok, result.detail

    # -- the loop -----------------------------------------------------------
    def tick(self, *, now: Optional[datetime] = None) -> list[tuple[NFLGame, bool, str]]:
        """One scheduler pass.  Safe to call as often as you like."""
        now = now or datetime.now(timezone.utc)
        try:
            season, week, stype = self.analyzer.resolve_week()
            games = self.analyzer.nfl.games(season, week, stype, cache_ttl=120)
        except ProviderError as exc:
            log.error("Could not load the NFL schedule this tick: %s", exc)
            return []

        watch = _primetime(games, self.cfg.include_slots)

        results = []
        for due in due_games(watch, self.cfg.minutes_before, now=now):
            if self.db.was_sent(due.game.id, season, week):
                continue
            log.info("Firing notification for %s %s (kickoff %s)",
                     due.game.slot.value, due.game.matchup, due.game.kickoff.isoformat())
            try:
                ok, detail = self.notify_game(due.game, week=week)
            except Exception as exc:  # noqa: BLE001 - a bad game must not kill the daemon
                log.exception("Notification failed for %s", due.game.matchup)
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            results.append((due.game, ok, detail))
        return results

    def run_forever(self, poll_seconds: int = POLL_SECONDS,
                    stop: Optional[Callable[[], bool]] = None,
                    live: bool = True, live_every: int = 60,
                    morning_every: int = 300) -> None:
        log.info("Fantasy rooting agent daemon started (tz=%s, %d min before kickoff, "
                 "morning digest=%s at %s, notifier=%s, live updates=%s)",
                 self.cfg.timezone, self.cfg.minutes_before, self.cfg.morning_summary,
                 self.cfg.morning_summary_time, self.notifier.name, live)
        last_live = 0.0
        last_morning = 0.0
        while not (stop and stop()):
            try:
                for game, ok, detail in self.tick():
                    log.info("%s -> %s (%s)", game.matchup, "SENT" if ok else "FAILED", detail)
            except Exception:  # noqa: BLE001
                log.exception("Scheduler tick blew up; continuing")

            if (time.monotonic() - last_morning) >= morning_every:
                last_morning = time.monotonic()
                try:
                    for game, ok, detail in self.morning_tick():
                        log.info("Morning preview for %s -> %s (%s)",
                                game.matchup, "SENT" if ok else "FAILED", detail)
                except Exception:  # noqa: BLE001
                    log.exception("Morning preview tick blew up; continuing")

            if live and (time.monotonic() - last_live) >= live_every:
                last_live = time.monotonic()
                try:
                    for game, n in self.live_tick():
                        log.info("Live update sent for %s (%d event(s))", game.matchup, n)
                except Exception:  # noqa: BLE001
                    log.exception("Live tick blew up; continuing")
            time.sleep(poll_seconds)


def _primetime(games, include):
    from .providers.nfl import primetime_games
    return primetime_games(games, include)
