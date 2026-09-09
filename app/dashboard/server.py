"""Lightweight local dashboard (stdlib only - no web framework dependency).

    fantasy-agent dashboard

Serves on 127.0.0.1 only.  Data is recomputed on every page load, so a refresh
is always live.
"""

from __future__ import annotations

import html
import json
import logging
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from ..analysis import Analyzer, GameGuide
from ..config import AppConfig
from ..formatting import phone_message, phone_title, slot_word
from ..leverage import compute_leverage

log = logging.getLogger(__name__)

CSS = """
:root{--bg:#0f1216;--card:#171b21;--line:#262c35;--fg:#e7ecf3;--dim:#8b97a8;
--grn:#37d67a;--red:#ff5f6d;--yel:#ffc857;--blu:#5aa9ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text",Segoe UI,sans-serif}
header{padding:20px 24px;border-bottom:1px solid var(--line);display:flex;
justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:12px}
h1{font-size:19px;margin:0;letter-spacing:.2px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:1.2px;color:var(--dim);
margin:28px 0 10px}
main{padding:0 24px 48px;max-width:1180px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px 18px;margin-bottom:14px}
.game-head{display:flex;justify-content:space-between;align-items:baseline;
flex-wrap:wrap;gap:10px;margin-bottom:12px}
.slot{font-weight:700;letter-spacing:.5px}
.dim{color:var(--dim)}
.pill{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;
border:1px solid var(--line);margin-right:6px}
.pos{color:var(--grn);border-color:#1e5c39}.neg{color:var(--red);border-color:#6b2630}
.con{color:var(--yel);border-color:#6b5320}
.player{border-top:1px solid var(--line);padding:12px 0}
.player:first-of-type{border-top:0}
.pname{font-weight:650;font-size:15px}
.score{font-variant-numeric:tabular-nums;font-weight:700}
.bar{height:6px;background:#232a33;border-radius:3px;overflow:hidden;margin-top:6px;width:220px}
.bar i{display:block;height:100%}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.8px;
color:var(--dim);font-weight:600;padding:7px 10px;border-bottom:1px solid var(--line)}
td{padding:7px 10px;border-bottom:1px solid #1e242c}
tr:last-child td{border-bottom:0}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.push{white-space:pre-wrap;background:#101418;border:1px dashed var(--line);
border-radius:8px;padding:12px;font-size:13px;color:#cdd7e4}
a{color:var(--blu)}
"""


def _sign_class(score: float) -> str:
    return "pos" if score >= 4 else "neg" if score <= -4 else "con"


def _bar(prob: float) -> str:
    color = "#37d67a" if prob >= .5 else "#ff5f6d"
    return f'<div class="bar"><i style="width:{prob*100:.0f}%;background:{color}"></i></div>'


def render_game(guide: GameGuide, tz) -> str:
    g = guide.game
    local = g.kickoff.astimezone(tz)
    parts = ['<div class="card">', '<div class="game-head">',
             f'<div><span class="slot">{html.escape(slot_word(g.slot))}</span> '
             f'&nbsp;<span style="font-size:17px">{html.escape(g.matchup)}</span>'
             f'<div class="dim">{local:%a %b %-d · %-I:%M %p %Z} · {html.escape(g.broadcast)}</div></div>',
             f'<div style="text-align:right"><div class="score">${guide.money_at_stake:.0f}</div>'
             f'<div class="dim">buy-ins affected</div>'
             f'<div class="dim">${guide.live_dollars:.0f} live</div></div>',
             '</div>']

    if not guide.relevant:
        parts.append('<div class="dim">Nobody of ours (or against us) is starting here.</div></div>')
        return "".join(parts)

    for p in guide.relevant:
        parts.append('<div class="player">')
        rng = p.range_phrase()
        parts.append(
            f'<div><span class="pname">{html.escape(p.player.name)}</span> '
            f'<span class="dim">{p.player.position}-{p.player.nfl_team}</span> '
            f'<span class="pill {_sign_class(p.score)}">{p.emoji} {html.escape(p.verdict)}</span>'
            f'<span class="dim mono">rooting score {p.score:+.0f} · ${p.dollar_swing:.0f} live · '
            f'{html.escape(p.best_confidence.value)}</span>'
            + (f'<span class="pill con">sweet spot {html.escape(rng)}</span>' if rng else '')
            + '</div>')
        parts.append('<table><tr><th>League</th><th>Side</th><th>Buy-in</th><th>Weight</th>'
                     '<th>Threshold</th><th>Confidence</th><th>Win prob</th><th>Leverage</th></tr>')
        for l in sorted(p.lines, key=lambda x: -x.dollar_swing):
            t = l.threshold
            if t.feasible:
                thr = (f'need {t.qualifier} <b>{t.value:.1f}</b>' if l.mine
                       else f'allow up to <b>{t.value:.1f}</b>')
            else:
                thr = f'<span class="dim">{html.escape(t.impossible_reason)}</span>'
            parts.append(
                f'<tr><td>{html.escape(l.league.name)}</td>'
                f'<td class="{"pos" if l.mine else "neg"}">{"OURS" if l.mine else "AGAINST"}</td>'
                f'<td>${l.league.buy_in_usd:g}</td><td>{l.league.effective_weight:g}</td>'
                f'<td>{thr}</td><td class="dim">{html.escape(t.confidence.value)}</td>'
                f'<td>{l.leverage.win_prob:.0%}{_bar(l.leverage.win_prob)}</td>'
                f'<td>${l.dollar_swing:.0f}<div class="dim">{l.leverage.descriptor}</div></td></tr>')
        parts.append('</table>')
        parts.append(f'<div class="dim" style="margin-top:8px">→ {html.escape(p.narrative())}</div>')
        parts.append('</div>')

    parts.append('<h2>Push preview</h2>')
    parts.append(f'<div class="push">{html.escape(phone_title(guide))}\n\n'
                 f'{html.escape(phone_message(guide))}</div>')
    parts.append('</div>')
    return "".join(parts)


