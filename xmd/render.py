"""Markdown rendering: normalized items -> one full-content archive file.

Items are partitioned into sections (every X interest group is its own
section — a `## group` header in sources/x.md — ungrouped tweets form the
platform's main section), grouped by source inside a section, oldest-first
within a source so a thread reads top-down. Deliberately uncapped and full
text — there's no link-only summary variant, because without an LLM step
there's nothing to summarize down to.
"""

from __future__ import annotations

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


def sectioned(items: list[FeedItem]) -> list[tuple[str, list[FeedItem]]]:
    """Ordered (section_key, items) pairs — a bare platform key ("x") sorts
    before any of its groups ("x/biz") since it's their string prefix, which
    is the order we want (main section first, then groups alphabetically)."""
    keys = sorted({section_key(i) for i in items})
    return [(k, [i for i in items if section_key(i) == k]) for k in keys]


def _grouped_chronological(items: list[FeedItem]) -> list[tuple[str, list[FeedItem]]]:
    """Group by source (alphabetical), oldest first within each source."""
    by_source = sorted(items, key=lambda i: (i.source.lower(), i.source))
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


def build_markdown(items: list[FeedItem], now: datetime, label: str = "") -> str:
    suffix = f" ({label})" if label else ""
    lines = [f"# X to MD — {now.strftime('%Y-%m-%d %H:%M')}{suffix}", ""]
    if not items:
        lines.append("*Nothing new.*")
        return "\n".join(lines) + "\n"

    for key, sec_items in sectioned(items):
        lines.append(f"## {section_label(key)}")
        lines.append("")
        first = True
        # still grouped by source, oldest-first within a source (so a thread
        # reads top-down) — just without a separate "### source" header,
        # since each item's own heading already names its source
        for _source, batch in _grouped_chronological(sec_items):
            for item in batch:
                if not first:
                    lines += ["---", ""]
                first = False
                lines += _item_block(item)
    return "\n".join(lines)
