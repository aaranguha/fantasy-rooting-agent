# 🏈 Primetime Rooting Agent

**"Who should I actually be rooting for during TNF, SNF and MNF across all five of my fantasy leagues?"**

A cross-league fantasy rooting *decision engine*. ~15 minutes before every primetime
kickoff it re-reads all five leagues live, works out who you need and who you fear,
solves for the exact points you need (or can afford) from each player, and texts you a
verdict.

It is not an exposure tracker. It weighs **buy-in**, **how close each matchup is**,
**each league's own scoring**, and **how much a given player can actually swing your
win probability** — then tells you what to want.

```
🏈 TNF in 15: SF @ LAR

🟢 Puka Nacua · GO OFF (proj: 19)
   ours: Gary Harris, Dynasties and Dystopia

⚖️ Kyren Williams · WANT 12-15
   ours: Turf Wars
   vs us: Fantasy Football

🔴 Harrison Mevis · UNDER 9
   vs us: Turf Wars

🎯 Turf Wars is tightest (52% to win)
```

No LLM is required — every number is deterministic arithmetic.

---

## 1. Installation

```bash
cd "/Users/aguha2021/Desktop/CS_Projects/Fantasy Agent"

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
```

That installs the `fantasy-agent` command. Verify:

```bash
fantasy-agent --help
fantasy-agent notifiers
```

> Not using the venv? Every command below also works as
> `python3 -m app.cli <command>` from this directory.

**Try the engine before connecting anything.** This runs the real analysis pipeline on
five realistic *fake* leagues built around a real upcoming primetime game:

```bash
fantasy-agent demo          # full report
fantasy-agent demo --push   # the phone-sized version
```

---

## 2. Setup

```bash
fantasy-agent setup
```

The wizard walks through Sleeper → ESPN → buy-ins → timezone → notifications, then
prints all five leagues and your current starters so you can eyeball them.

Config is written to `~/.fantasy-agent/config.json`.
Secrets go to `~/.fantasy-agent/.env` (chmod 600). Neither is ever committed.

---

## 3. What you need to give it

| # | Thing | Where you get it |
|---|-------|------------------|
| 1 | **Sleeper username** | Your Sleeper handle. The agent finds your 3 leagues and your roster automatically. |
| 2 | **2 ESPN league IDs** | The `leagueId=` number in your ESPN league URL. |
| 3 | **`espn_s2` + `SWID` cookies** | Only for **private** ESPN leagues — see below. |
| 4 | **Buy-in for each of the 5 leagues** | e.g. `$100 / $50 / $35 / $20 / $10`. |
| 5 | **Importance multiplier** *(optional)* | `1.0` normal, `1.5` "I care extra", `0.75` casual. Defaults to `1.0`. |
| 6 | **One notification target** | An iPhone number (iMessage), an ntfy topic, or a Telegram bot. All free. |

Your timezone (`America/Los_Angeles`) and lead time (15 min) are already the defaults.

### Getting the ESPN cookies

1. Sign in at **fantasy.espn.com** in Chrome or Safari.
2. Open DevTools → **Application** → **Cookies** → `https://fantasy.espn.com`.
3. Copy the values of **`espn_s2`** (a long URL-encoded string) and **`SWID`**
   (a GUID in curly braces, e.g. `{1A2B3C4D-...}`).
4. Paste them when the wizard asks, or put them in `~/.fantasy-agent/.env`:

```bash
ESPN_S2=AEB...long...string
ESPN_SWID={1A2B3C4D-5E6F-7890-ABCD-EF1234567890}
```

#### Or let the script find them

If you're already signed into fantasy.espn.com in a local browser, skip DevTools:

```bash
pip install cryptography          # only needed for Chrome/Brave/Edge/Arc
python scripts/grab_espn_cookies.py
```

It reads only cookies whose host is `espn.com`, writes `ESPN_S2` and `ESPN_SWID`
straight into `~/.fantasy-agent/.env` (chmod 600), and prints only masked previews —
the full value never reaches your terminal scrollback.

```bash
python scripts/grab_espn_cookies.py --dry-run          # find them, write nothing
python scripts/grab_espn_cookies.py --browser firefox  # force one browser
```