def render_page(cfg: AppConfig, week: Optional[int] = None) -> str:
    an = Analyzer(cfg)
    ctx = an.load_week(week)
    guides = [an.analyze_game(ctx, g) for g in an.primetime(ctx)]

    body = [f'<header><h1>🏈 Primetime Rooting Agent</h1>'
            f'<div class="dim">Week {ctx.week} · {ctx.season} · '
            f'{datetime.now(cfg.tz):%a %-I:%M %p %Z} · data tier: {html.escape(ctx.tier.value)}'
            f' · <a href="?refresh=1">refresh</a></div></header><main>']

    if ctx.errors:
        body.append('<div class="card"><b class="neg">Warnings</b><ul class="dim">'
                    + "".join(f"<li>{html.escape(e)}</li>" for e in ctx.errors) + "</ul></div>")

    body.append("<h2>This week</h2>")
    body += [render_game(g, cfg.tz) for g in guides] or ['<div class="card dim">No primetime games.</div>']

    # -- leagues ------------------------------------------------------------
    body.append("<h2>All leagues</h2><div class='card'><table>"
                "<tr><th>League</th><th>Platform</th><th>Scoring</th><th>Buy-in</th>"
                "<th>Mult</th><th>Weight</th><th>Opponent</th><th>Score</th>"
                "<th>Projection</th><th>Win prob</th></tr>")
    for s in ctx.states:
        if s.error and not s.my_starters:
            body.append(f'<tr><td>{html.escape(s.league.name)}</td>'
                        f'<td colspan="9" class="neg">{html.escape(s.error)}</td></tr>')
            continue
        lev = compute_leverage(s)
        body.append(
            f'<tr><td>{html.escape(s.league.name)}</td><td>{s.league.platform.value}</td>'
            f'<td class="dim">{html.escape(s.league.scoring.format_name)}</td>'
            f'<td>${s.league.buy_in_usd:g}</td><td>{s.league.importance_multiplier:g}</td>'
            f'<td><b>{s.league.effective_weight:g}</b></td>'
            f'<td>{html.escape(s.opponent_name)}</td>'
            f'<td>{s.current_score_mine:.1f} – {s.current_score_opponent:.1f}</td>'
            f'<td>{s.projected_final_mine:.1f} – {s.projected_final_opponent:.1f} '
            f'({s.projected_margin:+.1f})</td>'
            f'<td>{lev.win_prob:.0%}{_bar(lev.win_prob)}</td></tr>')
    body.append("</table></div>")

    # -- exposure -----------------------------------------------------------
    body.append("<h2>Exposure</h2><div class='card'><table>"
                "<tr><th>Player</th><th>Pos</th><th>Team</th><th>Ours</th><th>Against</th>"
                "<th>Raw count</th><th>Weighted value</th><th>Live $</th></tr>")
    for r in an.exposure_table(ctx)[:50]:
        p = r["player"]
        cls = "pos" if r["live"] >= 0 else "neg"
        body.append(
            f'<tr><td>{html.escape(p.name)}</td><td>{p.position}</td><td>{p.nfl_team}</td>'
            f'<td>{", ".join(f"${l.buy_in_usd:g}" for l in r["ours"]) or "—"}</td>'
            f'<td>{", ".join(f"${l.buy_in_usd:g}" for l in r["against"]) or "—"}</td>'
            f'<td class="dim">+{len(r["ours"])}/-{len(r["against"])}</td>'
            f'<td>{r["weighted"]:+.0f}</td><td class="{cls}">{r["live"]:+.1f}</td></tr>')
    body.append("</table></div></main>")

    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Primetime Rooting Agent</title><style>{CSS}</style></head>"
            f"<body>{''.join(body)}</body></html>")


class Handler(BaseHTTPRequestHandler):
    cfg: AppConfig
    week: Optional[int] = None

    def log_message(self, fmt, *args):  # noqa: A003 - quieter server
        log.debug(fmt, *args)

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        wk = int(qs["week"][0]) if "week" in qs else self.week
        try:
            if url.path == "/api/week.json":
                an = Analyzer(self.cfg)
                ctx = an.load_week(wk)
                payload = {
                    "season": ctx.season, "week": ctx.week, "errors": ctx.errors,
                    "games": [
                        {"matchup": g.game.matchup, "slot": g.game.slot.value,
                         "kickoff": g.game.kickoff.isoformat(),
                         "money_at_stake": g.money_at_stake,
                         "players": [{"name": p.player.name, "verdict": p.verdict,
                                      "score": p.score, "sweet_spot": p.sweet_spot,
                                      "narrative": p.narrative()} for p in g.relevant]}
                        for g in (an.analyze_game(ctx, x) for x in an.primetime(ctx))
                    ],
                }
                self._respond(200, json.dumps(payload, indent=2), "application/json")
            elif url.path in ("/", "/index.html"):
                self._respond(200, render_page(self.cfg, wk), "text/html; charset=utf-8")
            else:
                self._respond(404, "not found", "text/plain")
        except Exception as exc:  # noqa: BLE001
            log.exception("dashboard render failed")
            self._respond(500, f"<pre>{html.escape(str(exc))}</pre>", "text/html")

    def _respond(self, code: int, body: str, ctype: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(cfg: AppConfig, *, port: int = 8787, week: Optional[int] = None,
          open_browser: bool = True) -> None:
    handler = type("BoundHandler", (Handler,), {"cfg": cfg, "week": week})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"Dashboard on {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
