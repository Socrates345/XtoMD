"""LLM-ready export: the tiers that need summarizing, as compact numbered lines.

Nothing here calls a model; this is the shared input for whatever engine ends
up reading it (a local model, `claude -p`, a skill…), so the same file can be
put through each and compared. Priority and media items are left out on
purpose: they're shown verbatim in the digest, so a model never sees — and
can't garble — them. The rest is flattened to one line per post (URLs
stripped, retweet echoes removed, clipped) and numbered, so a summary can
cite `[n]` and be re-linked from the map deterministically instead of
trusting a model to reproduce URLs.
"""

from __future__ import annotations

import html
from datetime import datetime

from ..core.models import URL, FeedItem
from .render import _grouped_chronological, section_label, sectioned
from ..core.tiers import MEDIA, PRIORITY, RECAP, REGULAR, RETWEET, tier_of
from .trending import DEFAULT_SIMILARITY, find_stories

# characters kept per post: regular posts carry the news, retweets and recap
# groups only need their gist — and shorter input keeps every call small
DEFAULT_LIMITS = {REGULAR: 400, RETWEET: 220, RECAP: 220}
_BANDS = ((REGULAR, "regular"), (RETWEET, "retweets"), (RECAP, "recap"))


def _tag(item: FeedItem) -> str:
    if not item.retweet_of_author:
        return ""
    return f"{'RT' if item.is_pure_retweet else 'QT'}@{item.retweet_of_author} "


def _flat(item: FeedItem, limit: int) -> str:
    if item.is_pure_retweet:
        text = item.retweet_of_text
    else:
        text = item.own_comment
        if item.retweet_of_author:
            text = f"{text} ⟪{item.retweet_of_text}⟫"
    text = " ".join(URL.sub(" ", html.unescape(text)).split())
    if len(text) > limit:
        text = (text[:limit].rsplit(" ", 1)[0] or text[:limit]).rstrip(" ,;:-–—") + "…"
    return text


def build_export(
    items: list[FeedItem],
    now: datetime,
    label: str = "",
    priority_sources: frozenset[str] = frozenset(),
    recap_groups: frozenset[str] = frozenset(),
    similarity: float = DEFAULT_SIMILARITY,
    limits: dict[str, int] | None = None,
) -> tuple[str, dict[str, dict[str, str]]]:
    """(text, map): the numbered lines, and n -> {url, source, tier, group, words}.
    `words` is the post's full-text word count (Item.word_count), before the
    line was clipped, so length targets can be stated against the real thing."""
    limits = limits or DEFAULT_LIMITS
    tiers = {i.id: tier_of(i, priority_sources, recap_groups) for i in items}
    exported = [i for i in items if tiers[i.id] in limits]
    kept_back = [t for t in (PRIORITY, MEDIA) if any(tiers[i.id] == t for i in items)]

    def tally(names: list[str]) -> str:
        return " · ".join(f"{name} {sum(1 for i in items if tiers[i.id] == name)}" for name in names)

    suffix = f" ({label})" if label else ""
    lines = [
        f"# X to MD export — {now.strftime('%Y-%m-%d %H:%M')}{suffix}",
        f"# {len(items)} items: {len(exported)} below for the model ({tally(list(limits))});"
        f" {len(items) - len(exported)} shown verbatim, not included ({tally(kept_back) or 'none'})",
        "# [n] = post number (see the .map.json next to this file). RT@x = plain retweet of @x,"
        " QT@x = quote-tweet of @x, ⟪…⟫ = the quoted post.",
    ]
    numbers: dict[str, int] = {}
    mapping: dict[str, dict[str, str]] = {}
    for key, sec_items in sectioned(exported):
        lines += ["", f"## {section_label(key)}"]
        for tier, heading in _BANDS:
            band = [i for i in sec_items if tiers[i.id] == tier]
            if not band:
                continue
            lines.append(f"### {heading}")
            for _source, batch in _grouped_chronological(band, priority_sources):
                for item in batch:
                    n = len(numbers) + 1
                    numbers[item.id] = n
                    mapping[str(n)] = {
                        "url": item.url, "source": item.source, "tier": tier, "group": item.group,
                        "words": item.word_count,
                    }
                    lines.append(f"[{n}] {item.source} {_tag(item)}{_flat(item, limits[tier])}")

    # repeated-story hints, restricted to what's in this file: a story whose
    # other posts were kept back (priority/media) has nothing left to point at
    repeated = []
    for story in find_stories(items, similarity):
        present = [i for i in story.items if i.id in numbers]
        if len({i.source for i in present}) >= 2:
            repeated.append("repeated: " + " ".join(f"[{numbers[i.id]}]" for i in present))
    if repeated:
        lines += ["", "## repeated stories", *repeated]
    return "\n".join(lines) + "\n", mapping
