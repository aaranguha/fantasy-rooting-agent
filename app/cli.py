"""fantasy-agent — command line interface."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

from .analysis import Analyzer, GameGuide, WeekContext
from .config import AppConfig, agent_home, config_path, espn_cookies, load_env, setup_logging
from .db.database import Database
from .formatting import long_report, phone_message, phone_title, slot_word, week_summary
from .models import Side, SlotType
from .notifiers import available_notifiers, autoselect, build
from .providers.base import ProviderError
from .scheduler import Scheduler, due_games, next_fire_time

console = Console()
log = logging.getLogger(__name__)


def _cfg() -> AppConfig:
    load_env()
    cfg = AppConfig.load()
    if not cfg.is_configured:
        console.print("[yellow]No leagues configured yet. Run:[/] [bold]fantasy-agent setup[/]")
        sys.exit(2)
    return cfg


def _load(cfg: AppConfig, week: Optional[int], *, quiet: bool = False) -> WeekContext:
    with console.status("[bright_black]Refreshing ESPN, Sleeper and the NFL scoreboard…"):
        ctx = Analyzer(cfg).load_week(week)
    if ctx.errors and not quiet:
        for e in ctx.errors:
            console.print(f"[yellow]⚠ {e}[/]")
    return ctx


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--log-level", default=None, help="DEBUG / INFO / WARNING")
@click.version_option("1.0.0", prog_name="fantasy-agent")
def cli(log_level: Optional[str]) -> None:
    """Who should I actually be rooting for tonight, across all five leagues?"""
    setup_logging(log_level)


# ---------------------------------------------------------------------------
# setup / inspection
# ---------------------------------------------------------------------------


@cli.command()
def setup() -> None:
    """Interactive first-run wizard (leagues, buy-ins, notifications)."""
    from .setup_wizard import run_wizard

    cfg = run_wizard()
    if cfg.is_configured:
        console.print("\n[bold green]Verifying every league…[/]")
        ctx = _load(cfg, None)
        _print_leagues(cfg, ctx)
        console.print("\n[bright_black]Tip: `fantasy-agent standings` explains every "
                      "season multiplier above.[/]")
        _print_matchups(ctx, starters=True)


@cli.command()
def leagues() -> None:
    """Show all leagues with buy-ins, multipliers and effective weights."""
    cfg = _cfg()
    ctx = _load(cfg, None, quiet=True)
    _print_leagues(cfg, ctx)


@cli.command("set-priority")
@click.argument("league_query")
@click.option("--low/--normal", "low", default=True,
              help="--low demotes it (default); --normal restores it")
def set_priority(league_query: str, low: bool) -> None:
    """Demote a league so it's still analyzed but never crowds your push.

    A low-priority league's players still get full analysis everywhere
    (`player`, `game`, the dashboard, `leagues`) - only the 15-minute push
    condenses them into one footer line instead of a full block per player.

    \b
      fantasy-agent set-priority "I MEAN WE COULDD"
      fantasy-agent set-priority "I MEAN WE COULDD" --normal
    """
    cfg = _cfg()
    q = league_query.strip().lower()
    matches = [l for l in cfg.leagues if q in l.name.lower()]
    if not matches:
        console.print(f"[red]No league matches {league_query!r}.[/] Leagues: "
                      + ", ".join(l.name for l in cfg.leagues))
        sys.exit(1)
    if len(matches) > 1:
        console.print(f"[red]{league_query!r} matches more than one league:[/] "
                      + ", ".join(l.name for l in matches) + ". Be more specific.")
        sys.exit(1)

    lc = matches[0]
    lc.low_priority = low
    cfg.save()
    state = "low priority — still mentioned, never in the way" if low else "normal priority"
    console.print(f"[green]✓[/] {lc.name} is now [bold]{state}[/].")


def _print_leagues(cfg: AppConfig, ctx: Optional[WeekContext] = None) -> None:
    states = {s.league.id: s for s in (ctx.states if ctx else [])}
    t = Table(title="LEAGUES", header_style="bold cyan")
    for col in ("League", "Plat", "Scoring", "Buy-in", "Manual", "Season",
                "Weight", "Season context", "Score"):
        t.add_column(col)

    live: list[tuple[str, float]] = []
    for lc in cfg.leagues:
        s = states.get(lc.league_id)
        league = s.league if s else lc.to_league()
        weight = league.effective_weight if s else lc.effective_weight
        live.append((lc.name or lc.league_id, weight))
        season = f"{league.season_multiplier:g}" if s else "—"
        if s and league.season_multiplier > 1.15:
            season = f"[green]×{league.season_multiplier:g}[/]"
        elif s and league.season_multiplier < 0.85:
            season = f"[red]×{league.season_multiplier:g}[/]"
        elif s:
            season = f"×{league.season_multiplier:g}"
        note = league.season_note if s else ""
        if lc.loser_punishment:
            note = "💀 " + note
        name = (lc.name or lc.league_id) + (" 🔕" if lc.low_priority else "")
        t.add_row(
            name,
            lc.platform[:4],
            league.scoring.format_name if s else "—",
            f"${lc.buy_in_usd:g}",
            f"×{lc.importance_multiplier:g}",
            season,
            f"[bold]{weight:g}[/]",
            (note[:44] if not (s and s.error) else "[red]error[/]"),
            f"{s.current_score_mine:.1f} – {s.current_score_opponent:.1f}" if s else "—",
        )
    console.print(t)
    total = sum(w for _, w in live)
    console.print(f"Total buy-in: [bold]${cfg.total_buy_in:g}[/]   "
                  f"Total live weight: [bold]{total:g}[/]")
    console.print("[bright_black]weight = buy-in × manual multiplier × season multiplier[/]")
    if len(live) >= 2:
        hi = max(live, key=lambda x: x[1])
        lo = min(live, key=lambda x: x[1])
        if lo[1]:
            console.print(f"[bright_black]Right now, affecting “{hi[0]}” matters "
                          f"{hi[1] / lo[1]:.1f}× as much as “{lo[0]}”.[/]")


@cli.command()
@click.option("--week", type=int, default=None)
def standings(week: Optional[int]) -> None:
    """Season outlook per league, and how it's reweighting your rooting."""
    cfg = _cfg()
    ctx = _load(cfg, week)
    by_id = {l.league_id: l for l in cfg.leagues}

    t = Table(title=f"SEASON OUTLOOK — week {ctx.week}", header_style="bold cyan")
    for col in ("League", "Record", "Seed", "Playoffs", "Title", "Last place",
                "Season ×", "Live weight"):
        t.add_column(col)
    for s in ctx.states:
        lg = s.league
        o = lg.outlook
        lc = by_id.get(lg.id)
        if s.error and not s.my_starters:
            t.add_row(lg.name, "[red]error[/]", "—", "—", "—", "—", "—", "—")
            continue
        if not o or not o.available:
            t.add_row(lg.name, "—", "—", "[bright_black]standings unavailable[/]",
                      "—", "—", "×1", f"{lg.effective_weight:g}")
            continue
        last = f"{o.last_place_odds:.0%}"
        if lc and lc.loser_punishment and o.last_place_odds >= 0.25:
            last = f"[red bold]{last} 💀[/]"
        mult = f"{lg.season_multiplier:g}"
        colour = "green" if lg.season_multiplier > 1.15 else (
            "red" if lg.season_multiplier < 0.85 else "white")
        t.add_row(lg.name, o.record, f"#{o.seed}/{o.teams}",
                  f"{o.playoff_odds:.0%}", f"{o.title_odds:.0%}", last,
                  f"[{colour}]×{mult}[/]", f"[bold]{lg.effective_weight:g}[/]")
    console.print(t)

    console.print("\n[bold]Why each league is weighted the way it is[/]")
    for s in ctx.states:
        if s.league.season_note:
            console.print(f"  [cyan]{s.league.name}[/]: {s.league.season_note}")
    console.print("\n[bright_black]Season multiplier = max(title upside, punishment "
                  "downside). Turn it off per league with \"dynamic_importance\": false "
                  "in config.json.[/]")


