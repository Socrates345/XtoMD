"""Assemble a run's replies into the readable brief.

The model wrote summaries of chunks (see runner.py); this turns them into one markdown
file a person reads, in the order of the reader's own ladder: priority tweeters first
and in full, then trending stories, then each group's section, the most important
group first and the recap group last. A section holds its image tweets whole, the
model's summary bullets (each linking to the tweets it draws on), and retweet themes.

What the model wrote is never taken on trust:
- cited post numbers that are not in the chunk are dropped, and an item left with no
  real source is dropped with them;
- an item whose numbers, handles or tickers are in none of its cited posts is kept but
  marked with a warning, naming them;
- a chunk that failed, or was never run, falls back to the quick digest's one-liners
  for its posts, and says so.

Priority and image tweets are rendered by render.py, exactly as in the quick digest:
the model never sees them. Like the quick digest this uses plain headings, lists and
links only, no `<details>` (Obsidian's Live Preview cannot render it).
"""

from __future__ import annotations

import html
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .chunk import LEVEL_ORDER
from .models import URL, FeedItem
from .render import (
    DEFAULT_MEDIA_CHARS, DEFAULT_RETWEET_CHARS, DEFAULT_SNIPPET_CHARS, _count, _fragment, _grouped_chronological,
    _inline, _md, _post_line, _retweet_line, _stories, _story_lines, _trending_block, section_key,
    section_key_for, section_label, sectioned,
)
from .tiers import MEDIA, PRIORITY, RECAP, REGULAR, RETWEET, tier_of
from .topics import Topic
from .trending import DEFAULT_SIMILARITY, find_stories
from .verify import support

# the plan's reading-speed assumptions (docs/digest-summary.md, "Size and reading time")
WPM_NORMAL = 230
WPM_DIAGONAL = 400
SECONDS_PER_IMAGE = 4
LINES_PER_MINUTE = 40  # the reader's own pace, measured 2026-09-20: 200 lines in 5 minutes, links clicked included
MAX_CITES = 4  # links shown per item; the rest are counted

_ITEM_ID = re.compile(r"<!-- item:([0-9a-f]+) -->")
_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2})-(\d{4})")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG = re.compile(r"<[^>\n]*>")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


@dataclass(frozen=True)
class BriefItem:
    """One summary bullet, after checking."""

    tier: str  # of the chunk it came from: regular, retweet or recap
    section: str  # the section label it is filed under
    headline: str
    detail: str
    ids: tuple[int, ...]  # cited posts that really are in its chunk, without repeats
    trending: bool  # two or more of them are the same repeated story
    unsupported: tuple[str, ...]  # numbers, handles and tickers in none of the cited posts


@dataclass
class Brief:
    markdown: str
    stats: dict = field(default_factory=dict)


def measure(text: str) -> tuple[int, int]:
    """(words a reader meets, images) in some of the brief's markdown: link text counts, URLs, tags,
    comments and markup do not. One measure for every system, as the plan asks."""
    images = len(_IMAGE.findall(text))
    text = _COMMENT.sub(" ", _IMAGE.sub(" ", text))
    text = URL.sub(" ", _LINK.sub(r"\1", _TAG.sub(" ", text)))
    return sum(1 for token in text.split() if any(c.isalnum() for c in token)), images


def reading_minutes(words: int, images: int, wpm: int) -> float:
    return words / wpm + images * SECONDS_PER_IMAGE / 60


def digest_item_ids(markdown: str) -> list[str]:
    """The ids of the items a full digest lists, in order and without repeats: every item's heading carries
    `<!-- item:ID -->`, in markdown and inside the retweet toggle alike."""
    return list(dict.fromkeys(_ITEM_ID.findall(markdown)))


def digest_time(stem: str) -> datetime | None:
    """When a digest was made, read from its file name (`2026-09-20-1156`, or the end of a frozen window's
    `frozen-2026-09-18-1600--2026-09-19-1600`); None if the name has no stamp."""
    found = _STAMP.findall(stem)
    if not found:
        return None
    day, hhmm = found[-1]
    return datetime.strptime(day + hhmm, "%Y-%m-%d%H%M").replace(tzinfo=timezone.utc)


def load_run(run_dir: Path) -> tuple[dict, list[dict]]:
    """(manifest, chunk records in run order) of a run saved by runner.write_run."""
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    records = [
        json.loads((run_dir / f"{row['chunk']}.json").read_text(encoding="utf-8")) for row in manifest["chunks"]
    ]
    return manifest, records


