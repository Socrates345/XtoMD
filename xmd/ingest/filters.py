"""Content-quality filters, applied once in the fetcher so junk never
reaches the store or the rendered markdown.

X noise is detected by title prefix: the adapters tag retweets "RT @user:"
and replies "R to @user:" (see adapters/twitterapi.py _title()). The
settings in sources.yaml (`filters:`) apply to every source; a source's own
`reposts:off`, `only:` and `hide:` (sources/x.md) add to them for its tweets.

A poster's thread is one post: its first tweet. The tweets that continue it
(`FeedItem.thread_root`) are always dropped, whatever `drop_replies` says,
unless the first tweet is neither in this fetch nor stored: then the oldest of
them stands in for it, so a late update to an old tweet still shows.

A post its account has already posted is dropped too: the same words (see
_content), and for a post with pictures the same pictures, byte for byte. Once
in this fetch it is a duplicate; already stored by an earlier fetch, a repeat
(an account that posts the same disclaimer image every day, for one).
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from functools import lru_cache

from ..core.config import Config, Source
from ..core.models import FeedItem

log = logging.getLogger("xmd")

RETWEET_PREFIXES = ("RT by @", "RT @")
REPLY_PREFIXES = ("R to @",)

# why an item is filtered out, in the order the count names them
THREADS, RETWEETS, REPLIES, HIDDEN, NOT_ONLY, DUPLICATES, REPEATS = (
    "thread replies", "retweets", "replies", "hidden keywords", "outside only:", "duplicates", "repeats")

_STATUS = re.compile(r"/status/(\d+)")
# a link to X itself (a post's own photo, video or status link): different on every post, however alike the posts
_X_LINK = re.compile(r"(?:https?://(?:[\w-]+\.)*(?:x|twitter)\.com|\bpic\.(?:x|twitter)\.com)/\S*", re.IGNORECASE)
# X's short link: on a post with pictures, the pictures' own link; elsewhere it may stand for any page
_SHORT_LINK = re.compile(r"https?://t\.co/\S*", re.IGNORECASE)

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


def _thread_replies(items: list[FeedItem], known: Callable[[set[str]], set[str]] | None) -> set[int]:
    """The positions in `items` of the tweets that continue a thread whose first tweet is in `items` or, by `known`,
    stored; of the ones whose first tweet is in neither, all but the oldest. Run on the whole fetch, so a thread
    whose first tweet a keyword hides goes with it."""
    fetched = {m.group(1) for i in items if (m := _STATUS.search(i.url))}
    threads: dict[str, list[int]] = {}
    for k, item in enumerate(items):
        if item.thread_root:
            threads.setdefault(item.thread_root, []).append(k)
    missing = set(threads) - fetched
    shown = fetched | (known(missing) if known and missing else set())
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    drop: set[int] = set()
    for root, ks in threads.items():
        if root not in shown:
            ks = sorted(ks, key=lambda k: items[k].published or oldest)[1:]
        drop.update(ks)
    return drop


def _is_self_reply(item: FeedItem, title: str) -> bool:
    """A reply to the author's own account is a thread continuation, not
    conversation noise, so drop_replies leaves it alone: _thread_replies
    decides whether it goes."""
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


def _content(item: FeedItem) -> str:
    """What makes two posts of one account the same post: its own words and, for a retweet, who wrote the post it
    quotes and what it says, entities decoded, case and spacing ignored, links to X itself left out ("" = no words
    at all, an image-only post). A t.co link is left out only on a post with pictures, where the pictures are
    compared too: on a post of words alone it may be the one thing that tells two articles apart."""
    def plain(text: str) -> str:
        text = _X_LINK.sub(" ", html.unescape(text))
        return " ".join((_SHORT_LINK.sub(" ", text) if item.images else text).lower().split())

    own = plain(item.own_comment)
    if item.retweet_of_author:  # two people's posts saying the same thing are two posts, retweeted or not
        return f"{own}\n@{item.retweet_of_author.lower()}: {plain(item.retweet_of_text)}"
    return own or plain(item.title)  # a feed that gives a title and no text


def _status(item: FeedItem) -> str:
    """The tweet's id, which stays the same whatever form its url takes; the url when it holds none."""
    m = _STATUS.search(item.url)
    return m.group(1) if m else item.url