@cli.command()
@click.option("--week", type=int, default=None)
@click.option("--starters/--no-starters", default=True, help="List the starting lineups")
def matchups(week: Optional[int], starters: bool) -> None:
    """This week's matchup in every league, with live scores and projections."""
    ctx = _load(_cfg(), week)
    _print_matchups(ctx, starters=starters)


def _print_matchups(ctx: WeekContext, *, starters: bool) -> None:
    from .leverage import compute_leverage

    console.print(f"\n[bold]Week {ctx.week}, {ctx.season}[/]  ·  data tier: {ctx.tier.value}")
    for s in ctx.states:
        if s.error and not s.my_starters:
            console.print(f"\n[red]✗ {s.league.name}: {s.error}[/]")
            continue
        lev = compute_leverage(s)
        console.print(
            f"\n[bold cyan]{s.league.name}[/] (${s.league.buy_in_usd:g} × "
            f"{s.league.importance_multiplier:g} = {s.league.effective_weight:g}w, "
            f"{s.league.scoring.format_name})")
        console.print(
            f"  {s.league.my_team_name} [bold]{s.current_score_mine:.1f}[/] – "
            f"[bold]{s.current_score_opponent:.1f}[/] {s.opponent_name}   "
            f"proj {s.projected_final_mine:.1f} – {s.projected_final_opponent:.1f} "
            f"({s.projected_margin:+.1f})   win [bold]{lev.win_prob:.0%}[/] "
            f"({lev.descriptor}, {lev.leverage:.0f} live $)")
        if not starters:
            continue
        t = Table(box=None, header_style="bright_black", pad_edge=False)
        for c in ("Slot", "Mine", "Now", "Proj", "", "Slot", "Theirs", "Now", "Proj"):
            t.add_column(c)
        rows = max(len(s.my_starters), len(s.opp_starters))
        for i in range(rows):
            a = s.my_starters[i] if i < len(s.my_starters) else None
            b = s.opp_starters[i] if i < len(s.opp_starters) else None
            t.add_row(
                a.slot if a else "", a.canonical.short_name if a else "",
                f"{a.current_points:.1f}" if a else "", f"{a.projected_points:.1f}" if a else "",
                "│",
                b.slot if b else "", b.canonical.short_name if b else "",
                f"{b.current_points:.1f}" if b else "", f"{b.projected_points:.1f}" if b else "",
            )
        console.print(t)


