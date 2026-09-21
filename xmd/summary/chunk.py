"""Split the LLM export into chunks one call of a small local model can take.

The export (xmd/digest/export.py) is the frozen input every system is compared on. This
reads that text back, together with its .map.json (which knows each post's tier,
group and full word count), so chunking needs neither the database nor the config.

A chunk holds posts of one tier and one importance level only, because each is
briefed differently (prompts/brief.md): a `high` group gets more of its words kept
than a `low` one, and plain retweets and the recap group are briefed as a handful of
themes. Regular posts and retweets are packed across sections up to the size budget;
a recap group is kept apart so each group's picture stays its own.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from itertools import groupby

from ..digest.export import _BANDS
from ..core.tiers import RECAP, REGULAR, RETWEET

BAND = dict(_BANDS)  # tier -> the export's heading for it ("retweet" -> "retweets")
TIER_ORDER = (REGULAR, RETWEET, RECAP)

# How important a group is (sources/x.md: `## business | high`) scales how much of its tier's share of
# words is kept. Chunks are made most important first. A recap group is always the lowest, so it has no
# level: one written on it (`| recap | low`) is ignored, and its share is what THEME_SHARES says.
LEVEL_ORDER = ("high", "normal", "low")
LEVEL_FACTORS = {"high": 1.5, "normal": 1.0, "low": 0.5}

# Qwen3.5's tokenizer on the frozen export: 3.65 characters per token (first real run, 2026-09-20). The
# default budget keeps the chunks that 4,500 tokens at 3.0 characters made: 13,500 characters of posts.
CHARS_PER_TOKEN = 3.6
DEFAULT_MAX_INPUT_TOKENS = 3750  # of posts; with the ~700-token prompt about 4.2K in, inside the plan's 8K
MIN_TARGET_WORDS = 20  # a tiny chunk still gets a usable allowance

# Length is asked for in items as well as words, because a model counts items far better than words:
# a chunk's reply may have target / (words per item) items, at most the number of posts. Plain retweets
# and the recap group are briefed as themes, so they get a capped number: a handful of retweet themes, and
# more for a recap group, which the reader found far too short at 2% and 8 themes a chunk.
WORDS_PER_ITEM = {REGULAR: 30, RETWEET: 30, RECAP: 30}
MAX_THEME_ITEMS = {RETWEET: 8, RECAP: 14}
THEME_TIERS = tuple(MAX_THEME_ITEMS)
MIN_ITEMS_SHARE = 0.25  # fewer items than this share of the limit is not a summary of the chunk (1 item for 58 posts)

# What share of each tier's full-text words the brief may keep, before the level factor (docs/digest-summary.md, "Size and reading time"). For the
# regular posts it is the run's compression ratio, summary words over source words (--compression in
# scripts/run_system.py): 0.30 is the plan's scenario A, 0.10 its scenario B. Plain retweets and the recap group
# are the least important, so they keep fixed, smaller shares; recap went 3% -> 2% -> 6% after the reader called
# 2% "way too short". The plan's scenario C only changes how image tweets are shown, so it is no compression.
DEFAULT_COMPRESSION = 0.30
THEME_SHARES = {RETWEET: 0.04, RECAP: 0.06}


def tier_shares(compression: float) -> dict[str, float]:
    """Tier -> share of its full-text words the brief may keep, with `compression` for the regular posts."""
    return {REGULAR: compression, **THEME_SHARES}


def parse_compression(text: str) -> float:
    """A compression ratio from "0.30" or "30%": the share of the regular posts' words the brief keeps. Anything
    outside 0 to 1 raises ValueError saying what to write; a bare "30" is refused rather than guessed at, since
    it could mean 30% or 30 times."""
    problem = f"{text!r} is not a ratio from 0 to 1: write 0.30, or 30% for a percentage"
    raw = text.strip()
    try:
        value = round(float(raw[:-1]) / 100 if raw.endswith("%") else float(raw), 6)
    except ValueError:
        raise ValueError(problem) from None
    if not 0 < value <= 1:
        raise ValueError(problem)
    return value

_POST = re.compile(r"^\[(\d+)\] (.*)$")
_REF = re.compile(r"\[(\d+)\]")
_REPEATED_HEADING = "## repeated stories"


@dataclass(frozen=True)
class Post:
    n: int
    section: str  # "X", "X / finance"
    tier: str
    text: str  # what follows "[n] " in the export: "@source RT@x ..." as the model sees it
    words: int  # full-text words, before the export clipped the line
    group: str = ""  # the sources/x.md group ("" = none), lowercased

    @property
    def line(self) -> str:
        return f"[{self.n}] {self.text}"


@dataclass(frozen=True)
class Chunk:
    id: str  # "regular-03", "regular-high-01"
    tier: str
    posts: tuple[Post, ...]
    repeated: tuple[tuple[int, ...], ...]  # same-story groups, cut down to the posts in this chunk
    target_words: int  # allowance for the whole reply
    level: str = "normal"  # importance of the group(s) these posts are from

    @property
    def band(self) -> str:
        return BAND[self.tier]

    @property
    def max_items(self) -> int:
        """How many items the reply may have: the target at this tier's usual item length, never more than
        the posts, and for themes (retweets, recap) at most a handful."""
        items = round(self.target_words / WORDS_PER_ITEM[self.tier])
        if self.tier in THEME_TIERS:
            items = min(items, MAX_THEME_ITEMS[self.tier])
        return max(1, min(items, len(self.posts)))

    @property
    def min_items(self) -> int:
        """The fewest items a real summary of this chunk has; a reply below it is treated as a failure."""
        return max(1, math.ceil(self.max_items * MIN_ITEMS_SHARE))

    def render(self) -> str:
        """The user message: header, posts under their section headings, then the repeated-story hints."""
        lines = [f"Band: {self.band}"]
        if self.level != "normal":
            lines.append(f"Importance: {self.level}")
        lines.append(
            f"Length: at most {self.max_items} items and about {self.target_words} words in total, "
            "all headlines and details together; shorter is fine."
        )
        section = None
        for post in self.posts:
            if post.section != section:
                section = post.section
                lines += ["", f"## {section}"]
            lines.append(post.line)
        if self.repeated:
            lines += ["", _REPEATED_HEADING]
            lines += ["repeated: " + " ".join(f"[{n}]" for n in group) for group in self.repeated]
        return "\n".join(lines) + "\n"


def estimate_tokens(text: str, chars_per_token: float = CHARS_PER_TOKEN) -> int:
    return math.ceil(len(text) / chars_per_token)


def parse_export(text: str, mapping: dict[str, dict]) -> tuple[list[Post], list[tuple[int, ...]]]:
    """(posts in export order, same-story groups) from an export's text and its map."""
    posts: list[Post] = []
    repeated: list[tuple[int, ...]] = []
    section, in_repeated = "", False
    for line in text.splitlines():
        if line.startswith("### "):  # band heading: the map's tier is the source of truth
            continue
        if line.startswith("## "):
            in_repeated = line == _REPEATED_HEADING
            if not in_repeated:
                section = line[3:]
            continue
        if in_repeated:
            if line.startswith("repeated:"):
                repeated.append(tuple(int(n) for n in _REF.findall(line)))
            continue
        found = _POST.match(line)
        if not found:  # the "# ..." header lines and blanks
            continue
        n, body = int(found[1]), found[2]
        entry = mapping.get(str(n))
        if entry is None:
            raise ValueError(f"post [{n}] is in the export but not in its map")
        # a post that is only a URL really has 0 words; only an old map without the key falls back to the line
        words = entry["words"] if "words" in entry else len(body.split())
        posts.append(Post(n, section, entry["tier"], body, words, entry.get("group") or ""))
    return posts, repeated