Supports Chrome, Brave, Edge, Arc, Vivaldi, Firefox and Safari. macOS will show a
Keychain prompt for Chromium browsers (click **Always Allow**); Safari additionally
needs your terminal to have Full Disk Access.

These cookies rotate when you sign out of ESPN. If ESPN leagues start failing with an
auth error, re-run the script (or re-copy them) and try again.

**Public** ESPN leagues need no cookies at all.

### League importance is deliberately transparent

```
effective_weight = buy_in_usd × importance_multiplier × season_multiplier
                   └─ static ─┘   └─ yours to set ──┘   └─ from the standings ┘
```

Nothing is hidden behind a made-up AI score. `fantasy-agent leagues` shows all three
numbers, and tells you things like *"Right now, affecting Dynasty matters 5.0× as much
as Family."* A free league gets a nominal $10 base so its multiplier still means
something.

### The season multiplier: leagues matter more or less as the year goes on

A $100 league you're 2-9 in is worth less of your attention tonight than a $20 league
you can still win. Every week, each league's stake is rescaled by what's actually left
on the table:

```
season_multiplier = max( UPSIDE, DOWNSIDE )

UPSIDE   = 0.30 + 1.10 × playoff_odds, bonused by title odds
           → 0.30 when eliminated, ~1.4-1.6 when genuinely contending
DOWNSIDE = loser_punishment × last_place_odds × (0.5 + 1.5 × how_late_it_is)
           → 0 unless the league has a punishment, and it ramps hard into playoff time
```