# ---------------------------------------------------------------------------
# rooting
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--week", type=int, default=None)
@click.option("--all-games", is_flag=True, help="Include non-primetime games too")
@click.option("--dry-run", is_flag=True,
              help="Show the phone-sized push for each game instead of the full report")
def week(week: Optional[int], all_games: bool, dry_run: bool) -> None:
    """Rooting guides for every primetime game this week."""
    _week_impl(week, all_games, dry_run=dry_run)


@cli.command("tonight")
@click.option("--week", type=int, default=None)
def tonight(week: Optional[int]) -> None:
    """The next primetime game (or the one happening right now)."""
    cfg = _cfg()
    ctx = _load(cfg, week)
    an = Analyzer(cfg)
    now = datetime.now(timezone.utc)
    games = an.primetime(ctx)
    live = [g for g in games if g.kickoff <= now <= g.kickoff + timedelta(hours=4)]
    upcoming = sorted([g for g in games if g.kickoff > now], key=lambda g: g.kickoff)
    picks = live or upcoming[:1]
    if not picks:
        console.print("[yellow]No primetime games left this week.[/]")
        return
    for g in picks:
        console.print(long_report(an.analyze_game(ctx, g), cfg.tz))


@cli.command("game")
@click.argument("query")
@click.option("--week", type=int, default=None)
def game_cmd(query: str, week: Optional[int]) -> None:
    """Rooting guide for one game, e.g. `fantasy-agent game "BUF MIA"` or `game MNF`."""
    cfg = _cfg()
    ctx = _load(cfg, week)
    an = Analyzer(cfg)
    g = an.find_game(ctx, query)
    if not g:
        console.print(f"[red]No game matching {query!r} in week {ctx.week}.[/]")
        console.print("Games: " + ", ".join(x.matchup for x in ctx.games))
        sys.exit(1)
    console.print(long_report(an.analyze_game(ctx, g), cfg.tz))


@cli.command("player")
@click.argument("name")
@click.option("--week", type=int, default=None)
def player_cmd(name: str, week: Optional[int]) -> None:
    """Detailed cross-league analysis for one player."""
    cfg = _cfg()
    ctx = _load(cfg, week)
    an = Analyzer(cfg)
    hits = ctx.registry.search(name)
    if not hits:
        console.print(f"[red]No NFL player matching {name!r}.[/]")
        sys.exit(1)
    target = hits[0]
    if len(hits) > 1:
        console.print("[bright_black]Also matched: " +
                      ", ".join(str(h) for h in hits[1:5]) + "[/]")
    pr = an.analyze_player(ctx, target)
    if pr is None:
        console.print(f"[yellow]{target} is not in anyone's starting lineup "
                      f"in your leagues this week.[/]")
        return

    console.print(f"\n[bold]{target.name}[/]  {target.position}-{target.nfl_team}")
    console.print(f"{pr.headline()}   [bright_black]score {pr.score:+.1f} · "
                  f"${pr.dollar_swing:.0f} live · {pr.best_confidence.value}[/]\n")
    for group, label in ((pr.owned_lines, "OURS"), (pr.faced_lines, "AGAINST")):
        if not group:
            continue
        console.print(f"[bold]{label}[/]")
        for l in sorted(group, key=lambda x: -x.dollar_swing):
            t = l.threshold
            console.print(f"  [cyan]{l.league.label}[/] ({l.league.scoring.format_name})")
            if t.feasible:
                verb = "Need" if l.mine else "Can afford"
                console.print(f"    {verb} {t.qualifier} [bold]{t.value:.1f}[/] points "
                              f"({t.remaining_required:+.1f} from here) — {t.confidence.value}")
            else:
                console.print(f"    {t.impossible_reason}")
            proj_prob = l.leverage.win_prob
            console.print(f"    Current win probability: [bold]{proj_prob:.0%}[/] "
                          f"({l.leverage.descriptor})")
            lo = _prob_at(pr, l, 0.0)
            hi = _prob_at(pr, l, t.value * 1.6 if t.value else 25)
            console.print(f"    If he's shut out: {lo:.0%}   ·   if he goes off: {hi:.0%}   "
                          f"[bright_black](swing {l.swing:.0%}, ${l.dollar_swing:.0f})[/]")
    rng = pr.range_phrase()
    console.print(f"\n[bold]Net rooting recommendation:[/] {pr.emoji} "
                  f"[bold]{pr.verdict}[/]" + (f"  (ideal: {rng})" if rng else ""))
    console.print(f"[italic]Reason:[/] {pr.narrative()}\n")


def _prob_at(pr, line, points: float) -> float:
    from .leverage import conditional_win_probability

    return conditional_win_probability(line.threshold.state, line.threshold.exposure, points)


