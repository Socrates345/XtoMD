"""X via twitterapi.io — a paid, pay-per-tweet hosted scraper.

No X account, no login, no self-hosting: one API key
(https://twitterapi.io, header X-API-Key) buys GET access to a user's
recent tweets. Priced per tweet returned (not per request), so
fetch.max_per_source sets the bill: up to twice its value per handle, and `0`
keeps a safety ceiling of 100 (see fetcher._fetch_ceiling and
sources.example.yaml).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import page_limit
from ...core.config import Source
from ...core.models import FeedItem, is_echo

log = logging.getLogger("xmd")

BASE_URL = "https://api.twitterapi.io/twitter/user/last_tweets"
PAGE_SIZE = 20  # fixed by the API
MAX_RETRIES = 4  # 429s are common in bursts (fetcher runs up to 10 sources concurrently)
RETRY_BACKOFF = 2.0  # seconds; doubles each retry (1x, 2x, 4x, 8x)


def _title(tweet: dict) -> str:
    """Prefix retweets/replies the way filters.py expects (RETWEET_PREFIXES /
    REPLY_PREFIXES), so drop_retweets/drop_replies apply uniformly across
    backends regardless of how each API represents them."""
    text = " ".join(str(tweet.get("text", "")).split())
    retweeted = tweet.get("retweeted_tweet")
    if retweeted:
        author = (retweeted.get("author") or {}).get("userName", "")
        original = " ".join(str(retweeted.get("text", "")).split())
        return f"RT @{author}: {original}" if author else text
    if tweet.get("isReply") and tweet.get("inReplyToUsername"):
        return f"R to @{tweet['inReplyToUsername']}: {text}"
    return text


def _retweet_of(tweet: dict) -> tuple[str, str]:
    """(original author, original text) when `tweet` wraps a retweet or a
    quote tweet, else ("", ""). twitterapi.io's schema, unlike its `_title()`
    prefixing, does distinguish the two: a plain repost nests under
    `retweeted_tweet`, a quote tweet (commentary attached on top of an
    embedded post — including a quote of someone else's retweet) nests under
    `quoted_tweet`. Both map onto FeedItem's single retweet_of_author/
    retweet_of_text pair so the quoted/retweeted content always renders
    alongside the wrapper's own added comment (see display_body()) instead of
    the comment showing with no context for what it's responding to."""
    wrapped = tweet.get("retweeted_tweet") or tweet.get("quoted_tweet")
    if not wrapped:
        return "", ""
    author = (wrapped.get("author") or {}).get("userName", "")
    if not author:
        return "", ""
    original = " ".join(str(wrapped.get("text", "")).split())
    return author, original


def _added_comment(tweet: dict, retweet_of_author: str, retweet_of_text: str = "") -> str:
    """The retweeter's own added comment — empty for a bare retweet.
    twitterapi.io doesn't distinguish a bare retweet from a quote-tweet: both
    wrap the original under `retweeted_tweet`, and a bare retweet's own
    `text` field is X's auto-generated, truncated "RT @author: text…" echo of
    the original — or, as seen in stored data, the original's text itself —
    not real commentary. Only text that *isn't* such an echo is treated as an
    actual added comment."""
    if not retweet_of_author:
        return ""
    raw = " ".join(str(tweet.get("text", "")).split())
    if raw.lower().startswith(f"rt @{retweet_of_author.lower()}:"):
        return ""
    if is_echo(raw, retweet_of_text):
        return ""
    return raw


def _images(tweet: dict) -> list[str]:
    # covers photo, video, and animated_gif entries (video/gif give a thumbnail
    # frame) — never let a schema drift break a fetch
    try:
        media = (tweet.get("extendedEntities") or {}).get("media") or tweet.get("media") or []
        seen: list[str] = []
        for m in media:
            url = m.get("media_url_https")
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
        return datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y").astimezone(timezone.utc)
    except ValueError:
        return None


def _get(url: str, params: dict, api_key: str, timeout: float) -> dict:
    """GET with retry-on-429: twitterapi.io throttles bursts hard (observed:
    a fresh key 429s on nearly every call under the fetcher's concurrent
    load), and returns no Retry-After header — so back off blind."""
    delay = RETRY_BACKOFF
    for attempt in range(MAX_RETRIES + 1):
        resp = httpx.get(url, params=params, headers={"X-API-Key": api_key}, timeout=timeout)
        if resp.status_code != 429 or attempt == MAX_RETRIES:
            resp.raise_for_status()
            return resp.json()
        time.sleep(delay)
        delay *= 2
    raise AssertionError("unreachable")  # loop always returns or raises


def fetch(
    source: Source,
    api_key: str,
    max_items: int = 20,
    timeout: float = 20.0,
    max_age_hours: int = 48,
    fallback_latest: bool = True,
) -> list[FeedItem]:
    """`max_age_hours` (0 = unlimited) stops paginating the moment a tweet
    older than the cutoff is seen — last_tweets returns newest-first, so
    everything after that point (this page and any further pages) is older
    still. Saves both pointless spend (twitterapi.io bills per tweet
    returned) and stale items reaching the store.

    When that leaves nothing for this source (even the newest tweet is past
    the cutoff) and `fallback_latest` is true, that single newest tweet is
    returned anyway rather than nothing."""
    if not api_key:
        raise ValueError("x.backend is 'twitterapi' but x.api_key (or XMD_TWITTERAPI_KEY) is not set")

    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        if max_age_hours > 0
        else None
    )

    items: list[FeedItem] = []
    fallback_item: FeedItem | None = None
    cursor = ""
    pages, pages_allowed = 0, page_limit(max_items, PAGE_SIZE)
    while len(items) < max_items:
        if pages >= pages_allowed:  # only reached while the window has not ended: see adapters.page_limit
            log.warning(
                "twitterapi.io: @%s stopped after %d pages with older tweets still to come (page guard)",
                source.handle, pages,
            )
            break
        pages += 1
        try:
            data = _get(BASE_URL, {"userName": source.handle, "cursor": cursor}, api_key, timeout)
        except Exception:
            if items:
                # a later page failed (e.g. 429s outlasted the retry budget) —
                # keep what we already have rather than losing the whole fetch
                log.info(
                    "twitterapi.io: @%s stopped early with %d item(s) (page fetch failed)",
                    source.handle, len(items),
                )
                break
            raise
        if data.get("status") == "error":
            raise RuntimeError(f"twitterapi.io error for @{source.handle}: {data.get('msg')}")

        # tweets nest under data.data.tweets; fall back to top-level in case
        # the shape varies by endpoint/version — never let that break a fetch
        tweets = ((data.get("data") or {}).get("tweets")) or data.get("tweets") or []
        past_cutoff = False
        for tweet in tweets:
            url = tweet.get("url", "")
            if not url:
                continue
            published = _published(tweet)
            title = _title(tweet)
            retweet_of_author, retweet_of_text = _retweet_of(tweet)
            # plain tweets and replies keep the existing title==text behavior;
            # a retweet/quote's own text/full_text is the retweeter's added
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

        if past_cutoff or not tweets or not data.get("has_next_page") or len(items) >= max_items:
            break
        cursor = data.get("next_cursor") or ""
        if not cursor:
            break

    if not items and fallback_item is not None and fallback_latest:
        items.append(fallback_item)

    return items
