"""Cut a brief to a share of the whole day (`--keep 30`: keep 30% of it) rather than set a compression ratio.

A brief is weighed as brief.brief_length weighs it: its non-blank lines, a picture weighing PICTURE_LINES more on
top of its own line. "The whole day" is the brief with nothing cut and its summaries at the usual compression, so
`--keep 30` is 30% of that weight, whatever the day's size: about what `--time 10` was on a typical whole day
(six of them, 2026-10-01 to 10-15, weighed 1,094 to 1,616, median about 1,330, so 10 old minutes = 400 is 30%).

Most of a brief is not the model's summaries: on a real day (2026-09-29) image tweets were two thirds of it and
priority tweets a fifth, both shown whole, so a compression of 0.10 instead of 0.30 saved about a thirtieth. The
compression is the weak lever and the image tweets the strong one, so the share is spent in the reader's own
order, and the image tweets take up the difference:

1. what is always there: the header, trending, the sections' headings, a link for every image tweet;
2. priority tweets, in full unless they would take more than half of it, or the brief can't fit otherwise
   (then a line each);
3. the summaries, at the usual compression (chunk.DEFAULT_COMPRESSION) unless they would take more than half of it,
   or more than 1 and 2 leave: then the highest compression at which they do, never under 0.10;
4. image tweets, with what is left: half of it shows the first ones whole, with their pictures, the rest gives
   the next ones a line, and the others stay links. They are taken in turn across groups, by group level, and
   across sources, so one prolific account can't take them all.

It is met in two steps, each where what it needs is known:

- before the model runs (run_system.py), the compression (3): it sizes the model's replies, so it can't wait.
  The summaries are not written yet. The whole day is estimated with them at SUMMARY_FILL of the most the model
  may write (what replies really come to), and the target is the share of that; to stay under it, they are then
  counted at the most. The estimate is saved with the run.
- when the brief is assembled, the cuts (2 and 4), now that the summaries are there to be measured. The whole day
  is measured as written, unless the run lowered the compression (its summaries are then shorter than the whole
  day's would be): the run's estimate is used. What the summaries left unused of their share goes to the image
  tweets.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from dataclasses import dataclass, replace
from typing import Callable

from .brief import Brief, Cuts, brief_length
from .chunk import DEFAULT_COMPRESSION, LEVEL_FACTORS, Chunk
from ..core.models import FeedItem
from ..core.tiers import in_recap_group
from ..digest.render import section_key

MAX_COMPRESSION = DEFAULT_COMPRESSION  # the compression when the summaries have the room: as without --keep
MIN_COMPRESSION = 0.10  # below this the summaries are too thin to be worth reading
PRIORITY_SHARE = 0.5  # priority tweets in full may take up to this share of the target; past it, one line each
SUMMARY_SHARE = 0.5  # the summaries may take up to this share of the target; past it, the compression is lowered
PICTURE_SHARE = 0.5  # of what is left for image tweets, what goes to showing pictures before any gets a line
LINES_PER_SECTION = 4  # what a summarized section adds beside its bullets: anchor, heading, band heading, lead
SUMMARY_FILL = 0.83  # what the model's replies come to of their item limits: 77-89% on three real runs (2026-10)
FIT_ROUNDS = 6  # re-measures allowed when the estimate misses (a translation's quoted original, a links line)


def parse_keep(text: str) -> float:
    """The share of the day to keep, in percent, from "30" or "30%". Anything else, 0 and less, or more than 100
    raises ValueError saying what to write."""
    try:
        value = float(str(text).strip().removesuffix("%").strip())
    except ValueError:
        raise ValueError(f"{text!r} is not a percentage: write 30 to keep 30% of the day") from None
    if not 0 < value <= 100:
        raise ValueError(f"the share to keep is a percentage from 1 to 100, got {text!r}")
    return value


def summary_lines(chunks: list[Chunk], fill: float = 1.0) -> float:
    """Lines the model's replies can add to the brief: a bullet per item, `fill` of each chunk's item limit (all of
    it: the most they may add), and the headings of the sections they fill."""
    sections = {p.section for c in chunks for p in c.posts}
    return fill * sum(c.max_items for c in chunks) + LINES_PER_SECTION * len(sections)


@dataclass(frozen=True)
class Sizing:
    """What a share of the day decides before the model runs (see choose_compression), as brief_length weighs it.
    The summaries are counted as long as the model may make them."""

    compression: float
    whole: float  # the brief with nothing cut, its summaries at MAX_COMPRESSION, at the most
    summary: float  # its summaries at `compression`
    room: float  # what the summaries may take; the whole target when nothing has to be cut

    @property
    def fits(self) -> bool:
        """The summaries take no more than their room: False only at MIN_COMPRESSION, when even that is too long."""
        return self.summary <= self.room


def choose_compression(
    target: float, base: float, bare: float, lines_at: Callable[[float], float],
) -> Sizing:
    """The compression for a brief of `target` (as brief_length weighs it). `base` is the brief without its
    summaries and nothing cut, `bare` the same at its barest (see bare_cuts), `lines_at(compression)` the lines the
    summaries add at that compression.

    MAX_COMPRESSION when the whole brief fits, or when its summaries take no more than SUMMARY_SHARE of the target
    and no more than the barest brief leaves: the image tweets are cut to fit, not the summaries. Otherwise the
    highest compression at which they do, never under MIN_COMPRESSION."""
    whole = base + lines_at(MAX_COMPRESSION)
    if whole <= target:
        return Sizing(MAX_COMPRESSION, whole, lines_at(MAX_COMPRESSION), target)
    room = min(SUMMARY_SHARE * target, target - bare)
    steps = [k / 100 for k in range(round(MAX_COMPRESSION * 100), round(MIN_COMPRESSION * 100), -1)]
    compression = next((c for c in steps if lines_at(c) <= room), MIN_COMPRESSION)
    return Sizing(compression, whole, lines_at(compression), room)


def _newest_first(item: FeedItem) -> float:
    return -(item.published or datetime.min.replace(tzinfo=timezone.utc)).timestamp()


def rank_media(
    media: list[FeedItem], levels: dict[str, str], recap_groups: frozenset[str] = frozenset(),
) -> list[FeedItem]:
    """Image tweets in the order they get room: in turn across sections, each taking a share in proportion to
    its image tweets and its level (high x1.5, low x0.5; a recap group is always low), and within a section in
    turn across sources, each source's newest first."""
    queues: dict[str, list[FeedItem]] = {}
    by_section: dict[str, dict[str, list[FeedItem]]] = defaultdict(lambda: defaultdict(list))
    for item in sorted(media, key=_newest_first):
        by_section[section_key(item)][item.source].append(item)
    for key, sources in by_section.items():
        turns = [list(q) for q in sources.values()]
        queues[key] = [q[k] for k in range(max(map(len, turns))) for q in turns if k < len(q)]
    def level(item: FeedItem) -> str:
        return "low" if in_recap_group(item, recap_groups) else levels.get(item.group, "normal")

    weight = {key: LEVEL_FACTORS.get(level(q[0]), 1.0) * len(q) for key, q in queues.items()}
    taken = dict.fromkeys(queues, 0)
    ranked = []
    while any(taken[k] < len(q) for k, q in queues.items()):
        key = min((k for k, q in queues.items() if taken[k] < len(q)), key=lambda k: ((taken[k] + 1) / weight[k], k))
        ranked.append(queues[key][taken[key]])
        taken[key] += 1
    return ranked