def make_chunks(
    posts: list[Post],
    repeated: list[tuple[int, ...]],
    ratios: dict[str, float],
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
    chars_per_token: float = CHARS_PER_TOKEN,
    levels: dict[str, str] | None = None,
) -> list[Chunk]:
    """Chunks in reading order: regular, then retweets, then recap; within a tier, the most important
    level first. `ratios` is a tier -> share-of-full-words map (see tier_shares) and `levels` a
    group -> "high" | "low" map (see config.read_group_levels); together they set each chunk's target.

    A plain retweet of a story several accounts posted is not just a retweet: it is briefed as a
    regular post, so the story keeps its own line."""
    levels = levels or {}
    in_story = {n for group in repeated for n in group}
    posts = [replace(p, tier=REGULAR) if p.tier == RETWEET and p.n in in_story else p for p in posts]
    budget = int(max_input_tokens * chars_per_token)
    chunks: list[Chunk] = []
    for tier in TIER_ORDER:
        made = 0
        for level in (("normal",) if tier == RECAP else LEVEL_ORDER):
            mine = [p for p in posts if p.tier == tier and (tier == RECAP or levels.get(p.group, "normal") == level)]
            # a recap group's picture is its own; the other tiers are one stream across sections
            streams = [list(run) for _, run in groupby(mine, key=lambda p: p.section)] if tier == RECAP else [mine]
            for part in (part for stream in streams for part in _pack(stream, budget)):
                made += 1
                ids = {p.n for p in part}
                groups = (tuple(n for n in group if n in ids) for group in repeated)
                share = ratios.get(tier, 1.0) * LEVEL_FACTORS[level]
                chunks.append(Chunk(
                    id=f"{BAND[tier]}-{made:02d}" if level == "normal" else f"{BAND[tier]}-{level}-{made:02d}",
                    tier=tier,
                    posts=tuple(part),
                    repeated=tuple(g for g in groups if len(g) >= 2),
                    target_words=max(MIN_TARGET_WORDS, round(share * sum(p.words for p in part))),
                    level=level,
                ))
    return chunks


def _pack(posts: list[Post], budget: int) -> list[list[Post]]:
    """Consecutive posts, in as few parts as fit `budget` characters as render() lays them out:
    a post is its line and a newline, a section heading a blank line, "## ", the name and a newline."""
    parts: list[list[Post]] = []
    part: list[Post] = []
    used, section = 0, None
    for post in posts:
        cost = len(post.line) + 1 + (len(post.section) + 5 if post.section != section else 0)
        if part and used + cost > budget:
            parts.append(part)
            part, used, section = [], 0, None
            cost = len(post.line) + 1 + len(post.section) + 5
        part.append(post)
        used += cost
        section = post.section
    if part:
        parts.append(part)
    return parts