@cli.command()
@click.option("--week", type=int, default=None)
def exposure(week: Optional[int]) -> None:
    """Every started player across all leagues, ranked by weighted rooting value."""
    cfg = _cfg()
    ctx = _load(cfg, week)
    rows = Analyzer(cfg).exposure_table(ctx)
    t = Table(title=f"EXPOSURE — week {ctx.week}", header_style="bold cyan")
    for c in ("Player", "Pos", "Team", "Ours", "Against", "Raw count", "Weighted", "Live $"):
        t.add_column(c)
    for r in rows[:60]:
        p = r["player"]
        t.add_row(p.name, p.position, p.nfl_team,
                  ", ".join(f"${l.buy_in_usd:g}" for l in r["ours"]) or "—",
                  ", ".join(f"${l.buy_in_usd:g}" for l in r["against"]) or "—",
                  f"+{len(r['ours'])}/-{len(r['against'])}",
                  f"{r['weighted']:+.0f}",
                  f"[{'green' if r['live'] >= 0 else 'red'}]{r['live']:+.1f}[/]")
    console.print(t)


# ---------------------------------------------------------------------------
# notifications / daemon
# ---------------------------------------------------------------------------


def _week_impl(week: Optional[int], all_games: bool, *, dry_run: bool) -> None:
    cfg = _cfg()
    ctx = _load(cfg, week)
    an = Analyzer(cfg)
    games = ctx.games if all_games else an.primetime(ctx)
    if not games:
        console.print("[yellow]No primetime games found this week.[/]")
        return
    guides = [an.analyze_game(ctx, g) for g in games]
    console.print(week_summary(guides, cfg.tz))
    for guide in guides:
        if dry_run:
            title, body = phone_title(guide), phone_message(guide)
            console.print(f"[bold cyan]{title}[/]\n{body}\n")
        else:
            console.print(long_report(guide, cfg.tz))


@cli.command("run-once")
@click.option("--game", "query", default=None, help='Force a specific game, e.g. "BUF MIA"')
@click.option("--week", type=int, default=None)
@click.option("--force", is_flag=True, help="Send even if already sent")
@click.option("--dry-run", is_flag=True, help="Build and print, but send nothing")
def run_once(query: Optional[str], week: Optional[int], force: bool, dry_run: bool) -> None:
    """Do exactly what the daemon would do right now, once."""
    cfg = _cfg()
    sched = Scheduler(cfg, dry_run=dry_run)
    if query:
        ctx = _load(cfg, week)
        g = Analyzer(cfg).find_game(ctx, query)
        if not g:
            console.print(f"[red]No game matching {query!r}.[/]")
            sys.exit(1)
        ok, detail = sched.notify_game(g, force=force, week=ctx.week)
        console.print(f"{'[green]sent[/]' if ok else '[red]not sent[/]'}: {detail}")
        return

    results = sched.tick()
    if not results:
        season, wk, stype = sched.analyzer.resolve_week(week)
        games = sched.analyzer.nfl.games(season, wk, stype)
        from .providers.nfl import primetime_games

        nxt = next_fire_time(primetime_games(games, cfg.include_slots), cfg.minutes_before)
        console.print("[yellow]Nothing due right now.[/]")
        if nxt:
            console.print(f"Next notification fires {nxt.astimezone(cfg.tz):%a %b %-d %-I:%M %p %Z}.")
        return
    for g, ok, detail in results:
        console.print(f"{g.matchup}: {'[green]sent[/]' if ok else '[red]failed[/]'} — {detail}")


@cli.command("morning")
@click.option("--force", is_flag=True, help="Send now regardless of the time or dedupe")
@click.option("--dry-run", is_flag=True, help="Build and print, but send nothing")
@click.option("--game", "query", default=None,
              help='Preview ONE game right now, even if it isn\'t today, '
                   'e.g. "SF LAR" or MNF. Implies --force.')
@click.option("--week", type=int, default=None)
def morning_cmd(force: bool, dry_run: bool, query: Optional[str], week: Optional[int]) -> None:
    """Send (or preview) today's gameday-morning game preview(s) right now.

    Same title and body as the real kickoff push, just sent earlier - the only
    difference is the countdown. When the real kickoff push later fires, it
    swaps this one out (Telegram only) so you end up with one message per game.

    In the daemon this fires automatically once a day at `morning_summary_time`
    on any day with a primetime game. Use --force to test it on a non-gameday,
    or --game to test a specific game regardless of what day it falls on.
    """
    cfg = _cfg()
    sched = Scheduler(cfg, dry_run=dry_run)

    if query:
        ctx = _load(cfg, week)
        g = Analyzer(cfg).find_game(ctx, query)
        if not g:
            console.print(f"[red]No game matching {query!r}.[/]")
            sys.exit(1)
        game, ok, detail = sched.preview_game(g, week=ctx.week, force=True)
        console.print(f"{game.matchup}: {'[green]sent[/]' if ok else '[red]failed[/]'} — {detail}")
        return

    results = sched.morning_tick(force=force)
    if not results:
        console.print("[yellow]Not due — no primetime game today, already sent, "
                      "or before the configured time.[/] Use --force to override, "
                      "or --game \"TEAM TEAM\" to preview a specific game.")
        return
    for game, ok, detail in results:
        console.print(f"{game.matchup}: {'[green]sent[/]' if ok else '[red]failed[/]'} — {detail}")


