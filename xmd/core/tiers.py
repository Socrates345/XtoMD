"""Tiers: which band of the quick digest (and of the LLM export) an item is in.

The rules are the reader's own, not a ranking — every tweet is treated the
same except where a rule below says otherwise. First match wins:

- priority: from a `priority:high` source, or the first post of a poster who had
            been silent for more than SILENCE_DAYS (a plain retweet doesn't
            count, a retweet with the poster's own comment does); always shown
            in full
- media:    has images; kept whole, since a text model can't see an image and
            a meme's point may not survive being paraphrased (in a recap
            group too: the pictures help the reader along its themes)
- recap:    from a group marked `recap:on` in sources/x.md; collapsed to the
            global picture, nothing per-tweet
- retweet:  a plain retweet that adds no words of its own; one short line
- regular:  everything else (originals, and quote-tweets with commentary)

Like priority itself, the tier is resolved at render time from what's passed
in, not stored on the item.
"""

from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timedelta

from .models import FeedItem

PRIORITY = "priority"
RECAP = "recap"
MEDIA = "media"
RETWEET = "retweet"
REGULAR = "regular"

SILENCE_DAYS = 15  # a poster this long without a post is worth reading in full when they come back


def mark_after_silence(
    items: list[FeedItem], post_times: dict[str, list[datetime]], days: int = SILENCE_DAYS,
    history_start: datetime | None = None,
) -> int:
    """Set `after_silence` on each item whose poster's previous post came more than `days` days
    earlier. `post_times` is {source: sorted publish times} (Store.post_times) and holds each
    item's previous post while the database still has it; any post counts as a post, plain retweets too. A poster
    with no earlier post in it has been silent at least since `history_start`, where the database's history begins
    (it forgets what came before, see store.KEEP_DAYS): that is their previous post. Without one (an empty database)
    such a poster is left alone. A handle added after that start is flagged on its first post: it looks exactly like
    one that has been quiet that long. Returns how many were set."""
    marked = 0
    for item in items:
        if item.published is None:
            continue
        times = post_times.get(item.source, [])
        k = bisect_left(times, item.published)  # times[k - 1] is the latest strictly before this post
        previous = times[k - 1] if k else history_start
        item.after_silence = previous is not None and item.published - previous > timedelta(days=days)
        marked += item.after_silence
    return marked


def in_recap_group(item: FeedItem, recap_groups: frozenset[str]) -> bool:
    return bool(item.group) and item.group in recap_groups


def tier_of(
    item: FeedItem,
    priority_sources: frozenset[str] = frozenset(),
    recap_groups: frozenset[str] = frozenset(),
) -> str:
    if item.source in priority_sources or (item.after_silence and not item.is_pure_retweet):
        return PRIORITY
    if item.images:
        return MEDIA
    if in_recap_group(item, recap_groups):
        return RECAP
    if item.is_pure_retweet:
        return RETWEET
    return REGULAR
