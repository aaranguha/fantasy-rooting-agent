"""Bluesky buzz detection: is one of our players suddenly being talked about,
and - roughly - why (injury, big play, or news)?

Bluesky's `app.bsky.feed.searchPosts` is public and unauthenticated, so this
needs no token. We search each tracked player's name, keep a rolling baseline
of how much chatter is normal for them, and when the volume spikes we classify
the top posts and push a one-line "here's why he's blowing up".

This is a *signal*, not a source of truth. An injury spike here is an early
warning that primes the injury state machine (`injuries.py`); the official
ruling still comes from ESPN's game feed a few minutes later.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional

from .models import CanonicalPlayer
from .providers.base import AuthError, HttpClient, ProviderError

log = logging.getLogger(__name__)

#: Bluesky's public search host 403s datacenter IPs; the authenticated entryway
#: does not. So this uses an app password (two free-to-create throwaway creds)
#: rather than the unauthenticated public API.
ENTRYWAY = "https://bsky.social"
CREATE_SESSION = f"{ENTRYWAY}/xrpc/com.atproto.server.createSession"
SEARCH_PATH = "/xrpc/app.bsky.feed.searchPosts"
SESSION_FILE = "bsky_session.json"
SESSION_TTL = 90 * 60      # refresh the access token well inside its ~2h life

#: How far back a single check looks.
WINDOW_MINUTES = 20
#: A spike must clear both bars: this multiple of the player's baseline...
SPIKE_MULTIPLE = 3.0
#: ...and this many posts in absolute terms (so a baseline of ~0 still needs real volume).
SPIKE_FLOOR = 12
#: Don't trust a baseline until we've sampled a player this many times.
MIN_SAMPLES = 3
#: Smoothing for the exponential moving average of posts-per-window.
EMA_ALPHA = 0.3
#: Never re-alert the same player+category inside this many minutes.
COOLDOWN_MINUTES = 30


class BuzzCategory(str, Enum):
    INJURY = "injury"
    BIG_PLAY = "big_play"
    NEWS = "news"
    UNCLEAR = "unclear"

    @property
    def label(self) -> str:
        return {"injury": "INJURY", "big_play": "BIG PLAY", "news": "NEWS",
                "unclear": "TRENDING"}[self.value]

    @property
    def emoji(self) -> str:
        return {"injury": "🚑", "big_play": "🔥", "news": "📰", "unclear": "📈"}[self.value]


_KEYWORDS: dict[BuzzCategory, tuple[str, ...]] = {
    BuzzCategory.INJURY: (
        "injury", "injured", "hurt", "carted", "cart ", "limp", "limped", "limping",
        "did not return", "will not return", "won't return", "questionable to return",
        "ruled out", "left the game", "leaves the game", "exited", "blue tent",
        "medical tent", "locker room", "x-ray", "xray", "mri", "concussion",
        "ankle", "knee", "hamstring", "groin", "shoulder", "acl", "achilles",
        "strain", "sprain", "non-contact", "grabbing his", "walked off", "helped off",
        "stretcher", "air cast", "down on the field", "trainers",
    ),
    BuzzCategory.BIG_PLAY: (
        "touchdown", " td ", " td!", "td!", "to the house", "pick six", "pick-6",
        "walk-in", "what a catch", "are you kidding", "unreal", "ridiculous catch",
        "one-handed", "hurdle", "truck", "trucked", "house call", "long touchdown",
        "explosive", "highlight", " yards", "yard td", "yard touchdown", "dime",
        "wow", "🔥", "insane", "special",
    ),
    BuzzCategory.NEWS: (
        "traded", "trade", "acquired", "released", "waived", "cut ", "signed",
        "suspended", "suspension", "benched", "benching", "activated", "promoted",
        "elevated", "ruled inactive", "healthy scratch", "sources:", "per sources",
        "sources tell", "breaking", "reports:", "according to", "has requested",
        "contract", "extension", "fined", "arrested", "ejected", "deactivated",
    ),
}

#: Beat reporters / insiders whose displayName we treat as high-signal on sight.
#: Matched case-insensitively as a substring of the post author's display name.
REPORTER_NAMES = (
    "adam schefter", "ian rapoport", "tom pelissero", "mike garafolo", "field yates",
    "jeremy fowler", "dianna russini", "diana russini", "albert breer", "jordan schultz",
    "josina anderson", "mike florio", "ari meirov", "dov kleiman", "james palmer",
    "cameron wolfe", "aaron wilson", "jonathan jones", "mike jones", "kalyn kahler",
    "nick shook", "tyler dunne", "jane slater", "jay glazer", "peter schrager",
    "connor hughes", "matt barrows", "jordan raanan", "ian o'connor",
)

_WS = re.compile(r"\s+")


@dataclass
class Post:
    text: str
    handle: str
    display_name: str
    created_at: datetime
    likes: int = 0
    reposts: int = 0
    replies: int = 0
    uri: str = ""

    @property
    def engagement(self) -> int:
        return self.likes + 2 * self.reposts + self.replies

    @property
    def by_reporter(self) -> bool:
        dn = self.display_name.lower()
        return any(name in dn for name in REPORTER_NAMES)

    def age_minutes(self, now: Optional[datetime] = None) -> float:
        now = now or datetime.now(timezone.utc)
        return max(0.0, (now - self.created_at).total_seconds() / 60.0)

    def snippet(self, limit: int = 180) -> str:
        t = _WS.sub(" ", self.text).strip()
        return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


@dataclass
class BuzzResult:
    player: CanonicalPlayer
    posts: list[Post]
    category: BuzzCategory
    count: int
    baseline: float                       # expected posts in the window
    top_post: Optional[Post] = None
    reporter_posts: list[Post] = field(default_factory=list)

    @property
    def is_spike(self) -> bool:
        if self.reporter_posts and self.count >= 3:
            return True
        return (self.count >= SPIKE_FLOOR
                and self.count >= self.baseline * SPIKE_MULTIPLE)

    def headline(self) -> str:
        c = self.category
        tail = "trending, reason unclear" if c == BuzzCategory.UNCLEAR else c.label
        return f"{c.emoji} {self.player.name} is blowing up on Bluesky — {tail}"

    def body(self) -> str:
        lines = []
        quote = self._best_quote()
        if quote:
            who = quote.display_name or f"@{quote.handle}"
            tag = " · reporter" if quote.by_reporter else ""
            lines.append(f"“{quote.snippet()}” — {who}{tag} "
                         f"({quote.reposts} reposts)")
        usual = (f"usually ~{self.baseline:.0f}" if self.baseline >= 1
                 else "he's normally quiet")
        lines.append(f"{self.count} posts in the last {WINDOW_MINUTES}m ({usual}).")
        if self.category == BuzzCategory.INJURY:
            lines.append("Watching his game status now.")
        return "\n".join(lines)

    def _best_quote(self) -> Optional[Post]:
        pool = self.reporter_posts or self.posts
        return max(pool, key=lambda p: p.engagement, default=None)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def bluesky_configured() -> bool:
    return bool(os.getenv("BLUESKY_HANDLE", "").strip()
                and os.getenv("BLUESKY_APP_PASSWORD", "").strip())


class BlueskyClient:
    def __init__(self, http: Optional[HttpClient] = None, *,
                 cache_dir: Optional[Path] = None) -> None:
        self.http = http or HttpClient()
        self.cache_dir = cache_dir
        self._token: Optional[str] = None
        self._auth_tried = False      # one createSession attempt per client, max

    @property
    def enabled(self) -> bool:
        return bluesky_configured()

    # -- auth -------------------------------------------------------------
    def _session_file(self) -> Optional[Path]:
        return (self.cache_dir / SESSION_FILE) if self.cache_dir else None

    def _cached_token(self) -> Optional[str]:
        f = self._session_file()
        if not f or not f.exists():
            return None
        try:
            blob = json.loads(f.read_text())
        except (ValueError, OSError):
            return None
        if time.time() - blob.get("ts", 0) > SESSION_TTL:
            return None
        return blob.get("accessJwt")

    def _authenticate(self, *, force: bool = False) -> Optional[str]:
        if not force:
            tok = self._token or self._cached_token()
            if tok:
                self._token = tok
                return tok
            if self._auth_tried:
                return None      # already failed once this run; don't hammer it
        self._auth_tried = True
        handle = os.getenv("BLUESKY_HANDLE", "").strip()
        pw = os.getenv("BLUESKY_APP_PASSWORD", "").strip()
        if not (handle and pw):
            return None
        try:
            data = self.http.post(CREATE_SESSION,
                                  json_body={"identifier": handle, "password": pw})
        except (ProviderError, AuthError) as exc:
            log.warning("Bluesky login failed: %s", exc)
            return None
        tok = data.get("accessJwt")
        self._token = tok
        f = self._session_file()
        if f and tok:
            try:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(json.dumps({"accessJwt": tok, "ts": time.time()}))
            except OSError:
                pass
        return tok

    # -- search ---------------------------------------------------------
    def search(self, query: str, *, limit: int = 100) -> list[dict]:
        if not self.enabled:
            return []
        token = self._authenticate()
        for attempt in (0, 1):
            if not token:
                return []
            try:
                data = self.http.get(f"{ENTRYWAY}{SEARCH_PATH}",
                                     params={"q": query, "sort": "latest", "limit": limit},
                                     headers={"Authorization": f"Bearer {token}"},
                                     cache_ttl=30)
                return (data or {}).get("posts") or []
            except AuthError:
                if attempt == 0:
                    token = self._authenticate(force=True)
                    continue
                return []
            except ProviderError as exc:
                log.warning("Bluesky search failed for %r: %s", query, exc)
                return []
        return []


def _parse_post(raw: dict) -> Optional[Post]:
    rec = raw.get("record") or {}
    author = raw.get("author") or {}
    created = rec.get("createdAt") or raw.get("indexedAt")
    if not created:
        return None
    try:
        ts = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None
    return Post(
        text=str(rec.get("text") or ""),
        handle=str(author.get("handle") or ""),
        display_name=str(author.get("displayName") or ""),
        created_at=ts,
        likes=int(raw.get("likeCount") or 0),
        reposts=int(raw.get("repostCount") or 0),
        replies=int(raw.get("replyCount") or 0),
        uri=str(raw.get("uri") or ""),
    )


# Broad "this really is about football" lexicon, to survive name collisions.
_FOOTBALL_HINT = re.compile(
    r"\b(nfl|touchdown|td|yards?|yard|catch|reception|target|carr(y|ies)|snap|sack|"
    r"fumble|interception|pick|red\s?zone|quarterback|qb|rb|wr|te|fantasy|"
    r"inactive|questionable|doubtful|injur|game|drive|end\s?zone|field goal|"
    r"offense|defense|coach|lineup|start(ing|er)?|bench)\b",
    re.I,
)


def _relevant(post: Post, player: CanonicalPlayer) -> bool:
    text = post.text.lower()
    parts = [p for p in _WS.split(player.name.lower()) if len(p) > 1]
    last = parts[-1] if parts else player.name.lower()
    if last not in text:
        return False
    team = player.nfl_team.lower()
    return bool(_FOOTBALL_HINT.search(text) or (team and team in text))


def gather(client: BlueskyClient, player: CanonicalPlayer, *,
           now: Optional[datetime] = None, window: int = WINDOW_MINUTES) -> list[Post]:
    """Recent, football-relevant posts about one player, newest first."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=window)
    raw = client.search(f'"{player.name}"')
    out = []
    for r in raw:
        p = _parse_post(r)
        if p is None or p.created_at < cutoff:
            continue
        if _relevant(p, player):
            out.append(p)
    out.sort(key=lambda p: p.created_at, reverse=True)
    return out