Playoff and last-place odds come from each team's record and points-per-game: expected
final wins with a normal model on the games remaining, compared against the team on the
playoff bubble (and against the team you'd have to finish below to take last). It's an
approximation — remaining games are modelled against an average opponent rather than by
walking the schedule — and it's labelled as such.

`max()` rather than a sum is the important bit: a team eliminated from contention but
sliding toward the punishment is governed **entirely** by the downside. That's the case
where a league you've stopped caring about suddenly becomes the most urgent thing on
your board.

```
League              Buy-in  Manual  Season   Weight   Why
────────────────────────────────────────────────────────────────────────────────────
Dynasty Money       $  100     ×1    ×1.46      146   playoffs clinched (8-2)
Work League         $   50   ×1.5    ×1.31    98.25   contending (6-4, 85% playoffs)
College Buddies     $   35     ×1    ×0.30     10.5   eliminated (2-8)
Family League       $   20  ×0.75    ×1.68     25.2   💀 PUNISHMENT WATCH — 100% to
                                                         finish last, 3 games left
Reddit Free Roll    $   10     ×1    ×0.89      8.9   on the bubble (5-5)
```

The $20 punishment league now outweighs the $35 league you're dead in — which is
correct, and is not something a pure buy-in model can express.

```bash
fantasy-agent standings    # every league's odds and exactly why it's weighted that way
```

**Configuring it**, per league in `~/.fantasy-agent/config.json`:

| Field | Meaning |
|---|---|
| `loser_punishment` | `0` = none (default). `1.0` = normal dread, `1.5` = genuinely humiliating, `0.5` = mild ribbing. |
| `punished_places` | How many teams eat it. Default `1` (last only). |
| `dynamic_importance` | `false` pins the league to its static weight all season. |

The setup wizard asks about all three. If standings can't be loaded for a league, its
multiplier stays at `1.0` and the static weight is used — it never guesses.

### Low-priority leagues: mentioned, never in the way

A league you still play but don't want driving your notifications — say a free league
that's low-stakes for you — can be demoted without dropping it:

```bash
fantasy-agent set-priority "I MEAN WE COULDD"              # demote it
fantasy-agent set-priority "I MEAN WE COULDD" --normal     # restore it
```

Nothing about the analysis changes: its buy-in, multiplier, thresholds and win
probabilities all still compute exactly as before, and `fantasy-agent player`,
`fantasy-agent game` and the dashboard show it in full. Only the 15-minute push treats
it differently — a player who's relevant *only* because of a low-priority league gets
folded into a footer block instead of a full one, still labeled by side so it's clear
who's on your team and who isn't:

```
🟢 A.J. Brown · GO OFF (proj: 16)
   ours: Turf Wars, Fantasy Football

🔕 I MEAN WE COULDD (low priority)
   ours: Brock Purdy
   vs us: Davante Adams
```

If a player matters in a low-priority league **and** a real one, he keeps his full
block — the flag only suppresses a player who wouldn't otherwise register at all. It's
stored as `"low_priority": true` on the league in `config.json`; `fantasy-agent leagues`
marks it with 🔕.

---

## 4. Testing your five league mappings

```bash
fantasy-agent leagues     # all 5 leagues: buy-in, multiplier, weight, scoring, opponent
fantasy-agent matchups    # every league's live score, projection, and BOTH lineups
fantasy-agent status      # config, credentials, this week's slate, send history
```

What to check in `fantasy-agent matchups`:

- All five leagues appear, none marked `error`.
- The **opponent name** is your actual week's opponent.
- **"Mine"** really is your team (`my_team_name` in the header line).
- The starters listed are your *current* starters — no bench, IR or taxi players.
- The **Scoring** column reads correctly (PPR / Half-PPR / 6pt pass TD / TE+1 …).

If one league is wrong, fix it in `~/.fantasy-agent/config.json` (`my_team_id`) or
re-run `fantasy-agent setup`.

---

## 5. Testing the rooting calculations

```bash
fantasy-agent tonight                     # next primetime game, full working shown
fantasy-agent week                        # every primetime game this week
fantasy-agent game "BUF MIA"              # one specific game
fantasy-agent game MNF                    # or by slot
fantasy-agent player "Saquon Barkley"     # cross-league deep dive on one player
fantasy-agent exposure                    # every started player, weighted rooting value
fantasy-agent demo                        # the engine on synthetic leagues
```

`fantasy-agent player` shows, per league: the points needed or affordable, the
confidence level, your current win probability, and what happens if he's shut out
versus if he goes off — then a net recommendation and the reason.

The maths is also covered by the test suite:

```bash
pip install -e '.[dev]'
pytest -q          # 214 tests, no network access required
```

Notable cases it locks down:

- A player **ours in a $100 league (need ~18)** and **against us in a $20 league
  (can tolerate ~27)** → **ROOT FOR**, sweet spot ~18–27. Never "neutral".
- A player **ours in a $20 league we're winning by 40** and **against us in a close
  $100 league (tolerate ~16)** → **ROOT AGAINST**, despite owning him.
- Exact MNF thresholds (`need 13.74+`) vs projection-based TNF thresholds
  (`need roughly 17`), and the language difference between them.
- Multiple scoring formats, multiple MNF games, flexed games, a Wednesday opener,
  ESPN auth failure, Sleeper outage, notification retry, duplicate suppression.
- A $100 league you're eliminated in dropping below a $20 league you're contending in.
- A punishment league's importance climbing as the regular season runs out, and the
  punishment downside overriding elimination in the same league.

### How the thresholds work

- **Points needed** (a player of yours): `opponent's projected final − your projected
  final without him`.
- **Points affordable** (their player): `your projected final − their projected final
  without him`.
- Finished players contribute what they actually scored; in-progress players
  contribute banked points plus the projected share of the game left.
- **Confidence** is graded honestly: `EXACT` when nothing else is unresolved,
  down through `HIGH CONFIDENCE` / `PROJECTION-BASED` / `LOW CONFIDENCE`. A Thursday
  threshold never pretends to be Monday-night arithmetic.
- **Win probability** is a normal model over the remaining projected points, with
  per-position variance (QBs steady, defenses wild).
- **Rooting score** sweeps the player from 0 to ~2.4× his projection in 0.5-point
  steps, scores every league at every level, and weights the results by
  `buy_in × multiplier`. The score is how much your weighted win probability moves
  between a dud game and a big game — so a $100 blowout barely registers while a $25
  coin flip matters a lot.
- Leagues are never compared on raw point totals when their scoring differs; the sweep
  is rescaled per league and combined on **win probability**, and any quoted range says
  which league's points it's in.

---

## 6. Testing a notification (free)

Everything defaults to a **dry run** — nothing leaves your machine unless you pass
`--real`.

```bash
fantasy-agent test-notification --sample                      # print only
fantasy-agent test-notification --provider ntfy --sample      # still a dry run
fantasy-agent test-notification --provider ntfy --sample --real   # actually sends
```

### Option A — iMessage (free, native)

```bash
# .env
NOTIFIER=imessage
IMESSAGE_TO=+15551234567        # or your Apple ID email
```

```bash
fantasy-agent test-notification --provider imessage --sample --real
```

macOS will prompt once to allow Automation of Messages. If it doesn't, enable it in
**System Settings → Privacy & Security → Automation → (your terminal) → Messages**.

**Requirements at send time — all of them:** the Mac is powered on, **awake**, logged
into your user account, and Messages.app is signed into your Apple ID. If the lid is
shut at kickoff, you get nothing. Either run `caffeinate -s` / set *Prevent automatic
sleeping* in Battery settings, or use ntfy instead.

### Option B — ntfy push (free, recommended)

Works with your Mac asleep, and later from any cloud host.

1. Install **ntfy** from the App Store on your iPhone.
2. Tap **+ → Subscribe to topic** and use the long random topic the wizard generated.
3. `.env`:

```bash
NOTIFIER=ntfy
NTFY_SERVER=https://ntfy.sh
NTFY_TOPIC=ff-root-7f3a91c2e05b4d76      # SECRET — treat it like a password
# NTFY_TOKEN=tk_...                      # optional, for a private/authenticated server
```

> On public `ntfy.sh` anyone who knows the topic can read *and* publish to it. That is
> why setup generates a 128-bit random name instead of letting you pick `football`.
> For real privacy, self-host ntfy or use an access token.

```bash
fantasy-agent test-notification --provider ntfy --sample --real
```

### Option C — Telegram bot (free)

1. Message **@BotFather** → `/newbot` → copy the token.
2. Send your new bot any message.
3. The wizard auto-detects your chat id (or `fantasy-agent setup` will ask).

```bash
NOTIFIER=telegram
TELEGRAM_BOT_TOKEN=123456:AAH...
TELEGRAM_CHAT_ID=987654321
```

### Option D — console

`NOTIFIER=console` prints and sends nothing. Always available.

### Option E — Twilio (optional, paid)

Supported but never required, never the default, never auto-selected:
`pip install -e '.[twilio]'` and set the four `TWILIO_*` variables.

---

## 7. Keeping it running on your Mac

```bash
fantasy-agent install-launchd
```

That writes `~/Library/LaunchAgents/com.fantasyagent.rooting.plist` and starts the
daemon. It runs at login, restarts on crash, and polls every 30 seconds.

```bash
# check it
launchctl print gui/$(id -u)/com.fantasyagent.rooting | head -20
tail -f ~/.fantasy-agent/daemon.out.log

# stop / remove
fantasy-agent install-launchd --uninstall
```

Or run it in the foreground:

```bash
fantasy-agent daemon              # Ctrl-C to stop
fantasy-agent daemon --dry-run    # builds and logs real guides, sends nothing
```

**What happens at fire time** — nothing is precomputed. When a game enters its window
the agent re-fetches ESPN, Sleeper, the NFL scoreboard, live scores, current starters
and projections, *then* recalculates thresholds, weights, leverage and scenarios, and
only then sends. A guide built on Tuesday is never mailed on Thursday.

**Duplicates** are suppressed via SQLite (`~/.fantasy-agent/agent.sqlite`) and survive
restarts. A *failed* send is not recorded, so the next tick retries it. There's a
12-minute grace window, so if the Mac was asleep exactly at T-15 you still get the
message when it wakes (as long as kickoff hasn't passed).

```bash
fantasy-agent run-once                     # do what the daemon would do, right now
fantasy-agent run-once --game "DAL PHI" --force --dry-run
fantasy-agent reset-sent                   # clear dedupe for this week
```

### Gameday-morning preview

`fantasy-agent daemon` also sends today's primetime game(s) as a preview well before
the 15-minute kickoff push — early enough to still fix a lineup. Default is **9:00 AM
local**, and it only fires on a day that actually has a primetime game.

It's not a separate, shorter summary — it's **the exact same message** the kickoff push
sends, just rendered hours earlier. The only thing that differs is the countdown in the
title, because that's genuinely true (`in 8h` at 9am vs. `in 15` at kickoff):

```
🏈 Primetime in 8h: NE @ SEA          🏈 Primetime in 15: NE @ SEA
                                        (same body, word for word)
🔴 Rhamondre Stevenson · UNDER 14
   vs us: Fantasy Football
...
```

When the real kickoff push later lands, it **deletes the morning preview** (Telegram
only — see below) so you end up with one message per game, not two. The delete happens
*after* the new push is confirmed sent, never before — if the resend somehow fails,
the morning preview is left in place rather than deleting your only copy.

```bash
fantasy-agent morning                    # send today's game(s) right now if due
fantasy-agent morning --dry-run          # preview without sending
fantasy-agent morning --force            # ignore the time/dedupe (testing)
fantasy-agent morning --game "SF LAR"    # preview ONE game right now, any day - implies --force
```

`--game` is the easiest way to see this end to end without waiting for the actual morning:
it sends the preview immediately for whichever game you name, even one that's days out.

Configure in `~/.fantasy-agent/config.json` (or ask for it during `fantasy-agent setup`):

```json
"morning_summary": true,
"morning_summary_time": "09:00"
```

Dedupe is per game, not per day (key `morning:<game_id>` in the same SQLite the kickoff
push uses) — a doubleheader gets two independent previews, each retired at its own
kickoff. A failed send is never recorded, so it retries on the next pass. `.env`
overrides also work: `MORNING_SUMMARY=false` or `MORNING_SUMMARY_TIME=07:30`.

**The delete only works on Telegram** — it has a real `deleteMessage` API. ntfy and
iMessage have no way to recall a sent notification, so on those providers the morning
preview simply stays visible alongside the kickoff push; nothing breaks, you just end
up with two messages instead of one.

### Live in-game updates

Beyond the 15-minute warning, the daemon also polls every in-progress primetime game
(default: every 60s) and alerts when a starter of yours — or your opponent's — makes a
real play. A touchdown rewrites the arithmetic, so the alert recomputes thresholds on
the fresh score rather than repeating the pre-game number:

```
🔥 A.J. BROWN BIG PLAY, LET'S GO! +6.7
➡️ KEEP GOING — need 9 more in Turf Wars.
   Turf Wars (ours): 44% ↑ 59%

😩 UH OH... STEVENSON scored. +7.0
➡️ Need him under 15 the rest of the way (Fantasy Football).
   Fantasy Football (vs us): 92% ↑ 100%
```

The first look at any game just records a baseline silently — it never replays stats
from kickoff. After that, anything scoring **4+ fantasy points in one interval** (a
touchdown, most long plays) triggers an update; routine yardage doesn't.

```bash
fantasy-agent live --once             # one check, right now
fantasy-agent live --dry-run          # preview without sending
fantasy-agent live --threshold 6      # only alert on 6+ point swings
fantasy-agent daemon --no-live        # kickoff + morning digest only, no live updates
```

### Dashboard

```bash
fantasy-agent dashboard          # http://127.0.0.1:8787
```

Per game: root-for / root-against / conflicted, every threshold with its confidence,
money at stake, league weights and matchup leverage — plus an all-leagues table, an
exposure table, and a preview of the exact push you'd receive. Binds to localhost only.

### Running 24/7 without your Mac (GitHub Actions, free)

`launchd` only runs while your Mac is awake and logged in. If you want notifications
to keep firing with the lid closed or the machine off entirely, the process has to run
somewhere that's actually always on. Since nothing here is precomputed — every run
re-reads ESPN, Sleeper and the schedule from scratch — this app is a natural fit for a
**GitHub Actions cron job**: genuinely free, no new accounts beyond GitHub, and it works
with Telegram or ntfy since neither depends on this Mac. (iMessage does *not* work here —
no macOS runner, no Messages.app — which is exactly why Telegram/ntfy exist.)

`.github/workflows/agent.yml` is already in this repo. It runs every 5 minutes (GitHub's
shortest allowed interval), calls `morning`, `run-once` and `live --once` — each is a
no-op unless something is actually due, so this is safe to run constantly — and commits
the updated SQLite dedupe state back to the repo so nothing double-fires across runs.

**One-time setup:**

```bash
# 1. Turn this project into its own git repo (skip if you already have one)
git init
git add -A
git commit -m "Primetime rooting agent"

# 2. Copy your NON-secret league config into the tracked state directory.
#    (buy-ins, league ids, team ids - not a secret; your credentials are not in here)
cp ~/.fantasy-agent/config.json state/config.json
git add state/config.json
git commit -m "Add league config for CI"

# 3. Create a PRIVATE GitHub repo and push
gh repo create fantasy-rooting-agent --private --source=. --push
```

**Then add your secrets** (Settings → Secrets and variables → Actions → New repository
secret, or via `gh secret set`):

```bash
gh secret set ESPN_S2 < <(grep ^ESPN_S2= ~/.fantasy-agent/.env | cut -d= -f2-)
gh secret set ESPN_SWID < <(grep ^ESPN_SWID= ~/.fantasy-agent/.env | cut -d= -f2-)
gh secret set TELEGRAM_BOT_TOKEN < <(grep ^TELEGRAM_BOT_TOKEN= ~/.fantasy-agent/.env | cut -d= -f2-)
gh secret set TELEGRAM_CHAT_ID < <(grep ^TELEGRAM_CHAT_ID= ~/.fantasy-agent/.env | cut -d= -f2-)
# using ntfy instead? set NTFY_SERVER / NTFY_TOPIC the same way, and change
# NOTIFIER=telegram to NOTIFIER=ntfy near the top of .github/workflows/agent.yml
```

**Enable it:** GitHub Actions runs automatically once the workflow file is pushed — no
extra toggle needed for a repo you just created. Verify from the **Actions** tab, or:

```bash
gh workflow run agent.yml     # trigger one run immediately, without waiting for the cron
gh run watch                  # follow it live
```

**Notes worth knowing:**

- A **private** repo's Actions minutes come out of your free 2,000/month quota. Five-minute
  polling is ~288 runs/day at well under a minute each — comfortably inside that budget.
- GitHub can delay a scheduled run by a few minutes under load; the app's own grace
  windows (12 min for kickoff, 3h for the morning preview) absorb that.
- If a repo goes 60 days with no pushes, GitHub auto-disables its scheduled workflows.
  The state-commit step counts as a push, so this won't happen during an active season —
  worth knowing about if you set this up in the off-season and it goes quiet.
- Now that two places can send notifications (this Mac's `launchd` daemon *and* GitHub
  Actions), either stop `fantasy-agent install-launchd --uninstall` locally, or accept
  you might occasionally get the same push from both — the shared dedupe logic prevents
  a true duplicate only if they're reading the *same* `agent.sqlite`, which they aren't
  once both are running independently.
- **Keep the repo private.** `state/config.json` names your leagues and buy-ins; nothing
  in it is a login credential, but there's no reason to make it public.

### Running on a different always-on machine

Anything that stays powered on works too — a Raspberry Pi, a home server, a cheap VPS.
Copy `config.json` and `.env`, set `NOTIFIER=ntfy` or `telegram`, and run
`fantasy-agent daemon` there instead. Same trade-off as GitHub Actions: no iMessage,
since that needs an actual Mac signed into Messages.

---

## Command reference

| Command | What it does |
|---|---|
| `setup` | Interactive wizard: leagues, buy-ins, notifications |
| `leagues` | Buy-ins, multipliers, effective weights, scoring formats |
| `set-priority "name"` | Demote a league so it's analyzed but never crowds the push |
| `standings` | Playoff / title / last-place odds and each season multiplier |
| `matchups` | Live score, projection and both lineups per league |
| `tonight` | Full rooting guide for the next primetime game |
| `week` | Every primetime game this week |
| `week --all-games` | Include the Sunday-afternoon slate too |
| `game "BUF MIA"` | One game, by teams or by slot (`game MNF`) |
| `player "Saquon Barkley"` | Cross-league deep dive on one player |
| `exposure` | Every started player, weighted rooting value |
| `demo` | Full engine on synthetic leagues — no credentials |
| `status` | Config, credentials, slate, send history |
| `notifiers` | Which providers are configured |
| `test-notification` | Send yourself a test (dry run unless `--real`) |
| `run-once` | One scheduler pass, now |
| `morning` | Send/preview today's gameday preview (same as the kickoff push) |
| `live` | Watch for big plays and alert live (`--once`, `--dry-run`) |
| `telegram-chat-id` | Find and save your real Telegram chat id |
| `daemon` | Run forever: morning digest + kickoff push + live updates |
| `reset-sent` | Clear duplicate suppression |
| `dashboard` | Local web UI |
| `install-launchd` | Auto-start on macOS |

---

## Architecture

```
app/
  models.py       normalized League / Exposure / MatchupState / NFLGame
  standings.py    season outlook + the dynamic per-week importance multiplier
  config.py       config.json + .env, secrets never on disk unencrypted in repo
  playerids.py    canonical player identity (ESPN ↔ Sleeper crosswalk)
  thresholds.py   points needed / points affordable + confidence grading
  leverage.py     win probability, matchup closeness, live dollars
  scenarios.py    deterministic performance sweep → weighted utility curve
  rooting.py      categories, sweet spots, plain-English verdicts
  analysis.py     orchestration + graceful degradation
  formatting.py   phone-sized and full-length rendering
  scheduler.py    timezone-aware firing, full refresh at send time
  cli.py          the fantasy-agent command
  demo.py         synthetic leagues for testing the engine
  launchd.py      macOS auto-start
  providers/      base (retry/cache) · espn · sleeper · nfl
  notifiers/      base · console · imessage · ntfy · telegram · twilio
  db/database.py  SQLite dedupe + send log
  dashboard/      local web UI (stdlib only)
tests/            214 tests, fully mocked
```

**Data sources**

| Source | Endpoint | Auth |
|---|---|---|
| Sleeper leagues/rosters/matchups | `api.sleeper.app/v1` | none |
| Sleeper raw stat projections | `api.sleeper.com/projections` | none |
| ESPN leagues | `lm-api-reads.fantasy.espn.com/apis/v3` | `espn_s2` + `SWID` for private |
| NFL schedule & live state | `site.api.espn.com/.../nfl/scoreboard` | none |

Sleeper returns **raw stat lines**, so each league's own `scoring_settings` is applied
to them — a projection in your 6-pt-passing-TD TE-premium league is genuinely that
league's number. ESPN returns points already in league scoring, which we use directly.

ESPN needs `view=mBoxscore` alongside `mMatchupScore` — the latter returns only team
totals, and without the former every roster entry comes back as an anonymous stat line
with no player id. If ESPN players ever show up unnamed again, dump the payload shape
(keys and types only, no values) with:

```bash
python scripts/espn_payload_shape.py <leagueId> [week]
```

**Primetime detection** is derived, never hardcoded: a game qualifies when it stands
alone in its window on a national broadcast. TNF/SNF/MNF are named by Eastern-time day
and hour; everything else (Wednesday openers, Black Friday, Christmas, London games,
double-MNF, flexed games) still gets caught as `PRIMETIME` / `HOLIDAY` /
`INTERNATIONAL`. Configurable via `include_slots` in `config.json`.

**Graceful degradation** — if projections vanish, or a league won't load, the run
continues on whatever tier of data survives and says so, rather than failing.

---

## Security

- `.env`, `config.json`, `*.sqlite` and `cache/` are gitignored. **Never commit them.**
- `~/.fantasy-agent/.env` and `config.json` are written `chmod 600`.
- The ntfy topic is a secret; it is kept out of log lines and result messages.
- Secrets are read from the environment only — nothing is hardcoded.
- The dashboard binds to `127.0.0.1`.
- If you ever paste `espn_s2` somewhere public, sign out of ESPN to rotate it.