def _length(brief: Brief) -> float:
    return brief.stats["length"]


def bare_cuts(target: float, full: Brief, keep: float) -> Cuts:
    """The cuts that leave the image tweets nothing but their links, for a brief of `target` that is `full` when
    uncut: priority tweets stay whole unless that takes more than PRIORITY_SHARE of the target. `keep` is the share
    asked for, for the header."""
    one_liners = full.stats["band_lengths"].get("priority", 0) > PRIORITY_SHARE * target
    return Cuts(keep, one_liners, frozenset(), frozenset())


def plan_cuts(
    keep: float, build: Callable[[Cuts | None], Brief], media: list[FeedItem], levels: dict[str, str],
    recap_groups: frozenset[str] = frozenset(), whole: float | None = None,
) -> tuple[Cuts | None, Brief]:
    """(what to leave out so the brief `build` makes keeps `keep` percent of the whole day, and that brief).
    `build(cuts)` renders the brief with those cuts; `media` are its image tweets, ranked by `levels` and
    `recap_groups` (see rank_media). The whole day is `whole` when given (the run's estimate, see the module's
    docstring), else the uncut brief as `build` makes it. None when the uncut brief fits. When even the barest
    brief (priority as one-liners, image tweets as links) does not fit, that is what comes back: it can be no
    shorter. Each brief returned says in its stats what the whole day weighed (`whole_length`)."""
    full = build(None)
    whole = whole if whole is not None else _length(full)
    target = keep / 100 * whole

    def told(cuts: Cuts | None, brief: Brief) -> tuple[Cuts | None, Brief]:
        brief.stats["whole_length"] = round(whole, 1)
        return cuts, brief

    if _length(full) <= target:
        return told(None, full)
    bare = bare_cuts(target, full, keep)
    brief = build(bare)
    if _length(brief) > target and not bare.priority_one_liners and full.stats["band_lengths"].get("priority"):
        bare = replace(bare, priority_one_liners=True)  # under their share, but in full the brief can't fit
        brief = build(bare)
    one_liners = bare.priority_one_liners
    slack = target - _length(brief)
    if slack <= 0:
        return told(bare, brief)

    # the image tweets share the slack: the first ones in turn get their pictures (a line and PICTURE_LINES each,
    # on top of their caption) out of PICTURE_SHARE of it, the next ones a line out of the rest, the others a link.
    # A line left over when every image tweet has one goes back to pictures.
    ranked = rank_media(media, levels, recap_groups)
    pictures, lined = [], []
    picture_room = slack * PICTURE_SHARE
    for item in ranked:
        cost = brief_length(len(item.images) + 1, len(item.images))
        if cost <= picture_room:
            pictures.append(item)
            picture_room -= cost
            slack -= cost
    rest = [i for i in ranked if i not in pictures]
    lined = rest[:min(len(rest), math.floor(slack))]
    slack -= len(lined)
    for item in list(lined):
        cost = brief_length(len(item.images), len(item.images))  # its line is paid for already
        if cost <= slack:
            lined.remove(item)
            pictures.append(item)
            slack -= cost

    def cut() -> Cuts:
        return Cuts(keep, one_liners, frozenset(i.id for i in pictures), frozenset(i.id for i in pictures + lined))

    brief = build(cut())
    for _ in range(FIT_ROUNDS):  # the estimate can miss by a few lines: pictures back to lines, then lines to links
        over = _length(brief) - target
        if over <= 0:
            break
        if pictures:
            drop = max(1, math.ceil(over / brief_length(1, 1)))
            pictures, lined = pictures[:-drop], pictures[-drop:] + lined
        elif lined:
            lined = lined[:-max(1, math.ceil(over))]
        else:
            break
        brief = build(cut())
    return told(cut(), brief)
