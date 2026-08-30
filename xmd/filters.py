"""Content-quality filters, applied once in the fetcher so junk never
reaches the store or the rendered markdown.

X noise is detected by title prefix: the adapters tag retweets "RT @user:"
and replies "R to @user:" (see adapters/twitterapi.py _title()). The
keyword blocklist works the same regardless of source.
"""

from __future__ import annotations

import logging

from .config import Config
from .models import FeedItem

log = logging.getLogger("xmd")

RETWEET_PREFIXES = ("RT by @", "RT @")
REPLY_PREFIXES = ("R to @",)


def _is_self_reply(item: FeedItem, title: str) -> bool:
    """A reply to the author's own account is a thread continuation, not
    conversation noise — threads must survive drop_replies."""
    for prefix in REPLY_PREFIXES:
        if title.startswith(prefix):
            target = title[len(prefix):].split(":", 1)[0].strip().lstrip("@").lower()
            own = {
                item.author.strip().lstrip("@").lower(),
                item.source.strip().lstrip("@").lower(),
            }
            return bool(target) and target in own
    return False


def _keep(item: FeedItem, config: Config) -> bool:
    title = item.title.strip()
    if config.drop_retweets and title.startswith(RETWEET_PREFIXES):
        return False
    if (
        config.drop_replies
        and title.startswith(REPLY_PREFIXES)
        and not _is_self_reply(item, title)
    ):
        return False
    lowered = title.lower()
    if any(kw.lower() in lowered for kw in config.block_keywords):
        return False
    return True


def _normalize(text: str) -> str:
    return " ".join(text.split()).lower()


def _dedup_by_text(items: list[FeedItem]) -> list[FeedItem]:
    """Two items from the same author with identical text are almost always
    one real-world post under two different ids (e.g. an accidental
    double-post) — keep the first, drop the rest. Keyed by author so two
    *different* people posting the same thing is never treated as a
    duplicate."""
    seen: set[tuple[str, str]] = set()
    kept = []
    for item in items:
        content = item.full_text or item.text or item.title
        key = ((item.author or item.source).strip().lower(), _normalize(content))
        if key in seen:
            continue
        seen.add(key)
        kept.append(item)
    return kept


def apply_filters(items: list[FeedItem], config: Config) -> list[FeedItem]:
    kept = [i for i in items if _keep(i, config)]
    kept = _dedup_by_text(kept)
    if len(kept) < len(items):
        log.debug("filters dropped %d of %d item(s)", len(items) - len(kept), len(items))
    return kept
