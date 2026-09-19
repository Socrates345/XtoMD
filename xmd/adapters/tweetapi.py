"""X via tweetapi.com — an alternative paid X backend to twitterapi.io,
selected with `x.backend: tweetapi` in sources.yaml (default stays
"twitterapi"). See adapters/twitterapi.py for the primary backend; this
module implements the exact same fetch() contract so fetcher.py can swap
between them without any other code changing.

Billing differs fundamentally from twitterapi.io: flat monthly request
quotas (e.g. $17/mo for 100,000 calls on the Pro tier) rather than
pay-per-tweet-returned. One /user/tweets call returns ~20 tweets (confirmed
against a live response — not documented anywhere on tweetapi.com's pricing
or API pages).

Two things twitterapi.io doesn't need that this backend does:
- tweetapi.com's /user/tweets takes a numeric userId, not a handle, so every
  fetch first resolves @handle -> id via /user/by-username. Cheap under this
  pricing model (billed per call, not per tweet), so resolving on every
  fetch is simpler than maintaining a persistent handle->id cache.
- Cloudflare fronts api.tweetapi.com and blocks requests carrying no
  browser-like User-Agent (error code 1010, discovered empirically) — every
  call sets one.

Caveat: the exact field names below for a tweet's own timestamp and for
non-video media are inferred from a single live sample (one video tweet),
not from documentation — worth double-checking against a wider sample
before relying on this backend for real delivery.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

from ..config import Source
from ..models import FeedItem, is_echo

log = logging.getLogger("xmd")

BASE_URL = "https://api.tweetapi.com/tw-v2"
PAGE_SIZE = 20  # observed against a live call; undocumented
MAX_RETRIES = 4  # tw-v2 endpoints return no Retry-After/X-RateLimit-* headers — back off blind
RETRY_BACKOFF = 2.0  # seconds; doubles each retry (1x, 2x, 4x, 8x)
RETRYABLE_STATUS_CODES = (429, 403)  # a 403 was observed on /user/by-username even while
# pacing exactly at the documented per-minute cap (see _RateLimiter below) — Cloudflare's
# edge in front of tweetapi.com likely enforces its own, stricter/burst-sensitive rate
# limit distinct from the plan's advertised average, and returns 403 rather than 429 for
# it (a common Cloudflare rate-limiting-rule default). Backing off and retrying gives a
# transient edge-level block a chance to clear instead of failing the whole source.

# Multiplicative cushion applied on top of the strict 60/per_minute spacing (see
# _RateLimiter.configure): trades a bit of throughput for headroom against exactly the
# 403 above, since pacing at the exact nominal rate still isn't safe in practice.
RATE_LIMIT_SAFETY_MARGIN = 1.25


class _RateLimiter:
    """Thread-safe request pacer, shared across every concurrent fetch().

    fetcher.py runs up to MAX_CONCURRENCY sources at once via
    asyncio.to_thread, and each source issues 2+ calls here (resolve-id plus
    one per tweets page) — without pacing, that blows straight through
    tweetapi.com's per-minute cap (10/min on the free tier as of writing)
    well before the retry-on-429/403 backoff in _get() ever gets a chance to
    help. Spacing every call evenly (with RATE_LIMIT_SAFETY_MARGIN headroom)
    keeps the steady-state rate under the cap instead of bursting then
    backing off."""

    def __init__(self):
        self._lock = threading.Lock()
        self._interval = 0.0  # seconds between requests; 0 = unlimited
        self._next_slot = 0.0

    def configure(self, per_minute: int) -> None:
        with self._lock:
            self._interval = 60.0 / per_minute * RATE_LIMIT_SAFETY_MARGIN if per_minute > 0 else 0.0

    def wait(self) -> None:
        with self._lock:
            if self._interval <= 0:
                return
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._interval
        sleep_for = slot - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)


_rate_limiter = _RateLimiter()

# Cloudflare's bot fingerprinting (error code 1010) blocks requests with no
# browser-like User-Agent before they ever reach the API.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _title(tweet: dict) -> str:
    """Mirrors adapters/twitterapi.py's _title(): identical RT @/R to @
    prefixes, so filters.py's drop_retweets/drop_replies apply the same way
    regardless of which backend is active."""
    text = " ".join(str(tweet.get("text", "")).split())
    retweeted = tweet.get("retweetedTweet")
    if retweeted:
        author = (retweeted.get("author") or {}).get("username", "")
        original = " ".join(str(retweeted.get("text", "")).split())
        return f"RT @{author}: {original}" if author else text
    reply_to = tweet.get("replyTo")
    if reply_to:
        target = (reply_to.get("author") or {}).get("username", "")
        if target:
            return f"R to @{target}: {text}"
    return text


def _retweet_of(tweet: dict) -> tuple[str, str]:
    """(original author, original text) from either a plain retweet
    (retweetedTweet) or a quote tweet (quotedTweet) — tweetapi.com's schema,
    unlike twitterapi.io's, actually distinguishes the two, but both map onto
    FeedItem's single retweet_of_author/retweet_of_text pair the same way."""
    wrapped = tweet.get("retweetedTweet") or tweet.get("quotedTweet")
    if not wrapped:
        return "", ""
    author = (wrapped.get("author") or {}).get("username", "")
    if not author:
        return "", ""
    text = " ".join(str(wrapped.get("text", "")).split())
    return author, text


