"""First-run setup wizard.

Secrets policy: anything sensitive is written to ~/.fantasy-agent/.env with mode
0600 and is never placed in config.json, never echoed back to the terminal, and
never committed (.env is gitignored).
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path
from typing import Optional

import click
from zoneinfo import ZoneInfo, available_timezones

from .config import AppConfig, LeagueConfig, agent_home, espn_cookies, load_env
from .models import Platform
from .notifiers import available_notifiers, build
from .playerids import build_registry
from .providers.base import AuthError, ProviderError
from .providers.espn import ESPNProvider
from .providers.nfl import NFLScheduleProvider
from .providers.sleeper import SleeperProvider

ENV_FILE = "⚙"


def env_path() -> Path:
    return agent_home() / ".env"


def write_env(updates: dict[str, str]) -> Path:
    """Merge keys into ~/.fantasy-agent/.env, chmod 600."""
    path = env_path()
    existing: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() and not line.strip().startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                existing[k.strip()] = v.strip()
    existing.update({k: v for k, v in updates.items() if v is not None})
    body = ["# Written by `fantasy-agent setup`. Secrets - never commit this file.", ""]
    body += [f"{k}={v}" for k, v in sorted(existing.items())]
    path.write_text("\n".join(body) + "\n")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    for k, v in updates.items():
        if v is not None:
            os.environ[k] = v
    return path


def hr(title: str = "") -> None:
    click.echo()
    click.secho("─" * 66, fg="bright_black")
    if title:
        click.secho(title, fg="cyan", bold=True)
        click.secho("─" * 66, fg="bright_black")


def ask_league_stakes(name: str, platform: str) -> dict:
    """Buy-in, manual multiplier, and whether losing here carries a punishment."""
    click.secho(f"\n  {platform.upper()} · {name}", fg="yellow", bold=True)
    buy_in = click.prompt("    Buy-in (USD, 0 for a free league)", type=float, default=0.0)
    click.echo("    Importance multiplier: 1.0 = normal, 1.5 = care extra, 0.75 = casual.")
    mult = click.prompt("    Importance multiplier", type=float, default=1.0)

    click.echo("    Does last place in this league get a punishment "
               "(tattoo, billboard, Waffle House, etc.)?")
    punishment, places = 0.0, 1
    if click.confirm("    Loser punishment?", default=False):
        click.echo("      1 = annoying   1.5 = genuinely humiliating   0.5 = mild ribbing")
        punishment = click.prompt("      How much do you dread it?", type=float, default=1.0)
        places = click.prompt("      How many teams get punished?", type=int, default=1)
        click.secho("      → this league's importance will climb automatically if you "
                    "start sliding toward last, especially late in the season.", fg="red")

    dynamic = click.confirm(
        "    Auto-adjust this league's importance from the standings each week?", default=True)

    weight = (buy_in if buy_in > 0 else 10.0) * mult
    click.secho(f"    → static weight {weight:.1f}"
                + ("  (× a season multiplier each week)" if dynamic else "  (fixed)"),
                fg="green")
    return {"buy_in_usd": buy_in, "importance_multiplier": mult,
            "loser_punishment": punishment, "punished_places": places,
            "dynamic_importance": dynamic}


# ---------------------------------------------------------------------------


def run_wizard(cfg: Optional[AppConfig] = None) -> AppConfig:
    load_env()
    cfg = cfg or AppConfig.load()

    click.clear()
    click.secho("🏈  PRIMETIME ROOTING AGENT — SETUP", fg="green", bold=True)
    click.echo("Cross-league rooting decisions for TNF / SNF / MNF and every other "
               "standalone national game.")
    click.echo(f"\nConfig will be written to: {agent_home()}")
    click.secho("Secrets go to ~/.fantasy-agent/.env (chmod 600) and are never committed.",
                fg="bright_black")

    nfl = NFLScheduleProvider()
    try:
        season, week, _ = nfl.current_week()
    except ProviderError:
        season, week = 0, 1
    season = click.prompt("\nSeason year", type=int, default=season or 2026)
    cfg.season = season

    leagues: list[LeagueConfig] = []
    registry = None

    # -- Sleeper ------------------------------------------------------------
    hr("SLEEPER  (3 leagues)")
    if click.confirm("Configure Sleeper leagues?", default=True):
        registry = registry or _registry(season)
        sp = SleeperProvider(registry, season)
        while True:
            username = click.prompt("  Sleeper username", default=cfg.sleeper_username or "")
            try:
                uid = sp.user_id(username)
                break
            except ProviderError as exc:
                click.secho(f"  ✗ {exc}", fg="red")
                if not click.confirm("  Try again?", default=True):
                    uid, username = "", ""
                    break
        if uid:
            cfg.sleeper_username, cfg.sleeper_user_id = username, uid
            found = sp.user_leagues(uid, season)
            if not found:
                click.secho(f"  No {season} Sleeper leagues found for {username}.", fg="yellow")
            for i, lg in enumerate(found, 1):
                click.echo(f"    [{i}] {lg['name']}  ({lg.get('total_rosters','?')} teams)")
            if found:
                picks = click.prompt(
                    "  Which leagues? (comma-separated numbers, or 'all')", default="all")
                idxs = (range(len(found)) if picks.strip().lower() == "all"
                        else [int(x) - 1 for x in picks.split(",") if x.strip().isdigit()])
                for i in idxs:
                    if not 0 <= i < len(found):
                        continue
                    lg = found[i]
                    rid = sp.my_roster_id(lg["league_id"], uid)
                    if not rid:
                        click.secho(f"  ✗ Could not find your roster in {lg['name']}", fg="red")
                        continue
                    stakes = ask_league_stakes(lg["name"], "sleeper")
                    leagues.append(LeagueConfig(
                        platform=Platform.SLEEPER.value, league_id=str(lg["league_id"]),
                        name=lg["name"], my_team_id=str(rid), season=season, **stakes))

    # -- ESPN ---------------------------------------------------------------
    hr("ESPN  (2 leagues)")
    click.echo("Private ESPN leagues need two cookies from a logged-in browser:")
    click.echo("  1. Sign in at fantasy.espn.com in Chrome/Safari")
    click.echo("  2. DevTools → Application → Cookies → https://fantasy.espn.com")
    click.echo("  3. Copy the values of  espn_s2  and  SWID  (SWID includes the { })")
    if click.confirm("\nConfigure ESPN leagues?", default=True):
        if not espn_cookies():
            if click.confirm("  Enter your ESPN cookies now? (stored chmod-600, never committed)",
                             default=True):
                s2 = click.prompt("  espn_s2", hide_input=True)
                swid = click.prompt("  SWID", hide_input=True)
                write_env({"ESPN_S2": s2.strip(), "ESPN_SWID": swid.strip()})
                click.secho(f"  ✓ saved to {env_path()}", fg="green")
        registry = registry or _registry(season)
        ep = ESPNProvider(registry, season, cookies=espn_cookies())
        while True:
            lid = click.prompt("  ESPN league ID (blank to stop)", default="", show_default=False)
            if not lid.strip():
                break
            try:
                teams = ep.teams(lid.strip())
                meta = ep.league_meta(lid.strip())
                name = meta.get("settings", {}).get("name", f"ESPN {lid}")
            except AuthError as exc:
                click.secho(f"  ✗ ESPN rejected that: {exc}", fg="red")
                continue
            except ProviderError as exc:
                click.secho(f"  ✗ Could not load league {lid}: {exc}", fg="red")
                continue
            my_id = ep.my_team_id(lid.strip())
            if my_id:
                who = next((t["name"] for t in teams if t["id"] == my_id), my_id)
                click.secho(f"  ✓ {name} — identified your team as “{who}”", fg="green")
                if not click.confirm("    Is that right?", default=True):
                    my_id = None
            if not my_id:
                for i, t in enumerate(teams, 1):
                    click.echo(f"      [{i}] {t['name']}")
                pick = click.prompt("    Which team is yours?", type=int)
                my_id = teams[pick - 1]["id"]
            stakes = ask_league_stakes(name, "espn")
            leagues.append(LeagueConfig(
                platform=Platform.ESPN.value, league_id=str(lid).strip(), name=name,
                my_team_id=str(my_id), season=season, **stakes))

    if leagues:
        cfg.leagues = leagues

    # -- Preferences --------------------------------------------------------
    hr("PREFERENCES")
    tz = click.prompt("  Timezone", default=cfg.timezone or "America/Los_Angeles")
    if tz not in available_timezones():
        click.secho(f"  ✗ Unknown timezone {tz}; keeping {cfg.timezone}", fg="red")
    else:
        cfg.timezone = tz
    cfg.minutes_before = click.prompt("  Minutes before kickoff to notify", type=int,
                                      default=cfg.minutes_before)

    cfg.morning_summary = click.confirm(
        "  Also send a gameday-morning heads-up (in time to fix your lineup)?",
        default=cfg.morning_summary)
    if cfg.morning_summary:
        cfg.morning_summary_time = click.prompt(
            "  What time (24-hour HH:MM, local)?", default=cfg.morning_summary_time)

    # -- Notifications ------------------------------------------------------
    hr("NOTIFICATIONS  (all free options)")
    click.echo("  1. iMessage  — native macOS Messages; Mac must be awake and signed in")
    click.echo("  2. ntfy      — free push to the ntfy iPhone app; works with the Mac asleep")
    click.echo("  3. Telegram  — free bot messages; works anywhere")
    click.echo("  4. console   — print only, sends nothing")
    click.echo("  5. Twilio    — OPTIONAL, paid, not required")
    choice = click.prompt("  Choose", type=click.Choice(["1", "2", "3", "4", "5"]), default="2")
    cfg.notifier = {"1": "imessage", "2": "ntfy", "3": "telegram",
                    "4": "console", "5": "twilio"}[choice]
    _configure_notifier(cfg.notifier)

    path = cfg.save()
    hr("SAVED")
    click.secho(f"  {path}", fg="green")
    return cfg


def _configure_notifier(name: str) -> None:
    if name == "imessage":
        to = click.prompt("  Your iPhone number (+15551234567) or Apple ID email")
        write_env({"IMESSAGE_TO": to.strip(), "NOTIFIER": "imessage"})
        click.secho("  ⚠ macOS will ask to allow Automation of Messages the first time. "
                    "The Mac must be awake and signed into Messages at kickoff.", fg="yellow")
    elif name == "ntfy":
        server = click.prompt("  ntfy server", default=os.getenv("NTFY_SERVER", "https://ntfy.sh"))
        current = os.getenv("NTFY_TOPIC", "")
        suggested = current or f"ff-root-{secrets.token_hex(8)}"
        click.echo("  Your topic is a shared secret — anyone who knows it can read your")
        click.echo("  notifications on the public server. A random one is generated for you.")
        topic = click.prompt("  ntfy topic", default=suggested)
        token = click.prompt("  Access token (blank if using public ntfy.sh)",
                             default="", show_default=False, hide_input=True)
        write_env({"NTFY_SERVER": server.strip(), "NTFY_TOPIC": topic.strip(),
                   "NTFY_TOKEN": token.strip(), "NOTIFIER": "ntfy"})
        click.secho("\n  On your iPhone: install “ntfy” from the App Store → + → "
                    "Subscribe to topic →", fg="green")
        click.secho(f"      {topic}", fg="green", bold=True)
        if server.rstrip("/") != "https://ntfy.sh":
            click.secho(f"      (use server {server})", fg="green")
    elif name == "telegram":
        click.echo("  In Telegram: message @BotFather → /newbot → copy the token.")
        token = click.prompt("  Bot token", hide_input=True)
        write_env({"TELEGRAM_BOT_TOKEN": token.strip(), "NOTIFIER": "telegram"})
        click.echo("  Now send your new bot any message (say 'hi'), then press Enter.")
        click.pause()
        try:
            chats = build("telegram").discover_chat_id()
        except Exception as exc:  # noqa: BLE001
            chats = []
            click.secho(f"  Could not auto-detect: {exc}", fg="yellow")
        if len(chats) == 1:
            chat_id = chats[0]["id"]
            click.secho(f"  ✓ Found chat {chats[0]['name']} ({chat_id})", fg="green")
        elif chats:
            for i, c in enumerate(chats, 1):
                click.echo(f"    [{i}] {c['name']} ({c['id']})")
            chat_id = chats[click.prompt("  Which chat?", type=int) - 1]["id"]
        else:
            chat_id = click.prompt("  Chat ID")
        write_env({"TELEGRAM_CHAT_ID": str(chat_id)})
    elif name == "twilio":
        click.secho("  Twilio is optional and costs money. Everything works without it.",
                    fg="yellow")
        write_env({
            "TWILIO_ACCOUNT_SID": click.prompt("  Account SID", default=""),
            "TWILIO_AUTH_TOKEN": click.prompt("  Auth token", default="", hide_input=True),
            "TWILIO_FROM": click.prompt("  From number", default=""),
            "TWILIO_TO": click.prompt("  To number", default=""),
            "NOTIFIER": "twilio",
        })
    else:
        write_env({"NOTIFIER": "console"})

    ok, why = build(name).available()
    click.secho(f"  {'✓ configured' if ok else '✗ ' + why}", fg="green" if ok else "red")


def _registry(season: int):
    click.secho("\n  Loading the NFL player universe (cached for 12h)…", fg="bright_black")
    return build_registry(season)