def _rank(group: str, levels: dict[str, str], recap_groups: frozenset[str]) -> tuple[int, str]:
    """Where a group sorts: high, normal, low, then the recap groups; ties alphabetically."""
    if group in recap_groups:
        return 3, group
    return LEVEL_ORDER.index(levels.get(group, "normal")), group


def _section_of(ids: list[int], mapping: dict, levels: dict, recap_groups: frozenset[str]) -> str:
    """The section an item is filed under: the group most of its posts are from (a tie goes to the more
    important group). X is the only platform so far."""
    groups = Counter(mapping[str(n)].get("group") or "" for n in ids)
    best = min(groups, key=lambda g: (-groups[g], _rank(g, levels, recap_groups)))
    return section_label(section_key_for("x", best))


def summary_items(
    records: list[dict], mapping: dict, post_text: dict[int, str], repeated: list[tuple[int, ...]],
    levels: dict[str, str], recap_groups: frozenset[str],
) -> tuple[list[BriefItem], int]:
    """(the checked items of every chunk that worked, how many were dropped for having no real source)."""
    kept: list[BriefItem] = []
    dropped = 0
    stories = [set(group) for group in repeated]
    for record in records:
        if not record.get("ok") or record.get("reply") is None:
            continue
        chunk_ids = set(record["ids"])
        for item in record["reply"]["items"]:
            ids = list(dict.fromkeys(n for n in item["ids"] if n in chunk_ids))
            headline, detail = item["headline"].strip(), item["detail"].strip()
            if not ids or not (headline or detail):
                dropped += 1
                continue
            flagged = support([{**item, "ids": ids}], {n: post_text[n] for n in ids if n in post_text}).flagged
            kept.append(BriefItem(
                tier=record["tier"],
                section=_section_of(ids, mapping, levels, recap_groups),
                headline=headline,
                detail=detail,
                ids=tuple(ids),
                trending=any(len(story & set(ids)) >= 2 for story in stories),
                unsupported=flagged[0][1] if flagged else (),
            ))
    return kept, dropped


def _priority_block(item: FeedItem) -> list[str]:
    """A priority tweet in full, like render._item_block but with its pictures first and a blank line after
    each, and the text below them as their caption."""
    heading = f"#### {item.source} <!-- item:{item.id} --> [Post Link]({item.url})"
    if item.published:
        heading += f" · {item.published.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"
    lines = [heading, ""]
    for image in item.images:
        lines += [f"![]({image})", ""]
    body = item.display_body()
    return lines + ([body, ""] if body else [])


def _render_priority(items: list[FeedItem], priority_sources: frozenset[str]) -> list[str]:
    lines: list[str] = []
    for _source, batch in _grouped_chronological(items, priority_sources):
        for item in batch:
            lines += (["---", ""] if lines else []) + _priority_block(item)
    return lines


def _cites(ids: tuple[int, ...], mapping: dict) -> str:
    links = [f"[{_md(mapping[str(n)]['source'])}]({mapping[str(n)]['url']})" for n in ids[:MAX_CITES]]
    return " · ".join(links) + (f" +{len(ids) - MAX_CITES}" if len(ids) > MAX_CITES else "")


def _item_line(item: BriefItem, mapping: dict) -> str:
    line = ("🔥 " if item.trending else "") + f"**{_inline(item.headline, 0)}**"
    if item.detail:
        line += f" — {_inline(item.detail, 0)}"
    line += f" ({_cites(item.ids, mapping)})"
    if item.unsupported:
        line += f" ⚠ not in the cited posts: {_md(', '.join(item.unsupported))}"
    return f"- {line}"


def _lead(label: str, topics: dict[str, list[Topic]], summaries: dict[str, str]) -> list[str]:
    """The top of a section: what it was about (a model's summary) and the names that were louder than usual
    (counted), as quotes so they stand apart from the bullets below."""
    paragraphs = []
    if summaries.get(label):
        paragraphs.append(f"> **In short:** {_inline(summaries[label], 0)}")
    if topics.get(label):
        loud = " · ".join(
            f"**{_md(t.term)}** ({t.posts} posts from {_count(t.sources, 'account')}, usually ~{t.usual:.0f})"
            for t in topics[label])
        paragraphs.append(f"> 🔥 **Louder than usual:** {loud}")
    lines: list[str] = []
    for k, paragraph in enumerate(paragraphs):
        lines += [">", paragraph] if k else [paragraph]
    return lines + [""] if lines else []