def _added_comment(tweet: dict, retweet_of_author: str, retweet_of_text: str = "") -> str:
    """Mirrors adapters/twitterapi.py's _added_comment(): a bare retweet's
    own `text` can carry X's auto-generated, truncated "RT @author: text…"
    echo of the original — or the original's text itself — rather than being
    empty. That echo is never real commentary, so it's treated as no comment
    either way."""
    if not retweet_of_author:
        return ""
    raw = " ".join(str(tweet.get("text", "")).split())
    if raw.lower().startswith(f"rt @{retweet_of_author.lower()}:"):
        return ""
    if is_echo(raw, retweet_of_text):
        return ""
    return raw


def _images(tweet: dict) -> list[str]:
    # never let a schema drift break a fetch
    try:
        media = tweet.get("media") or []
        seen: list[str] = []
        for m in media:
            url = m.get("thumbnailUrl") or m.get("url")
            if url and url not in seen:
                seen.append(url)
        return seen
    except (AttributeError, TypeError):
        return []


def _published(tweet: dict) -> datetime | None:
    raw = tweet.get("createdAt")
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _url(tweet: dict, handle: str) -> str:
    explicit = tweet.get("url")
    if explicit:
        return explicit
    author = (tweet.get("author") or {}).get("username") or handle
    tweet_id = tweet.get("id")
    return f"https://x.com/{author}/status/{tweet_id}" if tweet_id else ""


def _headers(api_key: str) -> dict:
    return {"X-API-Key": api_key, "User-Agent": USER_AGENT, "Accept": "application/json"}


def _get(url: str, params: dict, api_key: str, timeout: float) -> dict:
    """GET with retry-on-429/403 (see RETRYABLE_STATUS_CODES), mirroring
    adapters/twitterapi.py's _get() — paced by _rate_limiter first so
    concurrent sources don't blow through tweetapi.com's per-minute cap in
    the first place."""
    delay = RETRY_BACKOFF
    for attempt in range(MAX_RETRIES + 1):
        _rate_limiter.wait()
        resp = httpx.get(url, params=params, headers=_headers(api_key), timeout=timeout)
        if resp.status_code not in RETRYABLE_STATUS_CODES or attempt == MAX_RETRIES:
            resp.raise_for_status()
            return resp.json()
        time.sleep(delay)
        delay *= 2
    raise AssertionError("unreachable")  # loop always returns or raises


def _resolve_user_id(handle: str, api_key: str, timeout: float) -> str:
    resp = _get(f"{BASE_URL}/user/by-username", {"username": handle}, api_key, timeout)
    user_id = (resp.get("data") or {}).get("id")
    if not user_id:
        raise RuntimeError(f"tweetapi.com: could not resolve @{handle} to a user id")
    return user_id


def fetch(
    source: Source,
    api_key: str,
    max_items: int = 20,
    timeout: float = 20.0,
    max_age_hours: int = 48,
    fallback_latest: bool = True,
    rate_limit_per_minute: int = 0,
) -> list[FeedItem]:
    """Same contract as adapters/twitterapi.py's fetch(): newest-first,
    stops paginating past max_age_hours, falls back to the single newest
    tweet when the cutoff would otherwise leave nothing for this source.

    `rate_limit_per_minute` (0 = unlimited) paces every HTTP call this fetch
    makes — and every concurrent fetch() call, since the limiter is shared
    module state — to stay under tweetapi.com's per-minute request cap; see
    x.tweetapi_rate_limit_per_minute in sources.yaml."""
    if not api_key:
        raise ValueError(
            "x.backend is 'tweetapi' but x.tweetapi_api_key (or XMD_TWEETAPI_KEY) "
            "is not set"
        )
    _rate_limiter.configure(rate_limit_per_minute)

    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        if max_age_hours > 0
        else None
    )

    user_id = _resolve_user_id(source.handle, api_key, timeout)

    items: list[FeedItem] = []
    fallback_item: FeedItem | None = None
    cursor = ""
    while len(items) < max_items:
        params = {"userId": user_id}
        if cursor:
            params["cursor"] = cursor
        try:
            data = _get(f"{BASE_URL}/user/tweets", params, api_key, timeout)
        except Exception:
            if items:
                # a later page failed (e.g. 429s outlasted the retry budget) —
                # keep what we already have rather than losing the whole fetch
                log.info(
                    "tweetapi.com: @%s stopped early with %d item(s) (page fetch failed)",
                    source.handle, len(items),
                )
                break
            raise

        tweets = data.get("data") or []
        past_cutoff = False
        for tweet in tweets:
            url = _url(tweet, source.handle)
            if not url:
                continue
            published = _published(tweet)
            title = _title(tweet)
            retweet_of_author, retweet_of_text = _retweet_of(tweet)
            # plain tweets and replies keep the existing title==text behavior;
            # a retweet/quote's own text/full_text is the retweeter's own
            # comment, if any — see _added_comment()
            body_text = (
                _added_comment(tweet, retweet_of_author, retweet_of_text)
                if retweet_of_author
                else title
            )
            candidate = FeedItem(
                source=source.name,
                source_type="x",
                title=title[:200],
                url=url,
                author=source.handle,
                published=published,
                text=body_text,
                full_text=body_text,
                images=_images(tweet),
                group=source.group,
                retweet_of_author=retweet_of_author,
                retweet_of_text=retweet_of_text,
            )
            if fallback_item is None:
                fallback_item = candidate  # newest-first: the very first is the newest
            if cutoff and published and published < cutoff:
                past_cutoff = True
                break
            items.append(candidate)
            if len(items) >= max_items:
                break

        cursor = (data.get("pagination") or {}).get("nextCursor") or ""
        if past_cutoff or not tweets or not cursor or len(items) >= max_items:
            break

    if not items and fallback_item is not None and fallback_latest:
        items.append(fallback_item)

    return items
