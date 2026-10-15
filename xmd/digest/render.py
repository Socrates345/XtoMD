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
`priority:high` sources sort first within both the normal and retweet bands —
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
image tweets) shown in full. A post that several priority tweets retweet or
quote opens the priority band, shown once with their comments listed under it
(see `shared_retweets`). A recap group (see tiers.py) collapses to a count
and its repeated stories, leaving the per-tweet detail to the full file. It
uses plain headings, lists and links only — no `<details>` — for the same
Obsidian reasons as above.
"""

from __future__ import annotations

import html
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import groupby
from typing import Callable
from urllib.parse import quote

from ..core.config import LANGUAGE_NAMES
from ..core.models import URL, FeedItem, FetchReport, is_echo
from ..core.tiers import MEDIA, PRIORITY, RECAP, REGULAR, RETWEET, tier_of
from .trending import DEFAULT_SIMILARITY, Story, find_stories

PLATFORM_LABELS = {"x": "X"}
DEFAULT_SNIPPET_CHARS = 140
DEFAULT_RETWEET_CHARS = 100
DEFAULT_MEDIA_CHARS = 500
MIN_RETWEETS = 2  # priority tweets retweeting or quoting one post this often are gathered under it, at the top
FETCH_REPORT = "Fetch report"  # the heading of the section a troubled fetch adds at the end of a page
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
    return _plain(text)


def _trim_url(url: str) -> str:
    """A matched URL without the prose around it: trailing sentence punctuation, and a ")" with no "(" to match,
    are not part of the link (the same rule as GFM autolinks)."""
    while url and (url[-1] in ".,;:!?" or (url[-1] == ")" and url.count(")") > url.count("("))):
        url = url[:-1]
    return url


def _plain(text: str) -> str:
    """Words with nothing in them that markdown or HTML could read as markup."""
    return _md(text.replace("<", "&lt;").replace(">", "&gt;"))


def _safe_text(text: str) -> str:
    """One tweet's words for a page written in markdown: `_inline`'s escaping (entities decoded, whitespace
    collapsed, `<` `>` `[` `]` `` ` `` `*` `_` neutralized), except that bare URLs stay, so a t.co link is still
    clickable. `[` and `]` are legal in a URL, so inside one they are percent-encoded: otherwise a "link" like
    `https://a.example/![](https://x.example/p.gif)` would carry an image past the escaping."""
    text = " ".join(html.unescape(text).split())
    out: list[str] = []
    pos = 0
    for m in _URL.finditer(text):
        url = _trim_url(m.group())
        out.append(_plain(text[pos : m.start()]))
        out.append(url.replace("[", "%5B").replace("]", "%5D").replace("`", "%60"))
        pos = m.start() + len(url)
    out.append(_plain(text[pos:]))
    return "".join(out)


_BLOCK_OPENER = re.compile(r"^(?:([#+~$%-])|(\d+)([.)]))")  # starts a heading, list, fence, math block or comment


def _defuse_opener(match: re.Match) -> str:
    marker, digits, punctuation = match.groups()
    return f"\\{marker}" if marker else f"{digits}\\{punctuation}"


def words_md(text: str) -> str:
    """Some of a tweet's words as a paragraph of their own, made as safe as `body_md` makes a whole tweet."""
    return _BLOCK_OPENER.sub(_defuse_opener, _safe_text(text))


def body_md(item: FeedItem) -> str:
    """`item.display_body()` made safe to write into a markdown page, for the places that show a tweet whole (the
    full digest, and the priority tweets of the quick digest and the brief). What X hands over is anyone's text: a
    post may read `![](https://tracker.example/p.gif)`, `<img ...>` or `![[a private note]]`, and a viewer such as
    Obsidian would load, render or embed it. So the words are escaped like the one-liners' (see `_safe_text`), and
    whatever would open a block at the start of the line (`#`, `1.`, `-`, a fence...) is escaped too, so a
    tweet can't add headings or swallow the lines below it. `display_body()` itself stays as it is: the word
    counts are made from it."""
    body = words_md(item.own_comment)
    if not item.retweet_of_author:
        return body
    quoted = f"🔁 Retweeted @{_md(item.retweet_of_author)}: {_safe_text(item.retweet_of_text)}"
    return f"{body}\n\n{quoted}" if body else quoted


def _when(item: FeedItem) -> str:
    if not item.published:
        return ""
    return item.published.astimezone(timezone.utc).strftime("%m-%d %H:%M")


# the brief's translation of a text shown word for word (xmd.summary.translate.Translation: .lang, .text,
# .unsupported), or None to show it as posted
Translated = Callable[[str], "object | None"]


def _not_in_original(translations: list) -> str:
    """The ⚠ a translation gets for a number, handle or ticker its original doesn't have, or ""."""
    missing = list(dict.fromkeys(w for t in translations for w in t.unsupported))
    return f" ⚠ not in the original: {_md(', '.join(missing))}" if missing else ""


def _original_line(translations: list, text: str, limit: int, indent: str = "") -> str:
    """The original under a translated line, quoted, so a doubtful translation can be checked against it."""
    names = ", ".join(dict.fromkeys(LANGUAGE_NAMES.get(t.lang, t.lang) for t in translations))
    return f"{indent}> 🌐 *{names}:* {_inline(text, limit)}"


def _story_lines(
    stories: list[Story], priority_sources: frozenset[str], limit: int, translated: Translated | None = None,
) -> list[str]:
    """One line per story: how many sources, a link to each source's own
    post, and what it was about. Stories with a priority source (★) first,
    then the most widely repeated — find_stories already orders by that.
    With `translated` (the brief), a story in a language the reader doesn't
    read shows its translation, with the original quoted under it."""
    lines = []
    for story in sorted(stories, key=lambda s: not any(x in priority_sources for x in s.sources)):
        star = "★ " if any(x in priority_sources for x in story.sources) else ""
        links = " · ".join(f"[{_md(i.source)}]({i.url})" for i in story.first_by_source)
        found = translated(story.text) if translated else None
        if found:
            lines.append(f"- **×{len(story.sources)}** {star}{links} — {_inline(found.text, limit)}"
                         + _not_in_original([found]))
            lines.append(_original_line([found], story.text, limit, indent="  "))
        else:
            lines.append(f"- **×{len(story.sources)}** {star}{links} — {_inline(story.text, limit)}")
    return lines


def _trending_block(
    stories: list[Story], priority_sources: frozenset[str], limit: int, translated: Translated | None = None,
) -> list[str]:
    if not stories:
        return []
    return [
        '<a id="Trending"></a>',
        "## Trending",
        "",
        f"🔥 {_stories(len(stories))} posted or amplified by 2+ of your sources"
        " (★ = a priority source is in it)",
        "",
        *_story_lines(stories, priority_sources, limit, translated),
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
    heading = f"#### {_md(item.source)} <!-- item:{item.id} --> [Post Link]({item.url})"
    if item.published:
        heading += f" · {item.published.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"
    lines = [heading, ""]
    body = body_md(item)
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
        url = _trim_url(m.group())
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


@dataclass
class Retweeted:
    """A post that several priority tweets retweet or quote (see `shared_retweets`), shown once with what each of
    them said about it."""

    author: str  # the retweeted poster's handle, as the retweets name it
    text: str  # the retweeted post: its longest copy, since X cuts some
    retweets: list[FeedItem]  # oldest first
    original: FeedItem | None = None  # the post itself, when it is a priority tweet too

    @property
    def comments(self) -> list[FeedItem]:
        return [i for i in self.retweets if not i.is_pure_retweet]

    @property
    def plain(self) -> list[FeedItem]:
        return [i for i in self.retweets if i.is_pure_retweet]

    @property
    def images(self) -> list[str]:
        """The post's pictures: its own, or else the ones its plain retweets carry. A quote's pictures are the
        quote's own and are shown under its comment."""
        found = self.original.images if self.original else [image for i in self.plain for image in i.images]
        return list(dict.fromkeys(found))

    def takes(self, author: str, text: str) -> bool:
        """Whether a post by `author` reading `text` is this one (a copy cut short by X still is)."""
        return (_handle(author) == _handle(self.author)
                and (is_echo(text, self.text) or is_echo(self.text, text)))


def _handle(name: str) -> str:
    return name.lstrip("@").lower()


def shared_retweets(items: list[FeedItem]) -> tuple[list[Retweeted], list[FeedItem]]:
    """(the posts that MIN_RETWEETS or more of `items` retweet or quote, most retweeted first, each with those
    items and with the post itself when it is one of `items`; the items in none of them, in their order)."""
    groups: list[Retweeted] = []
    for item in sorted(items, key=lambda i: i.published or datetime.min.replace(tzinfo=timezone.utc)):
        if not item.retweet_of_author:
            continue
        group = next((g for g in groups if g.takes(item.retweet_of_author, item.retweet_of_text)), None)
        if group is None:
            groups.append(Retweeted(item.retweet_of_author, item.retweet_of_text, [item]))
            continue
        group.retweets.append(item)
        if len(item.retweet_of_text) > len(group.text):
            group.text = item.retweet_of_text
    groups = sorted((g for g in groups if len(g.retweets) >= MIN_RETWEETS), key=lambda g: -len(g.retweets))
    taken = {i.id for g in groups for i in g.retweets}
    for group in groups:
        group.original = next(
            (i for i in items if i.id not in taken and group.takes(i.author, i.own_comment)), None)
        if group.original:
            taken.add(group.original.id)
    return groups, [i for i in items if i.id not in taken]


def retweeted_heading(group: Retweeted) -> str:
    """A retweeted post's heading: its poster, linked and dated when the post itself is there, and how often it
    was retweeted."""
    original = group.original
    if original is None:
        heading = f"#### 🔁 @{_md(group.author.lstrip('@'))}"
    else:
        heading = f"#### 🔁 {_md(original.source)} <!-- item:{original.id} --> [Post Link]({original.url})"
        if original.published:
            heading += f" · {original.published.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"
    return heading + f" — retweeted {_count(len(group.retweets), 'time')}"


def retweet_comments(
    group: Retweeted, translated: Translated | None = None, limit: int = 0, indent: str = "",
) -> list[str]:
    """The list under a retweeted post: each comment on it, oldest first, then the plain retweets as links.
    A comment is whole with its pictures, or with `limit` one line of at most that many characters and none.
    With `translated` (the brief), a comment in a language the reader doesn't read shows its translation, with
    the original quoted under it."""
    lines = []
    for item in group.comments:
        found = translated(item.own_comment) if translated else None
        said = found.text if found else item.own_comment
        said = _inline(said, limit) if limit else _safe_text(said)
        head = " · ".join(p for p in (f"**{_md(item.source)}** <!-- item:{item.id} -->", _when(item), said) if p)
        lines.append(f"{indent}- {head} [↗]({item.url})" + (_not_in_original([found]) if found else ""))
        if found:
            lines.append(_original_line([found], item.own_comment, limit, indent=indent + "  "))
        if not limit:
            lines += [f"{indent}  ![]({image})" for image in item.images]
    if group.plain:
        links = " · ".join(f"[{_md(i.source)}]({i.url}) <!-- item:{i.id} -->" for i in group.plain)
        lines.append(f"{indent}- 🔁 without a comment: {links}")
    return lines


def _retweeted_block(group: Retweeted) -> list[str]:
    """A post several priority tweets retweet, whole like `_item_block`, with what each of them said under it."""
    lines = [retweeted_heading(group), ""]
    body = body_md(group.original) if group.original else words_md(group.text)
    if body:
        lines += [body, ""]
    for image in group.images:
        lines.append(f"![]({image})")
    if group.images:
        lines.append("")
    return lines + retweet_comments(group) + [""]


def _render_priority(items: list[FeedItem], priority_sources: frozenset[str]) -> list[str]:
    """Priority tweets in full: first, once each, the posts several of them retweet, with their comments (see
    `shared_retweets`); then the others as `_render_items` shows them."""
    groups, rest = shared_retweets(items)
    lines: list[str] = []
    for group in groups:
        lines += (["---", ""] if lines else []) + _retweeted_block(group)
    others = _render_items(rest, priority_sources)
    return lines + (["---", ""] if lines and others else []) + others


def fetch_note(report: FetchReport | None) -> str:
    """The fetch report in a few words, linked to it, for the top of a page: the report itself is at the very end,
    where a reader may never get. "" when there is nothing to report."""
    if report is None or report.clean:
        return ""
    missed = len(report.not_found) + len(report.failed)
    said = ([f"{_count(missed, 'source')} not fetched"] if missed else []) + (
        [_count(len(report.warnings), "fetch warning")] if report.warnings else [])
    return f"⚠ [{', '.join(said)}](#{_fragment(FETCH_REPORT)})"


def fetch_report_lines(report: FetchReport | None, now: datetime) -> list[str]:
    """The section that ends a digest or a brief whose fetch had trouble, so that nobody has to read the log: the
    accounts the backend no longer knows first (only the user can put those right), then the sources that failed
    for another reason, then what the fetch warned about. [] when it had none, or `report` is None. The fetch's
    time is on `now`'s clock, as the rest of the page. Labels and error messages are escaped like a tweet's words:
    the `<none>` of `Server error '520 <none>'` must reach the reader, and no tag may reach the page."""
    if report is None or report.clean:
        return []
    missed = len(report.not_found) + len(report.failed)
    when = report.at.astimezone(now.tzinfo).strftime("%Y-%m-%d %H:%M")
    asked = _count(report.sources, "source")
    lines = ["---", "", f'<a id="{html.escape(FETCH_REPORT)}"></a>', f"## {FETCH_REPORT}", "",
             f"*Fetch of {when}: " + (f"{missed} of {asked} not fetched" if missed else f"{asked} fetched") + ".*", ""]
    if report.not_found:
        lines += ["**Account not found: renamed, suspended or deleted? Correct or remove its handle in `x.md`.**", ""]
        lines += [f"- {_plain(label)}: {_plain(why)}" for label, why in report.not_found] + [""]
    if report.failed:
        lines += ["**Failed this time: the next fetch tries again.**", ""]
        lines += [f"- {_plain(label)}: {_plain(why)}" for label, why in report.failed] + [""]
    if report.warnings:
        lines += ["**Warnings**", ""]
        lines += [f"- {_plain(warning)}" for warning in report.warnings] + [""]
    return lines


def build_markdown(
    items: list[FeedItem],
    now: datetime,
    label: str = "",
    priority_sources: frozenset[str] = frozenset(),
    similarity: float = DEFAULT_SIMILARITY,
    snippet_chars: int = DEFAULT_SNIPPET_CHARS,
    fetch_report: FetchReport | None = None,
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
    lines += [fetch_note(fetch_report), ""] if fetch_note(fetch_report) else []
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
    lines += fetch_report_lines(fetch_report, now)
    return "\n".join(lines)


def _line_text(item: FeedItem) -> str:
    """What a one-liner says: the post's own words, and the post it retweets or quotes."""
    text = item.own_comment
    if item.is_pure_retweet:
        text = f"🔁 @{item.retweet_of_author}: {item.retweet_of_text}"
    elif item.retweet_of_author:  # a quote with commentary: keep what it's responding to
        text = f"{text} ↩ @{item.retweet_of_author}: {item.retweet_of_text}"
    return text


def _post_line(item: FeedItem, limit: int) -> str:
    head = " · ".join(
        p for p in (f"**{_md(item.source)}**", _when(item), _inline(_line_text(item), limit)) if p
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
    fetch_report: FetchReport | None = None,
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
    lines += [fetch_note(fetch_report), ""] if fetch_note(fetch_report) else []
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
            lines += _render_priority(band[PRIORITY], priority_sources)
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
    lines += fetch_report_lines(fetch_report, now)
    return "\n".join(lines)