def build_brief(
    items: list[FeedItem],
    now: datetime,
    records: list[dict],
    mapping: dict[str, dict],
    post_text: dict[int, str],
    repeated: list[tuple[int, ...]],
    *,
    priority_sources: frozenset[str] = frozenset(),
    recap_groups: frozenset[str] = frozenset(),
    levels: dict[str, str] | None = None,
    similarity: float = DEFAULT_SIMILARITY,
    snippet_chars: int = DEFAULT_SNIPPET_CHARS,
    retweet_chars: int = DEFAULT_RETWEET_CHARS,
    media_chars: int = DEFAULT_MEDIA_CHARS,
    full_name: str = "",
    model_line: str = "",
    topics: dict[str, list[Topic]] | None = None,
    summaries: dict[str, str] | None = None,
) -> Brief:
    """The brief for the window's `items`, from a run's chunk `records` (see load_run). `mapping`, `post_text`
    and `repeated` come from the frozen export the run read; `levels` are the group levels the run used.
    `topics` and `summaries` (section label -> ...) open the sections they belong to, when given."""
    levels = levels or {}
    topics, summaries = topics or {}, summaries or {}
    by_url = {i.url: i for i in items}
    tiers = {i.id: tier_of(i, priority_sources, recap_groups) for i in items}
    kept, dropped = summary_items(records, mapping, post_text, repeated, levels, recap_groups)
    words = Counter()  # by band, for the report

    def band(name: str, lines: list[str]) -> list[str]:
        words[name] += measure("\n".join(lines))[0]
        return lines

    # posts without a usable summary: their chunk failed or was never run
    covered = {n for r in records if r.get("ok") and r.get("reply") is not None for n in r["ids"]}
    loose = [by_url[m["url"]] for n, m in mapping.items() if int(n) not in covered and m["url"] in by_url]
    absent = sum(1 for m in mapping.values() if m["url"] not in by_url)

    # stories: one made only of recap-group posts belongs to that group's section
    recap_stories: dict[str, list] = defaultdict(list)
    general = []
    for story in find_stories(items, similarity):
        if all(tiers[i.id] == RECAP for i in story.items):
            recap_stories[section_key(story.items[0])].append(story)
        else:
            general.append(story)

    filed: dict[tuple[str, str], list[BriefItem]] = defaultdict(list)
    for item in kept:
        filed[(item.section, item.tier)].append(item)

    ordered = sorted(sectioned(items), key=lambda kv: _rank(kv[0].partition("/")[2], levels, recap_groups) + (kv[0],))
    sections: list[tuple[str, int, list[str]]] = []  # (label, entries, lines)
    for key, sec_items in ordered:
        label_ = section_label(key)
        by_tier = defaultdict(list)
        for item in sec_items:
            by_tier[tiers[item.id]].append(item)
        body: list[str] = []
        shown = 0

        if by_tier[MEDIA]:
            block = [f"### 🖼 Images — {len(by_tier[MEDIA])}", ""]
            for _source, batch in _grouped_chronological(by_tier[MEDIA], priority_sources):
                for item in batch:
                    for image in item.images:  # pictures first, each followed by a blank line, then the caption
                        block += [f"![]({image})", ""]
                    block += [_post_line(item, media_chars)[2:], ""]  # [2:] drops the list dash: it is a caption
            body += band("images", block)
            shown += len(by_tier[MEDIA])

        posts = filed[(label_, REGULAR)]
        if posts:
            body += band("summary", [f"### Summary — {_count(len(posts), 'item')}", "",
                                     *(_item_line(i, mapping) for i in posts), ""])
            shown += len(posts)

        themes = filed[(label_, RETWEET)]
        if themes:
            body += band("retweet themes", [f"### 🔁 Retweets — {_count(len(themes), 'theme')}", "",
                                            *(_item_line(i, mapping) for i in themes), ""])
            shown += len(themes)

        left = [i for i in loose if section_key(i) == key and tiers[i.id] in (REGULAR, RETWEET)]
        if left:
            block = [f"### Not summarized — {_count(len(left), 'post')}", "",
                     "*The model gave no usable summary for these, so they are shown as one-liners.*", ""]
            for _source, batch in _grouped_chronological(left, priority_sources):
                block += [(_retweet_line(i, retweet_chars) if i.is_pure_retweet else _post_line(i, snippet_chars))
                          for i in batch]
            body += band("not summarized", block + [""])
            shown += len(left)

        if by_tier[RECAP]:
            recap_items = filed[(label_, RECAP)]
            picture = _count(len(recap_items), "theme") if recap_items else "no usable summary"
            note = (f"*Recap group: {_count(len(by_tier[RECAP]), 'item')} from "
                    f"{_count(len({i.source for i in by_tier[RECAP]}), 'source')}, only the picture: {picture}.*")
            block = ["### Recap", "", note + (f" [full digest]({full_name})" if full_name else ""), ""]
            if recap_items:
                block += [*(_item_line(i, mapping) for i in recap_items), ""]
            if recap_stories[key]:
                block += [f"**Repeated in this group — {len(recap_stories[key])}**", "",
                          *_story_lines(recap_stories[key], priority_sources, snippet_chars), ""]
            body += band("recap", block)
            shown += len(recap_items) or 1

        if body:
            body = band("section summary", _lead(label_, topics, summaries)) + body
            sections.append((label_, shown, [f'<a id="{html.escape(label_)}"></a>', f"## {label_}", "", *body]))

    priority = [i for i in items if tiers[i.id] == PRIORITY]
    priority_lines = band("priority", [
        '<a id="★ Priority"></a>', "## ★ Priority", "",
        *_render_priority(priority, priority_sources),
    ]) if priority else []
    trending_lines = band("trending", _trending_block(general, priority_sources, snippet_chars))

    # the top of the file: the date, then only how long it takes and where the full digest is
    cited = {n for i in kept for n in i.ids}
    regular_posts = sum(1 for r in records if r["tier"] == REGULAR for _ in r["ids"])
    regular_cited = sum(1 for r in records if r["tier"] == REGULAR for n in r["ids"] if n in cited)
    lines = [f"# X brief — {now.strftime('%Y-%m-%d')}", ""]
    estimate_at = len(lines)
    lines.append("")  # the reading time goes here once the rest is counted
    lines.append("")

    entries = []
    if priority:
        entries.append(f"- [★ Priority](#{_fragment('★ Priority')}) — {_count(len(priority), 'tweet')}")
    if general:
        entries.append(f"- [Trending](#Trending) — {_stories(len(general))}")
    entries += [f"- [{name}](#{_fragment(name)}) — {n} entr{'y' if n == 1 else 'ies'}" for name, n, _ in sections]
    if len(entries) > 1:
        lines += entries + [""]
    lines += priority_lines + trending_lines
    for _name, _n, section_lines in sections:
        lines += section_lines

    notes = []
    if dropped:
        notes.append(f"{_count(dropped, 'summary item')} dropped: no cited post was in its chunk")
    flagged = sum(1 for i in kept if i.unsupported)
    if flagged:
        notes.append(f"{_count(flagged, 'summary item')} marked ⚠: a number, handle or ticker not in the posts it cites")
    if loose:
        notes.append(f"{_count(len(loose), 'post')} without a summary (chunk failed or not run)")
    if notes:
        lines += ["---", "", "*" + "; ".join(notes) + ".*", ""]

    text = "\n".join(lines)
    total_words, total_images = measure(text)
    content_lines = sum(1 for line in lines if line.strip())
    normal = reading_minutes(total_words, total_images, WPM_NORMAL)
    diagonal = reading_minutes(total_words, total_images, WPM_DIAGONAL)
    yours = content_lines / LINES_PER_MINUTE + total_images * SECONDS_PER_IMAGE / 60
    lines[estimate_at] = f"**~{max(1, round(yours))} min read**" + (f" · [full digest]({full_name})" if full_name else "")
    if model_line:  # provenance for whoever debugs it, invisible when reading
        lines.append(f"<!-- {model_line} -->")
    return Brief("\n".join(lines) + "\n", {
        "words": total_words, "images": total_images, "lines": content_lines,
        "minutes_at_your_pace": round(yours, 1),
        "minutes_normal": round(normal, 1), "minutes_diagonal": round(diagonal, 1),
        "bands": dict(words),
        "summary_items": len(kept), "dropped_items": dropped, "flagged_items": flagged,
        "trending_items": sum(1 for i in kept if i.trending),
        "section_summaries": len(summaries), "louder_topics": sum(len(v) for v in topics.values()),
        "posts_without_summary": len(loose), "posts_absent_from_the_database": absent,
        "regular_posts": regular_posts, "regular_posts_cited": regular_cited,
    })


def _filed_of(filed: dict[tuple[str, str], list[BriefItem]], tier: str) -> list[BriefItem]:
    """Every item of one tier, whatever section it is filed under."""
    return [i for (_section, t), group in filed.items() if t == tier for i in group]