@cli.command("sunday-morning")
@click.option("--force", is_flag=True, help="Send now regardless of the day, time, or dedupe")
@click.option("--dry-run", is_flag=True, help="Build and print, but send nothing")
def sunday_morning_cmd(force: bool, dry_run: bool) -> None:
    """Send (or preview) the condensed Sunday early+late slate digest.

    Only names with a strong signal (🚀 🟢 🟥 🔴/☠️) - the toss-up and mild-lean
    colors are left out on purpose to keep this a quick read. This replaces
    the per-game morning preview for SNF specifically; TNF and MNF still get
    their own full preview via `fantasy-agent morning`.
    """
    cfg = _cfg()
    sched = Scheduler(cfg, dry_run=dry_run)
    result = sched.sunday_morning_tick(force=force)
    if result is None:
        console.print("[yellow]Not due — not Sunday, no slate games, already sent, "
                      "or before the configured time.[/] Use --force to override.")
        return
    ok, detail = result
    console.print(f"{'[green]sent[/]' if ok else '[red]failed[/]'}: {detail}")


@cli.command("sunday-second-slate")
@click.option("--force", is_flag=True, help="Send now regardless of timing or dedupe")
@click.option("--dry-run", is_flag=True, help="Build and print, but send nothing")
def sunday_second_slate_cmd(force: bool, dry_run: bool) -> None:
    """Send (or preview) the second condensed digest, 15 minutes before the
    late Sunday window - recomputed on real early-window results."""
    cfg = _cfg()
    sched = Scheduler(cfg, dry_run=dry_run)
    result = sched.sunday_second_slate_tick(force=force)
    if result is None:
        console.print("[yellow]Not due — not Sunday, no late-window games, "
                      "already sent, or not within 15 minutes of the late window.[/] "
                      "Use --force to override.")
        return
    ok, detail = result
    console.print(f"{'[green]sent[/]' if ok else '[red]failed[/]'}: {detail}")


@cli.command("test-notification")
@click.option("--provider", default=None, help="imessage / ntfy / telegram / console / twilio")
@click.option("--real", is_flag=True,
              help="Actually send. Without this the message is only printed.")
@click.option("--week", type=int, default=None)
@click.option("--sample", is_flag=True, help="Use canned data instead of your live leagues")
def test_notification(provider: Optional[str], real: bool, week: Optional[int],
                      sample: bool) -> None:
    """Send yourself a test rooting guide. Defaults to a DRY RUN (nothing is sent)."""
    load_env()
    cfg = AppConfig.load()
    name = provider or cfg.notifier

    if sample or not cfg.is_configured:
        if sample and cfg.is_configured:
            console.print("[yellow]--sample uses FAKE players and FAKE leagues. "
                          "Drop the flag to test with your real week.[/]")
        title = "🧪 SAMPLE (not real) — SNF in 15: DAL @ PHI"
        body = ("🧪 EXAMPLE DATA — these players and leagues are made up.\n\n"
                "🚀 A.J. Brown · GO OFF (proj: 16)\n"
                "   ours: Dynasty, Work League\n\n"
                "☠️ CeeDee Lamb · UNDER 22\n"
                "   vs us: Dynasty, Family\n\n"
                "🟧 Saquon Barkley · WANT 17-26\n"
                "   ours: Work League\n"
                "   vs us: Family\n\n"
                "🎯 Dynasty is tightest (52% to win)")
    else:
        ctx = _load(cfg, week)
        an = Analyzer(cfg)
        games = an.primetime(ctx) or ctx.games
        if not games:
            console.print("[red]No games this week to build a test from.[/]")
            sys.exit(1)
        guide = an.analyze_game(ctx, games[0])
        title, body = phone_title(guide), phone_message(guide)

    n = build(name, dry_run=not real)
    ok, why = n.available()
    console.print(f"Provider [bold]{n.name}[/]: {'[green]configured[/]' if ok else f'[red]{why}[/]'}")
    if n.requires_local_mac:
        console.print("[yellow]Note: iMessage needs this Mac awake, logged in and signed "
                      "into Messages at send time.[/]")
    if not real:
        console.print("[bright_black]DRY RUN — nothing will actually be sent. "
                      "Add --real to send for real.[/]\n")
        console.print(f"[bold cyan]{title}[/]\n{body}\n")
    res = n.send(title, body)
    console.print(f"→ ok={res.ok} attempts={res.attempts} {res.detail}")
    if not res.ok:
        sys.exit(1)


