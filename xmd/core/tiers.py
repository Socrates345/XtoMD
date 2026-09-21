"""Tiers: which band of the quick digest (and of the LLM export) an item is in.

The rules are the reader's own, not a ranking — every tweet is treated the
same except where a rule below says otherwise. First match wins:

- priority: from a `| priority` source, or the first post of a poster who had
            been silent for more than SILENCE_DAYS (a plain retweet doesn't
            count, a retweet with the poster's own comment does); always shown
            in full
- recap:    from a group marked `| recap` in sources/x.md; collapsed to the
            global picture, nothing per-tweet
- media:    has images; kept whole, since a text model can't see an image and
            a meme's point may not survive being paraphrased
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
    items: list[FeedItem], post_times: dict[str, list[datetime]], days: int = SILENCE_DAYS
) -> int:
    """Set `after_silence` on each item whose poster's previous post came more than `days` days
    earlier. `post_times` is {source: sorted publish times} (Store.post_times) and must hold each
    item's previous post, however old; any post counts as a post, plain retweets too. A poster with no earlier
    post in it is left alone: a source added yesterday looks exactly like one that has been
    quiet for months, and promoting the first would flood the digest. Returns how many were set."""
    marked = 0
    for item in items:
        times = post_times.get(item.source)
        if not times or item.published is None:
            continue
        k = bisect_left(times, item.published)  # times[k - 1] is the latest strictly before this post
        item.after_silence = k > 0 and item.published - times[k - 1] > timedelta(days=days)
        marked += item.after_silence
    return marked


def tier_of(
    item: FeedItem,
    priority_sources: frozenset[str] = frozenset(),
    recap_groups: frozenset[str] = frozenset(),
) -> str:
    if item.source in priority_sources or (item.after_silence and not item.is_pure_retweet):
        return PRIORITY
    if item.group and item.group in recap_groups:
        return RECAP
    if item.images:
        return MEDIA
    if item.is_pure_retweet:
        return RETWEET
    return REGULAR
