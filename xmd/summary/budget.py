"""Fit a brief to a reading time (`--time MIN`) rather than a compression ratio.

Time is counted as the brief's own header counts it (brief.pace_minutes): lines at the reader's pace, plus a
few seconds a picture. So `--time 10` at 40 lines a minute is 400 lines to spend, a picture costing 4 seconds
(2.7 lines) on top of its own line.

Most of a brief's reading time is not the model's summaries: on a real day (2026-09-29) image tweets were
two thirds of it and priority tweets a fifth, both shown whole, so a compression of 0.10 instead of 0.30 saved
about a minute of thirty. The compression is the weak lever and the image tweets the strong one, so the time is
spent in the reader's own order, and the image tweets take up the difference:

1. what is always there: the header, trending, the sections' headings, a link for every image tweet;
2. priority tweets, in full unless they would take more than half the time, or the brief can't fit otherwise
   (then a line each);
3. the summaries, at the usual compression (0.30) unless they would take more than half the time, or more than
   1 and 2 leave: then the highest compression at which they do, never under 0.10;
4. image tweets, with what is left: half of it shows the first ones whole, with their pictures, the rest gives
   the next ones a line, and the others stay links. They are taken in turn across groups, by group level, and
   across sources, so one prolific account can't take them all.

It is met in two steps, each where what it needs is known:

- before the model runs (run_system.py), the compression (3): it sizes the model's replies, so it can't wait.
  The summaries are not written yet, so they are counted at the most the model may write.
- when the brief is assembled, the cuts (2 and 4), now that the summaries are there to be measured. What they
  left unused of their share goes to the image tweets.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from dataclasses import dataclass, replace
from typing import Callable

from .brief import LINES_PER_MINUTE, Brief, Cuts, pace_minutes
from .chunk import DEFAULT_COMPRESSION, LEVEL_FACTORS, Chunk
from ..core.models import FeedItem
from ..digest.render import section_key

MAX_COMPRESSION = DEFAULT_COMPRESSION  # the compression when the summaries have the room: as without --time
MIN_COMPRESSION = 0.10  # below this the summaries are too thin to be worth reading
PRIORITY_SHARE = 0.5  # priority tweets in full may take up to this share of the time; past it, one line each
SUMMARY_SHARE = 0.5  # the summaries may take up to this share of the time; past it, the compression is lowered
PICTURE_SHARE = 0.5  # of the time left for image tweets, what goes to showing pictures before any gets a line
LINES_PER_SECTION = 4  # what a summarized section adds beside its bullets: anchor, heading, band heading, lead
FIT_ROUNDS = 6  # re-measures allowed when the estimate misses (a translation's quoted original, a links line)


def parse_minutes(text: str) -> float:
    """A reading time in minutes from "10" or "10min". Anything else, or 0 and less, raises ValueError saying what
    to write."""
    try:
        value = float(str(text).strip().lower().removesuffix("min").strip())
    except ValueError:
        raise ValueError(f"{text!r} is not a number of minutes: write 10 for 10 minutes") from None
    if not value > 0:
        raise ValueError(f"a reading time must be more than 0 minutes, got {text!r}")
    return value


def summary_lines(chunks: list[Chunk]) -> int:
    """Lines the model's replies can add to the brief: a bullet per item, at most each chunk's item limit, and
    the headings of the sections they fill."""
    sections = {p.section for c in chunks for p in c.posts}
    return sum(c.max_items for c in chunks) + LINES_PER_SECTION * len(sections)


@dataclass(frozen=True)
class Sizing:
    """What a reading time decides before the model runs (see choose_compression). Minutes are at the most: the
    summaries are counted as long as the model may make them."""

    compression: float
    whole_minutes: float  # the brief with nothing cut, its summaries at MAX_COMPRESSION
    summary_minutes: float  # its summaries at `compression`
    room: float  # the minutes the summaries may take; the whole time when nothing has to be cut

    @property
    def fits(self) -> bool:
        """The summaries take no more than their room: False only at MIN_COMPRESSION, when even that is too long."""
        return self.summary_minutes <= self.room


def choose_compression(
    minutes: float, base_minutes: float, bare_minutes: float, lines_at: Callable[[float], int],
) -> Sizing:
    """The compression for a brief of `minutes`. `base_minutes` is the brief without its summaries and nothing
    cut, `bare_minutes` the same at its barest (see bare_cuts), `lines_at(compression)` the lines the summaries
    add at that compression.

    MAX_COMPRESSION when the whole brief fits, or when its summaries take no more than SUMMARY_SHARE of the time
    and no more than the barest brief leaves: the image tweets are cut to fit, not the summaries. Otherwise the
    highest compression at which they do, never under MIN_COMPRESSION."""
    def at(compression: float) -> float:
        return lines_at(compression) / LINES_PER_MINUTE

    whole = base_minutes + at(MAX_COMPRESSION)
    if whole <= minutes:
        return Sizing(MAX_COMPRESSION, whole, at(MAX_COMPRESSION), minutes)
    room = min(SUMMARY_SHARE * minutes, minutes - bare_minutes)
    steps = [k / 100 for k in range(round(MAX_COMPRESSION * 100), round(MIN_COMPRESSION * 100), -1)]
    compression = next((c for c in steps if at(c) <= room), MIN_COMPRESSION)
    return Sizing(compression, whole, at(compression), room)


def _newest_first(item: FeedItem) -> float:
    return -(item.published or datetime.min.replace(tzinfo=timezone.utc)).timestamp()


def rank_media(media: list[FeedItem], levels: dict[str, str]) -> list[FeedItem]:
    """Image tweets in the order they get room: in turn across sections, each taking a share in proportion to
    its image tweets and its level (high x1.5, low x0.5), and within a section in turn across sources, each
    source's newest first."""
    queues: dict[str, list[FeedItem]] = {}
    by_section: dict[str, dict[str, list[FeedItem]]] = defaultdict(lambda: defaultdict(list))
    for item in sorted(media, key=_newest_first):
        by_section[section_key(item)][item.source].append(item)
    for key, sources in by_section.items():
        turns = [list(q) for q in sources.values()]
        queues[key] = [q[k] for k in range(max(map(len, turns))) for q in turns if k < len(q)]
    weight = {key: LEVEL_FACTORS.get(levels.get(q[0].group, "normal"), 1.0) * len(q) for key, q in queues.items()}
    taken = dict.fromkeys(queues, 0)
    ranked = []
    while any(taken[k] < len(q) for k, q in queues.items()):
        key = min((k for k, q in queues.items() if taken[k] < len(q)), key=lambda k: ((taken[k] + 1) / weight[k], k))
        ranked.append(queues[key][taken[key]])
        taken[key] += 1
    return ranked


