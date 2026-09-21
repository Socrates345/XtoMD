"""Markdown rendering: normalized items -> one full-content archive file.

Items are partitioned into sections (every X interest group is its own
section — a `## group` header in sources/x.md — ungrouped tweets form the
platform's main section), grouped by source inside a section, oldest-first
within a source so a thread reads top-down. Within a section, retweets sink
below all of that section's normal tweets and are collapsed into a single
`<details>` toggle (one click reveals every retweet in the section, rather
than them consuming scroll space alongside normal tweets); normal tweets are
never collapsed — always fully visible, since per-item collapsing tested
worse ("too many clicks"). The retweet items inside the toggle are written as
HTML, not markdown, with no blank line anywhere between `<details>` and
`</details>`: Obsidian doesn't parse markdown inside an HTML block (it shows
the raw `####` / `![]()` source), so the content has to be HTML. That
renders in VS Code / GitHub and in Obsidian's Reading view; Obsidian's Live
Preview doesn't render `<details>` at all and shows the source instead — the
known limit of a `<details>` toggle (an Obsidian `> [!info]-` callout is the
alternative that folds in both of its views, but VS Code can't fold it).
`| priority` sources sort first within both the normal and retweet bands —
looked up live by source name against sources/x.md at render time (not
stored on the item), so re-marking a source as priority reorders even
already-fetched tweets on the next digest, no re-fetch required. A stats
line and, for a multi-section digest, a table of
contents linking into each section sit at the top so a long file can be
jumped into instead of scrolled through. The links target the heading's own
text (see `_fragment`), not a slug of it. Stories that several of your
sources posted or amplified are highlighted in a "Trending" block right
after them (see trending.py) — repetition is a signal, so it's surfaced
rather than merged away. `build_markdown` itself stays uncapped and full
text: the archive.

`build_quick` is its scan layer, written alongside it: every tweet is still
there, but at a glance — one line per regular post, plain retweets shorter
still, and the bands the reader asked to keep whole (priority sources' tweets,
image tweets) shown in full. A recap group (see tiers.py) collapses to a count
and its repeated stories, leaving the per-tweet detail to the full file. It
uses plain headings, lists and links only — no `<details>` — for the same
Obsidian reasons as above.
"""

from __future__ import annotations

import html
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import groupby
from urllib.parse import quote

from .models import URL, FeedItem
from .tiers import MEDIA, PRIORITY, RECAP, REGULAR, RETWEET, tier_of
from .trending import DEFAULT_SIMILARITY, Story, find_stories

PLATFORM_LABELS = {"x": "X"}
DEFAULT_SNIPPET_CHARS = 140
DEFAULT_RETWEET_CHARS = 100
DEFAULT_MEDIA_CHARS = 500
_URL = re.compile(r"https?://[^\s<>\"]+")
_MD_SPECIAL = re.compile(r"([\\`*_\[\]])")


def section_key_for(platform: str, group: str) -> str:
    return f"{platform}/{group}" if group else platform


def section_key(item: FeedItem) -> str:
    return section_key_for(item.source_type, item.group)


def section_label(key: str) -> str:
    """"X", "X / finance", … An "&" in a group name is written "and": Obsidian
    would not follow a contents link to a heading with an "&" (click-tested:
    every other section's link worked, including ones with a comma), so
    neither the heading nor its link carries one."""
    platform, _, group = key.partition("/")
    label = PLATFORM_LABELS.get(platform, platform.upper())
    group = re.sub(r"\s*&\s*", " and ", group).strip()
    return f"{label} / {group}" if group else label


def _fragment(label: str) -> str:
    """The `#…` target of a section's contents link: the heading's own text,
    percent-encoded ("X / finance" -> "X%20/%20finance"). Viewers resolve a
    heading link in different ways, and this one string serves both: Obsidian
    matches it against heading *text* (it ignores slugs and HTML ids), while
    a browser-based preview like VS Code's hands it to the browser, which
    matches the decoded text against an element id — the `<a id>` written
    above each section heading carries exactly that text."""
    return quote(label, safe="/")