@cli.command("telegram-chat-id")
@click.option("--save/--no-save", default=True, help="Write it to ~/.fantasy-agent/.env")
def telegram_chat_id(save: bool) -> None:
    """Find YOUR Telegram chat id and save it (fixes the 403 'bot can't message
    the bot' error)."""
    from .notifiers.telegram import TelegramNotifier
    from .setup_wizard import env_path, write_env

    load_env()
    n = TelegramNotifier()
    if not n.token:
        console.print("[red]TELEGRAM_BOT_TOKEN is not set.[/] Run `fantasy-agent setup`.")
        sys.exit(1)
    # Which bot is this token for? The decisive check when nothing is found.
    try:
        who = n.me()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Token rejected by Telegram: {exc}[/]")
        console.print("Re-copy it from @BotFather → /mybots → your bot → API Token, "
                      "then run `fantasy-agent setup` again.")
        sys.exit(1)

    handle = who.get("username", "?")
    console.print(f"Token belongs to: [bold]@{handle}[/] "
                  f"([bright_black]{who.get('first_name','')}, id {n.bot_id}[/])")
    if n.chat_id and n.chat_id == n.bot_id:
        console.print("[yellow]⚠ Your saved TELEGRAM_CHAT_ID is the bot's own id — "
                      "that's the bug. Replacing it.[/]")

    try:
        raw = n.raw_update_count()
        chats = n.discover_chat_id()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Could not reach Telegram: {exc}[/]")
        sys.exit(1)

    if not chats:
        console.print(f"\n[yellow]No human chats found[/] "
                      f"[bright_black]({raw} pending update(s) on this bot)[/]")
        if raw == 0:
            console.print(f"\nTelegram has received [bold]nothing at all[/] for "
                          f"[bold]@{handle}[/].")
            console.print("\n  Open this exact bot and tap [bold]Start[/]:")
            console.print(f"     [bold cyan]https://t.me/{handle}[/]")
            console.print("\n  [bright_black]BotFather lets two bots share a display "
                          "name, so a chat titled the same thing may be a different "
                          "bot. Tap the bot's name at the top of that chat to see its "
                          "@handle - if it isn't @" + handle + ", that's the wrong "
                          "bot.[/]")
            console.print("  [bright_black]To switch tokens instead: @BotFather → "
                          "/mybots → the right bot → API Token → `fantasy-agent setup`.[/]")
        else:
            console.print("  Updates exist but all are from bots. Send a message "
                          "yourself, from your own account.")
        sys.exit(1)

    for i, c in enumerate(chats, 1):
        console.print(f"  [{i}] {c['name']}  ({c['type']}, id {c['id']})")
    chosen = chats[0] if len(chats) == 1 else chats[
        click.prompt("Which chat?", type=int, default=1) - 1]
    console.print(f"\n[green]✓ Your chat id: [bold]{chosen['id']}[/][/]")

    if save:
        write_env({"TELEGRAM_CHAT_ID": chosen["id"]})
        console.print(f"Saved to {env_path()}")
        console.print("\nNow test it:  [bold]fantasy-agent test-notification "
                      "--provider telegram --sample --real[/]")


@cli.command()
def notifiers() -> None:
    """Show which notification providers are configured."""
    t = Table(title="NOTIFIERS (priority order)", header_style="bold cyan")
    t.add_column("Provider"); t.add_column("Ready"); t.add_column("Detail"); t.add_column("Cost")
    costs = {"imessage": "free", "ntfy": "free", "telegram": "free",
             "console": "free", "twilio": "PAID (optional)"}
    for name, ok, why in available_notifiers():
        t.add_row(name, "[green]✓[/]" if ok else "[red]✗[/]", why or "ready", costs[name])
    console.print(t)


@cli.command()
def status() -> None:
    """Config, credentials, schedule and recent notification history."""
    load_env()
    cfg = AppConfig.load()
    console.print(f"[bold]Config[/]        {config_path()} "
                  f"{'[green](exists)[/]' if config_path().exists() else '[red](missing)[/]'}")
    console.print(f"[bold]Home[/]          {agent_home()}")
    console.print(f"[bold]Timezone[/]      {cfg.timezone}")
    console.print(f"[bold]Lead time[/]     {cfg.minutes_before} min before kickoff")
    console.print(f"[bold]Morning[/]       "
                  + (f"{cfg.morning_summary_time} local, on gamedays"
                     if cfg.morning_summary else "[bright_black]disabled[/]"))
    console.print(f"[bold]Notifier[/]      {cfg.notifier}")
    console.print(f"[bold]Leagues[/]       {len(cfg.leagues)} "
                  f"({len(cfg.espn_leagues())} ESPN, {len(cfg.sleeper_leagues())} Sleeper), "
                  f"${cfg.total_buy_in:g} total")
    console.print(f"[bold]ESPN creds[/]    "
                  f"{'[green]present[/]' if espn_cookies() else '[yellow]absent (public leagues only)[/]'}")
    for name, ok, why in available_notifiers():
        console.print(f"  {name:10} {'[green]ready[/]' if ok else f'[bright_black]{why}[/]'}")

    try:
        an = Analyzer(cfg)
        season, wk, stype = an.resolve_week()
        games = an.nfl.games(season, wk, stype)
        from .providers.nfl import primetime_games

        pt = primetime_games(games, cfg.include_slots)
        console.print(f"\n[bold]Week {wk} ({season}) primetime slate[/]")
        db = Database()
        sent = {r["game_id"] for r in db.sent_this_week(season, wk)}
        for g in pt:
            fire = g.kickoff - timedelta(minutes=cfg.minutes_before)
            mark = "[green]sent[/]" if g.id in sent else "[bright_black]pending[/]"
            console.print(f"  {slot_word(g.slot):<16} {g.matchup:<11} "
                          f"kick {g.kickoff.astimezone(cfg.tz):%a %-I:%M %p} · "
                          f"notify {fire.astimezone(cfg.tz):%-I:%M %p}  {mark}")
        nxt = next_fire_time(pt, cfg.minutes_before)
        if nxt:
            console.print(f"\nNext fire: [bold]{nxt.astimezone(cfg.tz):%a %b %-d %-I:%M %p %Z}[/]")
        recent = db.recent(8)
        if recent:
            console.print("\n[bold]Recent sends[/]")
            for r in recent:
                console.print(f"  {r['ts_utc'][:19]}  {r['provider']:<9} "
                              f"{'[green]ok[/]' if r['ok'] else '[red]fail[/]'}  {r['detail'][:60]}")
    except ProviderError as exc:
        console.print(f"[red]Could not reach the NFL schedule: {exc}[/]")


