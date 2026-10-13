"""Content-quality filters, applied once in the fetcher so junk never
reaches the store or the rendered markdown.

X noise is detected by title prefix: the adapters tag retweets "RT @user:"
and replies "R to @user:" (see adapters/twitterapi.py _title()). The
settings in sources.yaml (`filters:`) apply to every source; a source's own
`reposts:off`, `only:` and `hide:` (sources/x.md) add to them for its tweets.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from functools import lru_cache

from ..core.config import Config, Source
from ..core.models import FeedItem

log = logging.getLogger("xmd")

RETWEET_PREFIXES = ("RT by @", "RT @")
REPLY_PREFIXES = ("R to @",)

# why an item is filtered out, in the order the count names them
RETWEETS, REPLIES, HIDDEN, NOT_ONLY, DUPLICATES = "retweets", "replies", "hidden keywords", "outside only:", "duplicates"

# a keyword counts from the start of a word: no Latin letter (accented or not) or digit right before it, so "ad"
# finds "ad", "ads" and "#ad" but not "made". Scripts written without spaces (Chinese, Japanese) are not held to it.
_WORD_START = r"(?<![0-9A-Za-z\u00C0-\u024F])"


@lru_cache(maxsize=None)
def _keyword(word: str) -> re.Pattern[str]:
    return re.compile(_WORD_START + re.escape(word), re.IGNORECASE)


def _mentions(parts: list[str], keywords: Iterable[str]) -> bool:
    """Whether one of `keywords` is in one of `parts`. Each part is searched on its own, so a keyword can't match
    across the seam between two."""
    return any(_keyword(kw).search(part) for kw in keywords for part in parts)


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


def _drop_reason(item: FeedItem, config: Config, source: Source | None) -> str:
    """Why the item is filtered out, "" when it is kept. `source` is the followed account it came from (None when
    it is no longer in sources/x.md)."""
    title = item.title.strip()
    if title.startswith(RETWEET_PREFIXES) and (config.drop_retweets or (source is not None and not source.reposts)):
        return RETWEETS
    if (
        config.drop_replies
        and title.startswith(REPLY_PREFIXES)
        and not _is_self_reply(item, title)
    ):
        return REPLIES
    # the adapters cut `title` at 200 characters: a keyword deeper in a long tweet, or in the post it quotes, is
    # only in the text
    parts = [p for p in (title, item.full_text or item.text, item.retweet_of_text) if p]
    if _mentions(parts, [*config.block_keywords, *(source.hide if source else ())]):
        return HIDDEN
    if source is not None and source.only and not _mentions(parts, source.only):
        return NOT_ONLY
    return ""


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
        content = _normalize(item.full_text or item.text or item.title)
        if not content:  # nothing to compare (an image-only post): each one is its own post
            kept.append(item)
            continue
        key = ((item.author or item.source).strip().lower(), content)
        if key in seen:
            continue
        seen.add(key)
        kept.append(item)
    return kept


def apply_filters(items: list[FeedItem], config: Config) -> list[FeedItem]:
    sources = {s.name: s for s in config.sources}  # an item names its source by display name, as `priority_sources` does
    dropped = dict.fromkeys((RETWEETS, REPLIES, HIDDEN, NOT_ONLY, DUPLICATES), 0)
    passed = []
    for item in items:
        reason = _drop_reason(item, config, sources.get(item.source))
        if reason:
            dropped[reason] += 1
        else:
            passed.append(item)
    kept = _dedup_by_text(passed)
    dropped[DUPLICATES] = len(passed) - len(kept)
    if len(kept) < len(items):
        log.info("filtered out %d of %d tweet(s): %s", len(items) - len(kept), len(items),
                 ", ".join(f"{n} {reason}" for reason, n in dropped.items() if n))
    return kept
