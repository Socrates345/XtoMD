"""Markdown rendering: normalized items -> one full-content archive file.

Items are partitioned into sections (every X interest group is its own
section — a `## group` header in sources/x.md — ungrouped tweets form the
platform's main section), grouped by source inside a section, oldest-first
within a source so a thread reads top-down. Within a section, retweets sink
below all of that section's normal tweets and are collapsed into a single
`<details>` toggle (one click reveals every retweet in the section, rather
than them consuming scroll space alongside normal tweets); normal tweets are
never collapsed — always fully visible, since per-item collapsing tested
worse ("too many clicks"). `| priority` sources sort first within both the
normal and retweet bands — looked up live by source name against
sources/x.md at render time (not stored on the item), so re-marking a source
as priority reorders even already-fetched tweets on the next digest, no
re-fetch required. A stats line and, for a multi-section digest, a table of
contents linking into each section (via an explicit `<a id>` anchor, not
relying on each renderer's own heading-slug rules) sit at the top so a long
file can be jumped into instead of scrolled through. Deliberately uncapped
and full text — there's no link-only summary variant, because without an
LLM step there's nothing to summarize down to.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from itertools import groupby

from .models import FeedItem

PLATFORM_LABELS = {"x": "X"}


def section_key_for(platform: str, group: str) -> str:
    return f"{platform}/{group}" if group else platform


def section_key(item: FeedItem) -> str:
    return section_key_for(item.source_type, item.group)


def section_label(key: str) -> str:
    """"X", "X / finance", …"""
    platform, _, group = key.partition("/")
    label = PLATFORM_LABELS.get(platform, platform.upper())
    return f"{label} / {group}" if group else label


def _slug(label: str) -> str:
    """"X" -> "x", "X / finance" -> "x-finance": a stable, renderer-agnostic
    anchor id. Heading auto-slug rules (what you'd get from just linking to
    "#x--finance") vary across GitHub/VS Code preview/Obsidian, especially
    for punctuation like "/" — an explicit `<a id>` anchor sidesteps that."""
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "section"


def _count(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def sectioned(items: list[FeedItem]) -> list[tuple[str, list[FeedItem]]]:
    """Ordered (section_key, items) pairs — a bare platform key ("x") sorts
    before any of its groups ("x/biz") since it's their string prefix, which
    is the order we want (main section first, then groups alphabetically)."""
    keys = sorted({section_key(i) for i in items})
    return [(k, [i for i in items if section_key(i) == k]) for k in keys]


def _grouped_chronological(
    items: list[FeedItem], priority_sources: frozenset[str] = frozenset()
) -> list[tuple[str, list[FeedItem]]]:
    """Group by source (priority sources first, then alphabetical), oldest
    first within each source."""
    by_source = sorted(
        items, key=lambda i: (i.source not in priority_sources, i.source.lower(), i.source)
    )
    groups = []
    for source, group in groupby(by_source, key=lambda i: i.source):
        batch = sorted(
            group,
            key=lambda i: i.published or datetime.min.replace(tzinfo=timezone.utc),
        )
        groups.append((source, batch))
    return groups


def _item_block(item: FeedItem) -> list[str]:
    """One self-labeled heading line — source, stable anchor, link, date —
    followed by the body. No separate title line: for X, the title is just
    the tweet text truncated, so showing it above the (untruncated) body
    would repeat it."""
    heading = f"#### {item.source} <!-- item:{item.id} --> [Post Link]({item.url})"
    if item.published:
        heading += f" · {item.published.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"
    lines = [heading, ""]
    body = item.display_body()
    if body:
        lines += [body, ""]
    for image in item.images:
        lines.append(f"![]({image})")
    if item.images:
        lines.append("")
    return lines


def _render_items(items: list[FeedItem], priority_sources: frozenset[str]) -> list[str]:
    """Item blocks for a flat, already-partitioned list of items — grouped
    by source (priority first, alphabetical otherwise), oldest-first within
    a source, "---"-separated. Shared by the normal-tweets band and the
    collapsed retweets band within a section."""
    lines: list[str] = []
    first = True
    for _source, batch in _grouped_chronological(items, priority_sources):
        for item in batch:
            if not first:
                lines += ["---", ""]
            first = False
            lines += _item_block(item)
    return lines


def build_markdown(
    items: list[FeedItem],
    now: datetime,
    label: str = "",
    priority_sources: frozenset[str] = frozenset(),
) -> str:
    suffix = f" ({label})" if label else ""
    lines = [f"# X to MD — {now.strftime('%Y-%m-%d %H:%M')}{suffix}", ""]
    if not items:
        lines.append("*Nothing new.*")
        return "\n".join(lines) + "\n"

    sections = sectioned(items)
    total_sources = len({i.source for i in items})
    lines.append(f"**{_count(len(items), 'item')} · {_count(total_sources, 'source')}**")
    lines.append("")
    # a jump-link per section only pays for itself once there's more than
    # one to jump between
    if len(sections) > 1:
        for key, sec_items in sections:
            sec_label = section_label(key)
            lines.append(f"- [{sec_label}](#{_slug(sec_label)}) — {_count(len(sec_items), 'item')}")
        lines.append("")

    for key, sec_items in sections:
        sec_label = section_label(key)
        lines.append(f'<a id="{_slug(sec_label)}"></a>')
        lines.append(f"## {sec_label}")
        lines.append("")

        normal = [i for i in sec_items if not i.retweet_of_author]
        retweets = [i for i in sec_items if i.retweet_of_author]

        lines += _render_items(normal, priority_sources)

        if retweets:
            if normal:
                lines += ["---", ""]
            lines.append("<details>")
            lines.append(f"<summary>🔁 Retweets — {_count(len(retweets), 'item')}</summary>")
            lines.append("")
            lines += _render_items(retweets, priority_sources)
            lines.append("</details>")
            lines.append("")
    return "\n".join(lines)
