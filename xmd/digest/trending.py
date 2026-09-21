"""Repeated stories: the same post or story turning up from several sources.

When several of the accounts you follow post (or amplify) the same thing on
their own, that's a sign it matters — so it's highlighted, not merged away.
Detection is deliberately lexical and cheap: two items are the same story when
their content words overlap enough (Jaccard), grouped by *leader* clustering —
an item joins the earliest cluster whose first post it resembles, never
chained through intermediate members — so one loose match can't snowball
unrelated posts into a single story.

For a retweet or quote-tweet the text compared is the *original's*, so an
original and everyone who amplified it land in the same story.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

from ..core.models import URL, FeedItem

DEFAULT_SIMILARITY = 0.4  # Jaccard overlap of content words: 0.6 is near-verbatim, 0.3 starts to pull in loosely related posts
MIN_SHARED = 3  # content words two items must have in common
MIN_TOKENS = 5  # shorter items carry too little to match on
_COMMON_POSTINGS = 200  # a word already leading this many stories no longer narrows the search

_WORD = re.compile(r"[^\W_]+")
_STOP = frozenset(
    "that this with your have from they them what when will about more just like only been "
    "were would could should there their than then into over some very much also even".split()
)
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def story_text(item: FeedItem) -> str:
    """What the item is *about*: the original post for a retweet/quote, else
    the item's own words."""
    if item.retweet_of_author and item.retweet_of_text:
        return item.retweet_of_text
    return item.own_comment


def _tokens(text: str) -> frozenset[str]:
    words = _WORD.findall(URL.sub(" ", text.lower()))
    return frozenset(w for w in words if len(w) > 3 and w not in _STOP)


@dataclass
class Story:
    items: list[FeedItem]  # oldest first; items[0] is the story's leader

    @property
    def sources(self) -> list[str]:
        """Distinct sources, in the order they first appeared."""
        return list(dict.fromkeys(i.source for i in self.items))

    @property
    def first_by_source(self) -> list[FeedItem]:
        """Each distinct source's earliest item in the story."""
        firsts: dict[str, FeedItem] = {}
        for item in self.items:
            firsts.setdefault(item.source, item)
        return list(firsts.values())

    @property
    def text(self) -> str:
        return story_text(self.items[0])


def find_stories(
    items: list[FeedItem], similarity: float = DEFAULT_SIMILARITY, min_sources: int = 2
) -> list[Story]:
    """Stories posted or amplified by at least `min_sources` distinct sources,
    most widely repeated first. A source repeating itself doesn't count."""
    ordered = sorted(items, key=lambda i: (i.published is None, i.published or _EPOCH))
    clusters: list[list[FeedItem]] = []
    leaders: list[frozenset[str]] = []
    index: dict[str, list[int]] = defaultdict(list)  # word -> clusters whose leader has it

    for item in ordered:
        tokens = _tokens(story_text(item))
        if len(tokens) < MIN_TOKENS:
            continue
        shared: Counter[int] = Counter()
        for word in tokens:
            postings = index.get(word, ())
            if len(postings) <= _COMMON_POSTINGS:
                shared.update(postings)
        best, best_score = -1, 0.0
        for cluster, count in shared.items():
            if count < MIN_SHARED:
                continue
            leader = leaders[cluster]
            score = len(tokens & leader) / len(tokens | leader)
            if score >= similarity and score > best_score:
                best, best_score = cluster, score
        if best < 0:
            clusters.append([item])
            leaders.append(tokens)
            for word in tokens:
                index[word].append(len(clusters) - 1)
        else:
            clusters[best].append(item)

    stories = [Story(c) for c in clusters if len({i.source for i in c}) >= min_sources]
    stories.sort(key=lambda s: -len(s.sources))  # stable: ties keep oldest-first order
    return stories