def _repeats(
    items: list[FeedItem], earlier: list[FeedItem], image_digest: Callable[[str], str | None] | None,
) -> dict[int, str]:
    """{position in `items`: DUPLICATES or REPEATS} for each post its account had already posted: earlier in
    `items`, or in `earlier` (what is stored) as another tweet, since a fetch gets the stored ones again. The same
    post is the same `_content` and, when it has pictures, the same pictures: `image_digest` gives a picture's
    fingerprint (None when it can't be had: then the post is kept). It is asked only when the words match. With
    no `image_digest`, a post with pictures is never a repeat. Keyed by author: two people posting the same thing
    is a trending story, not a repeat."""
    def key(item: FeedItem) -> tuple[str, str]:
        return (item.author or item.source).strip().lower(), _content(item)

    fingerprints: dict[tuple[str, ...], tuple[str, ...] | None] = {}

    def pictures(item: FeedItem) -> tuple[str, ...] | None:
        urls = tuple(item.images)
        if urls not in fingerprints:
            digests = tuple(image_digest(url) for url in urls) if image_digest else (None,)
            fingerprints[urls] = None if None in digests else digests
        return fingerprints[urls]

    def same(a: FeedItem, b: FeedItem) -> bool:
        if not a.images or not b.images:
            return not a.images and not b.images
        if a.images == b.images:
            return True
        theirs = pictures(b)
        return theirs is not None and theirs == pictures(a)

    stored: dict[tuple[str, str], list[FeedItem]] = {}
    for item in earlier:
        stored.setdefault(key(item), []).append(item)
    seen: dict[tuple[str, str], list[FeedItem]] = {}
    found: dict[int, str] = {}
    for k, item in enumerate(items):
        mine = key(item)
        if not mine[1] and not item.images:  # nothing to compare
            continue
        if any(same(item, other) for other in seen.get(mine, ())):
            found[k] = DUPLICATES
        elif any(_status(other) != _status(item) and same(item, other) for other in stored.get(mine, ())):
            found[k] = REPEATS
        else:
            seen.setdefault(mine, []).append(item)
    return found


def apply_filters(
    items: list[FeedItem], config: Config, known: Callable[[set[str]], set[str]] | None = None,
    earlier: Callable[[], list[FeedItem]] | None = None, image_digest: Callable[[str], str | None] | None = None,
) -> list[FeedItem]:
    """`known` answers which of some tweet ids are already stored (see Store.statuses); None = nothing is.
    `earlier` gives the stored posts, to find the repeats among the new ones; `image_digest` a picture's
    fingerprint (see _repeats). None = nothing is stored / pictures are not compared."""
    sources = {s.name: s for s in config.sources}  # an item names its source by display name, as `priority_sources` does
    dropped = dict.fromkeys((THREADS, RETWEETS, REPLIES, HIDDEN, NOT_ONLY, DUPLICATES, REPEATS), 0)
    threads = _thread_replies(items, known)
    passed = []
    for k, item in enumerate(items):
        reason = THREADS if k in threads else _drop_reason(item, config, sources.get(item.source))
        if reason:
            dropped[reason] += 1
        else:
            passed.append(item)
    again = _repeats(passed, earlier() if earlier and passed else [], image_digest)
    for reason in again.values():
        dropped[reason] += 1
    kept = [item for k, item in enumerate(passed) if k not in again]
    if len(kept) < len(items):
        log.info("filtered out %d of %d tweet(s): %s", len(items) - len(kept), len(items),
                 ", ".join(f"{n} {reason}" for reason, n in dropped.items() if n))
    return kept