def _count(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _stories(n: int) -> str:
    return f"{n} story" if n == 1 else f"{n} stories"


def _md(text: str) -> str:
    """Escape what would turn plain text into markdown (emphasis, links,
    code) — source names like "@_Some_Handle__" and tweets full of `*` and
    `_` shouldn't italicize a one-liner."""
    return _MD_SPECIAL.sub(r"\\\1", text)


def _inline(text: str, limit: int) -> str:
    """One line of plain text for a list entry: URLs dropped, whitespace
    collapsed, cut at `limit` on a word boundary, and escaped so a stray `<`
    or `[` in a tweet can't become a tag or a link. X sends entities
    ("&amp;"); they're decoded so the raw source reads "Trust & Safety", and a
    bare `&` renders as itself."""
    text = " ".join(URL.sub(" ", html.unescape(text)).split())
    if limit and len(text) > limit:
        text = (text[:limit].rsplit(" ", 1)[0] or text[:limit]).rstrip(" ,;:-–—") + "…"
    return _md(text.replace("<", "&lt;").replace(">", "&gt;"))


def _when(item: FeedItem) -> str:
    if not item.published:
        return ""
    return item.published.astimezone(timezone.utc).strftime("%m-%d %H:%M")


def _story_lines(stories: list[Story], priority_sources: frozenset[str], limit: int) -> list[str]:
    """One line per story: how many sources, a link to each source's own
    post, and what it was about. Stories with a priority source (★) first,
    then the most widely repeated — find_stories already orders by that."""
    lines = []
    for story in sorted(stories, key=lambda s: not any(x in priority_sources for x in s.sources)):
        star = "★ " if any(x in priority_sources for x in story.sources) else ""
        links = " · ".join(f"[{_md(i.source)}]({i.url})" for i in story.first_by_source)
        lines.append(f"- **×{len(story.sources)}** {star}{links} — {_inline(story.text, limit)}")
    return lines


def _trending_block(stories: list[Story], priority_sources: frozenset[str], limit: int) -> list[str]:
    if not stories:
        return []
    return [
        '<a id="Trending"></a>',
        "## Trending",
        "",
        f"🔥 {_stories(len(stories))} posted or amplified by 2+ of your sources"
        " (★ = a priority source is in it)",
        "",
        *_story_lines(stories, priority_sources, limit),
        "",
    ]


def sectioned(items: list[FeedItem]) -> list[tuple[str, list[FeedItem]]]:
    """Ordered (section_key, items) pairs — a bare platform key ("x") sorts
    before any of its groups ("x/business") since it's their string prefix, which
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


def _html_text(text: str) -> str:
    """HTML-escape `text` and turn bare URLs into links. Markdown renderers
    autolink bare URLs on their own, raw HTML doesn't — without this a
    retweet's t.co links would stop being clickable inside the toggle."""
    out: list[str] = []
    pos = 0
    for m in _URL.finditer(text):
        url = m.group()
        # trailing sentence punctuation, and a ")" with no "(" to match, belong
        # to the prose around the URL, not the URL — same rule as GFM autolinks
        while url and (url[-1] in ".,;:!?" or (url[-1] == ")" and url.count(")") > url.count("("))):
            url = url[:-1]
        out.append(html.escape(text[pos : m.start()]))
        out.append(f'<a href="{html.escape(url)}">{html.escape(url)}</a>')
        pos = m.start() + len(url)
    out.append(html.escape(text[pos:]))
    return "".join(out)


def _item_html(item: FeedItem) -> list[str]:
    """`_item_block`'s HTML twin, for inside the `<details>` retweet toggle:
    same heading content (source, stable anchor, link, date), body and
    images, but as tags. Never emits a blank line — one would end the HTML
    block early and leave the rest as markdown-inside-HTML, which is what
    this exists to avoid. Text is escaped, so a tweet can't inject tags
    (e.g. a stray `</details>`) into the block."""
    heading = (
        f"<h4>{html.escape(item.source)} <!-- item:{item.id} --> "
        f'<a href="{html.escape(item.url)}">Post Link</a>'
    )
    if item.published:
        heading += f" · {item.published.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"
    lines = [heading + "</h4>"]
    for paragraph in re.split(r"\n\s*\n", item.display_body()):
        text = _html_text(paragraph.strip())
        if text:
            lines.append("<p>" + text.replace("\n", "<br>\n") + "</p>")
    if item.images:
        lines.append("<p>" + "\n".join(f'<img src="{html.escape(i)}">' for i in item.images) + "</p>")
    return lines


def _render_items(
    items: list[FeedItem], priority_sources: frozenset[str], as_html: bool = False
) -> list[str]:
    """Item blocks for a flat, already-partitioned list of items — grouped
    by source (priority first, alphabetical otherwise), oldest-first within
    a source, "---"-separated. Shared by the normal-tweets band (markdown)
    and the collapsed retweets band within a section (`as_html`: tags and
    `<hr>` separators, with no blank lines — see `_item_html`)."""
    block, separator = (_item_html, ["<hr>"]) if as_html else (_item_block, ["---", ""])
    lines: list[str] = []
    first = True
    for _source, batch in _grouped_chronological(items, priority_sources):
        for item in batch:
            if not first:
                lines += separator
            first = False
            lines += block(item)
    return lines


def build_markdown(
    items: list[FeedItem],
    now: datetime,
    label: str = "",
    priority_sources: frozenset[str] = frozenset(),
    similarity: float = DEFAULT_SIMILARITY,
    snippet_chars: int = DEFAULT_SNIPPET_CHARS,
) -> str:
    suffix = f" ({label})" if label else ""
    lines = [f"# X to MD — {now.strftime('%Y-%m-%d %H:%M')}{suffix}", ""]
    if not items:
        lines.append("*Nothing new.*")
        return "\n".join(lines) + "\n"

    sections = sectioned(items)
    stories = find_stories(items, similarity)
    total_sources = len({i.source for i in items})
    lines.append(f"**{_count(len(items), 'item')} · {_count(total_sources, 'source')}**")
    lines.append("")
    # a jump-link per section only pays for itself once there's more than
    # one to jump between
    if len(sections) > 1:
        if stories:
            lines.append(f"- [Trending](#Trending) — {_stories(len(stories))}")
        for key, sec_items in sections:
            sec_label = section_label(key)
            lines.append(f"- [{sec_label}](#{_fragment(sec_label)}) — {_count(len(sec_items), 'item')}")
        lines.append("")
    lines += _trending_block(stories, priority_sources, snippet_chars)

    for key, sec_items in sections:
        sec_label = section_label(key)
        lines.append(f'<a id="{html.escape(sec_label)}"></a>')
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
            lines += _render_items(retweets, priority_sources, as_html=True)
            lines.append("</details>")
            lines.append("")
    return "\n".join(lines)


def _post_line(item: FeedItem, limit: int) -> str:
    text = item.own_comment
    if item.is_pure_retweet:
        text = f"🔁 @{item.retweet_of_author}: {item.retweet_of_text}"
    elif item.retweet_of_author:  # a quote with commentary: keep what it's responding to
        text = f"{text} ↩ @{item.retweet_of_author}: {item.retweet_of_text}"
    head = " · ".join(
        p for p in (f"**{_md(item.source)}**", _when(item), _inline(text, limit)) if p
    )
    return f"- {head} [↗]({item.url})"


def _retweet_line(item: FeedItem, limit: int) -> str:
    return (
        f"- 🔁 **{_md(item.source)}** → @{_md(item.retweet_of_author)}"
        f" · {_inline(item.retweet_of_text, limit)} [↗]({item.url})"
    )


def build_quick(
    items: list[FeedItem],
    now: datetime,
    label: str = "",
    priority_sources: frozenset[str] = frozenset(),
    recap_groups: frozenset[str] = frozenset(),
    full_name: str = "",
    similarity: float = DEFAULT_SIMILARITY,
    snippet_chars: int = DEFAULT_SNIPPET_CHARS,
    retweet_chars: int = DEFAULT_RETWEET_CHARS,
    media_chars: int = DEFAULT_MEDIA_CHARS,
) -> str:
    """The scan layer of a digest (see the module docstring). `full_name` is
    the filename of the full digest written next to it, linked from the top
    and from any collapsed recap group."""
    suffix = f" ({label})" if label else ""
    lines = [f"# X to MD quick — {now.strftime('%Y-%m-%d %H:%M')}{suffix}", ""]
    if not items:
        lines.append("*Nothing new.*")
        return "\n".join(lines) + "\n"

    tiers = {i.id: tier_of(i, priority_sources, recap_groups) for i in items}
    counts = Counter(tiers.values())
    sections = sectioned(items)
    full_link = f"[{full_name}]({full_name})" if full_name else ""

    # a story made only of recap-group posts belongs to that group's section;
    # one that also touches anything else is a story for the whole digest
    stories = find_stories(items, similarity)
    recap_stories: dict[str, list[Story]] = defaultdict(list)
    general: list[Story] = []
    for story in stories:
        if all(tiers[i.id] == RECAP for i in story.items):
            recap_stories[section_key(story.items[0])].append(story)
        else:
            general.append(story)

    stats = f"**{_count(len(items), 'item')} · {_count(len({i.source for i in items}), 'source')}**"
    if full_link:
        stats += f" · full text: {full_link}"
    mix = [
        (PRIORITY, f"★ {counts[PRIORITY]} priority"),
        (MEDIA, f"🖼 {counts[MEDIA]} with images"),
        (REGULAR, _count(counts[REGULAR], "post")),
        (RETWEET, _count(counts[RETWEET], "retweet")),
        (RECAP, f"{counts[RECAP]} in recap groups"),
    ]
    lines += [stats, " · ".join(text for tier, text in mix if counts[tier]), ""]
    if len(sections) > 1:
        if general:
            lines.append(f"- [Trending](#Trending) — {_stories(len(general))}")
        for key, sec_items in sections:
            sec_label = section_label(key)
            lines.append(f"- [{sec_label}](#{_fragment(sec_label)}) — {_count(len(sec_items), 'item')}")
        lines.append("")
    lines += _trending_block(general, priority_sources, snippet_chars)

    for key, sec_items in sections:
        sec_label = section_label(key)
        lines += [f'<a id="{html.escape(sec_label)}"></a>', f"## {sec_label}", ""]
        band = defaultdict(list)
        for item in sec_items:
            band[tiers[item.id]].append(item)

        if band[PRIORITY]:
            lines += [f"### ★ Priority — {len(band[PRIORITY])}", ""]
            lines += _render_items(band[PRIORITY], priority_sources)
        if band[MEDIA]:
            lines += [f"### 🖼 Images — {len(band[MEDIA])}", ""]
            for _source, batch in _grouped_chronological(band[MEDIA], priority_sources):
                for item in batch:
                    lines.append(_post_line(item, media_chars))
                    lines += [f"  ![]({image})" for image in item.images]
            lines.append("")
        if band[REGULAR]:
            lines += [f"### Posts — {len(band[REGULAR])}", ""]
            for _source, batch in _grouped_chronological(band[REGULAR], priority_sources):
                lines += [_post_line(item, snippet_chars) for item in batch]
            lines.append("")
        if band[RETWEET]:
            lines += [f"### 🔁 Retweets — {len(band[RETWEET])}", ""]
            for _source, batch in _grouped_chronological(band[RETWEET], priority_sources):
                lines += [_retweet_line(item, retweet_chars) for item in batch]
            lines.append("")
        if band[RECAP]:
            recap_sources = len({i.source for i in band[RECAP]})
            note = (
                f"*Recap group: {_count(len(band[RECAP]), 'item')} from"
                f" {_count(recap_sources, 'source')}, collapsed.*"
            )
            lines += [note + (f" Full text: {full_link}" if full_link else ""), ""]
            if recap_stories[key]:
                lines += [f"**Repeated in this group — {len(recap_stories[key])}**", ""]
                lines += _story_lines(recap_stories[key], priority_sources, snippet_chars)
                lines.append("")
    return "\n".join(lines)