@cli.command()
@click.option("--poll", type=int, default=30, help="Seconds between checks")
@click.option("--dry-run", is_flag=True, help="Never actually send")
@click.option("--live/--no-live", default=True,
              help="Also send in-game updates when a player makes a big play")
@click.option("--live-every", type=int, default=60, help="Seconds between live checks")
@click.option("--morning-every", type=int, default=300,
              help="Seconds between checks for the gameday-morning digest")
def daemon(poll: int, dry_run: bool, live: bool, live_every: int, morning_every: int) -> None:
    """Run forever: the gameday-morning digest, the 15-minute kickoff warning,
    and live updates during games."""
    cfg = _cfg()
    Scheduler(cfg, dry_run=dry_run).run_forever(
        poll_seconds=poll, live=live, live_every=live_every, morning_every=morning_every)


@cli.command("live")
@click.option("--poll", type=int, default=60, help="Seconds between checks")
@click.option("--once", is_flag=True, help="Check once and exit")
@click.option("--threshold", type=float, default=None,
              help="Fantasy points in one interval that count as a big play (default 4)")
@click.option("--dry-run", is_flag=True, help="Print updates instead of sending them")
@click.option("--week", type=int, default=None)
def live_cmd(poll: int, once: bool, threshold: Optional[float], dry_run: bool,
             week: Optional[int]) -> None:
    """Watch in-progress primetime games and alert when a big play changes things.

    The first pass on a game records a baseline and stays silent; after that,
    any player whose fantasy score jumps by the threshold triggers an update with
    freshly recomputed thresholds.
    """
    import time

    from .live import DEFAULT_THRESHOLD
    from .models import PlayerGameState

    cfg = _cfg()
    sched = Scheduler(cfg, dry_run=dry_run)
    thr = threshold if threshold is not None else DEFAULT_THRESHOLD
    console.print(f"Watching for plays worth [bold]{thr:g}+[/] fantasy points"
                  + (" [bright_black](dry run)[/]" if dry_run else "")
                  + f" · checking every {poll}s. Ctrl-C to stop.")
    try:
        while True:
            try:
                # One shared pass: live plays + injuries + Bluesky buzz, merged
                # into at most one push per game so they never pile up.
                results = sched.updates_tick(threshold=thr, week=week)
            except ProviderError as exc:
                console.print(f"[yellow]fetch failed, will retry: {exc}[/]")
                results = []
            try:
                for game, ok, detail in sched.recap_tick(week=week):
                    style = "green" if ok else "red"
                    label = "recap sent" if ok else "recap failed"
                    console.print(f"[{style}]{label} for {game.matchup}: {detail}[/]")
            except ProviderError as exc:
                console.print(f"[yellow]recap check failed, will retry: {exc}[/]")
            if results:
                for game, n in results:
                    where = game.matchup if game else "roster news"
                    console.print(f"[green]✓ sent {n} update(s) for {where}[/]")
            else:
                an = Analyzer(cfg)
                season, wk, stype = an.resolve_week(week)
                games = an.nfl.games(season, wk, stype, cache_ttl=30)
                from .providers.nfl import primetime_games

                inplay = [g for g in primetime_games(games, cfg.include_slots)
                          if g.state == PlayerGameState.IN_PROGRESS]
                if not inplay:
                    console.print("[bright_black]no primetime game in progress[/]")
                else:
                    console.print(f"[bright_black]{', '.join(g.matchup for g in inplay)}"
                                  f" — nothing big since last check[/]")
            if once:
                return
            time.sleep(poll)
    except KeyboardInterrupt:
        console.print("\nstopped")


@cli.command("buzz")
@click.argument("query", required=False)
@click.option("--week", type=int, default=None)
@click.option("--send", is_flag=True, help="Actually run the tick and push if something spikes")
def buzz_cmd(query: Optional[str], week: Optional[int], send: bool) -> None:
    """Show current Bluesky chatter for a player (or all your starters).

    `fantasy-agent buzz "Puka Nacua"` inspects one player; with no name it
    scans every starter. `--send` runs the real tick (baseline + dedupe + push).
    """
    from .bluesky import BlueskyClient, assess
    from .providers.base import HttpClient
    from .config import cache_dir

    cfg = _cfg()
    if send:
        sched = Scheduler(cfg)
        hits = sched.buzz_tick(week=week)
        console.print(f"[green]{len(hits)} buzz alert(s) sent[/]" if hits
                      else "[yellow]nothing spiking right now[/]")
        return

    from .bluesky import bluesky_configured
    if not bluesky_configured():
        console.print("[yellow]Set BLUESKY_HANDLE and BLUESKY_APP_PASSWORD to use this.[/]")
        return
    ctx = _load(cfg, week)
    client = BlueskyClient(HttpClient(cache_dir=cache_dir()), cache_dir=cache_dir())
    if query:
        matches = ctx.registry.search(query, limit=1)
        players = matches or []
    else:
        players = sorted({e.canonical for s in ctx.ok_states for e in s.all_starters()},
                         key=lambda p: p.name)
    if not players:
        console.print(f"[red]no player matching {query!r}[/]")
        return
    for p in players:
        res = assess(client, p, baseline=0.0)
        flag = "[bold red]SPIKE[/]" if res.is_spike else ""
        console.print(f"\n[bold]{p.name}[/] ({p.nfl_team}) — {res.count} posts / "
                      f"{res.category.value} {flag}")
        if res.top_post:
            console.print(f"  [bright_black]{res.top_post.snippet(140)}[/]")