# ---------------------------------------------------------------------------
# Classify
# ---------------------------------------------------------------------------


def classify(posts: Iterable[Post]) -> BuzzCategory:
    """Whichever bucket the chatter leans into, weighted by post engagement."""
    scores = {c: 0.0 for c in _KEYWORDS}
    for post in posts:
        text = f" {post.text.lower()} "
        weight = 1.0 + math.log1p(post.engagement)
        for cat, words in _KEYWORDS.items():
            if any(w in text for w in words):
                scores[cat] += weight
    top = max(scores, key=scores.get)
    if scores[top] <= 0:
        return BuzzCategory.UNCLEAR
    # Require a clear leader; a near-tie across buckets reads as generic noise.
    ordered = sorted(scores.values(), reverse=True)
    if len(ordered) > 1 and ordered[1] >= ordered[0] * 0.8:
        return BuzzCategory.UNCLEAR
    return top


def assess(client: BlueskyClient, player: CanonicalPlayer, baseline: float, *,
           now: Optional[datetime] = None) -> BuzzResult:
    posts = gather(client, player, now=now)
    reporters = [p for p in posts if p.by_reporter]
    result = BuzzResult(
        player=player, posts=posts, category=classify(posts),
        count=len(posts), baseline=baseline,
        reporter_posts=reporters,
    )
    result.top_post = result._best_quote()
    return result


def update_baseline(prev_ema: float, samples: int, observed_count: int) -> float:
    """New posts-per-window EMA. Early samples weight the observation more."""
    if samples <= 0:
        return float(observed_count)
    alpha = max(EMA_ALPHA, 1.0 / (samples + 1))
    return round(alpha * observed_count + (1 - alpha) * prev_ema, 3)