def _minutes(brief: Brief) -> float:
    return brief.stats["minutes_at_your_pace"]


def bare_cuts(minutes: float, full: Brief) -> Cuts:
    """The cuts that leave the image tweets nothing but their links, for a brief that is `full` when uncut:
    priority tweets stay whole unless that takes more than PRIORITY_SHARE of the time."""
    one_liners = full.stats["band_minutes"].get("priority", 0) > PRIORITY_SHARE * minutes
    return Cuts(minutes, one_liners, frozenset(), frozenset())


def plan_cuts(
    minutes: float, build: Callable[[Cuts | None], Brief], media: list[FeedItem], levels: dict[str, str],
) -> tuple[Cuts | None, Brief]:
    """(what to leave out so the brief `build` makes fits `minutes`, and that brief). `build(cuts)` renders the
    brief with those cuts; `media` are its image tweets. None when the whole brief fits. When even the barest
    brief (priority as one-liners, image tweets as links) does not fit, that is what comes back: it can be no
    shorter."""
    full = build(None)
    if _minutes(full) <= minutes:
        return None, full
    bare = bare_cuts(minutes, full)
    brief = build(bare)
    if _minutes(brief) > minutes and not bare.priority_one_liners and full.stats["band_minutes"].get("priority"):
        bare = replace(bare, priority_one_liners=True)  # under their share, but in full the brief can't fit
        brief = build(bare)
    one_liners = bare.priority_one_liners
    slack = minutes - _minutes(brief)
    if slack <= 0:
        return bare, brief

    # the image tweets share the slack: the first ones in turn get their pictures (a line and a few seconds each,
    # on top of their caption) out of PICTURE_SHARE of it, the next ones a line out of the rest, the others a link.
    # A line left over when every image tweet has one goes back to pictures.
    ranked = rank_media(media, levels)
    pictures, lined = [], []
    picture_room = slack * PICTURE_SHARE
    for item in ranked:
        cost = pace_minutes(len(item.images) + 1, len(item.images))
        if cost <= picture_room:
            pictures.append(item)
            picture_room -= cost
            slack -= cost
    rest = [i for i in ranked if i not in pictures]
    lined = rest[:min(len(rest), math.floor(slack * LINES_PER_MINUTE))]
    slack -= len(lined) / LINES_PER_MINUTE
    for item in list(lined):
        cost = pace_minutes(len(item.images), len(item.images))  # its line is paid for already
        if cost <= slack:
            lined.remove(item)
            pictures.append(item)
            slack -= cost

    def cut() -> Cuts:
        return Cuts(minutes, one_liners, frozenset(i.id for i in pictures), frozenset(i.id for i in pictures + lined))

    brief = build(cut())
    for _ in range(FIT_ROUNDS):  # the estimate can miss by a few lines: pictures back to lines, then lines to links
        over = _minutes(brief) - minutes
        if over <= 0:
            break
        if pictures:
            drop = max(1, math.ceil(over / pace_minutes(1, 1)))
            pictures, lined = pictures[:-drop], pictures[-drop:] + lined
        elif lined:
            lined = lined[:-max(1, math.ceil(over * LINES_PER_MINUTE))]
        else:
            break
        brief = build(cut())
    return cut(), brief
