"""Tiers: which band of the quick digest (and of the LLM export) an item is in.

The rules are the reader's own, not a ranking — every tweet is treated the
same except where a rule below says otherwise. First match wins:

- priority: from a `| priority` source; always shown in full
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

from .models import FeedItem

PRIORITY = "priority"
RECAP = "recap"
MEDIA = "media"
RETWEET = "retweet"
REGULAR = "regular"


def tier_of(
    item: FeedItem,
    priority_sources: frozenset[str] = frozenset(),
    recap_groups: frozenset[str] = frozenset(),
) -> str:
    if item.source in priority_sources:
        return PRIORITY
    if item.group and item.group in recap_groups:
        return RECAP
    if item.images:
        return MEDIA
    if item.is_pure_retweet:
        return RETWEET
    return REGULAR