@cli.command()
@click.option("--season", type=int, default=None)
@click.option("--week", type=int, default=None)
def reset_sent(season: Optional[int], week: Optional[int]) -> None:
    """Clear the de-duplication record so notifications can fire again."""
    cfg = _cfg()
    s, w, _ = Analyzer(cfg).resolve_week(week)
    n = Database().reset_week(season or s, week or w)
    console.print(f"Cleared {n} sent-notification record(s) for {season or s} week {week or w}.")


@cli.command()
@click.option("--week", type=int, default=None)
@click.option("--game", "game_query", default=None, help='Which game, e.g. "DAL NYG"')
@click.option("--push", is_flag=True, help="Show the phone-sized message instead")
def demo(week: Optional[int], game_query: Optional[str], push: bool) -> None:
    """Run the full engine on five realistic FAKE leagues - no credentials needed.

    Useful for sanity-checking the rooting maths, message format and primetime
    detection before you connect ESPN and Sleeper.
    """
    from .demo import build_demo

    load_env()
    cfg = AppConfig.load()
    if not cfg.season:
        cfg.season = 0
    with console.status("[bright_black]Loading the real NFL schedule and player universe…"):
        ctx, games = build_demo(cfg, week=week, game_query=game_query)
    if not games or not ctx.states:
        console.print("[red]Could not build a demo (no games found this week).[/]")
        sys.exit(1)
    console.print("[yellow]DEMO MODE — these five leagues are synthetic.[/]\n")
    an = Analyzer(cfg)
    guide = an.analyze_game(ctx, games[0])
    if push:
        console.print(f"[bold cyan]{phone_title(guide)}[/]\n{phone_message(guide)}")
    else:
        console.print(long_report(guide, cfg.tz))


@cli.command()
@click.option("--port", type=int, default=8787)
@click.option("--week", type=int, default=None)
@click.option("--no-browser", is_flag=True)
def dashboard(port: int, week: Optional[int], no_browser: bool) -> None:
    """Serve the local web dashboard."""
    from .dashboard.server import serve

    serve(_cfg(), port=port, week=week, open_browser=not no_browser)


@cli.command("manage-league")
@click.option("--league-id", default=None,
              help='Sleeper league id. Defaults to the league named "I MEAN WE COULDD" in config.json.')
def manage_league(league_id: Optional[str]) -> None:
    """Research one league and text lineup/waiver/trade recommendations.

    Separate engine from the primetime rooting commands above - this one
    decides what to DO with a roster instead of what to root for. Recommend-
    only: it never touches Sleeper directly (no write API exists, and driving
    the site via browser automation ran straight into Sleeper's own bot
    detection - see README.md "League Manager" for the full story). You tap
    its suggestions into the app yourself.
    """
    from .league_manager.run import run as run_league_manager

    load_env()
    cfg = AppConfig.load()
    if not cfg.sleeper_username:
        console.print("[red]No Sleeper username configured.[/] Run `fantasy-agent setup` first.")
        sys.exit(1)

    lid = league_id
    if not lid:
        match = next((l for l in cfg.leagues
                      if l.platform == "sleeper" and "i mean we could" in (l.name or "").lower()),
                     None)
        if not match:
            console.print("[red]No --league-id given and no \"I MEAN WE COULDD\" league found "
                          "in config.json.[/]")
            sys.exit(1)
        lid = match.league_id

    console.print(f"[bold]Researching league {lid}[/]")
    with console.status("[bright_black]Gathering roster state, researching, deciding…[/]"):
        summary = run_league_manager(lid, cfg.sleeper_username)

    console.print(f"Week {summary['week']} · {summary['league']}")
    console.print(f"  Lineup changes: {summary['lineup_changes']}  "
                  f"Waiver claims: {summary['waiver_claims']}  "
                  f"Trade proposals: {summary['trade_proposals']}")
    for line in summary["recommendations"]:
        console.print(f"  → {line}")
    console.print(f"Telegram: {'[green]sent[/]' if summary['telegram_sent'] else '[red]failed[/]'} "
                  f"— {summary['telegram_detail']}")


@cli.command("install-launchd")
@click.option("--uninstall", is_flag=True)
def install_launchd(uninstall: bool) -> None:
    """Install (or remove) the macOS launchd job that keeps the daemon running."""
    from .launchd import install, uninstall as remove

    console.print(remove() if uninstall else install())


if __name__ == "__main__":  # pragma: no cover
    cli()
