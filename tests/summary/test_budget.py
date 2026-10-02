from datetime import datetime, timezone

import pytest

from xmd.core.models import FeedItem
from xmd.digest.export import build_export
from xmd.summary.brief import Cuts, build_brief
from xmd.summary.budget import (
    MAX_COMPRESSION, MIN_COMPRESSION, bare_cuts, choose_compression, parse_minutes, plan_cuts, rank_media,
    summary_lines,
)
from xmd.summary.chunk import make_chunks, parse_export, tier_shares

NOW = datetime(2026, 9, 19, 16, 0, tzinfo=timezone.utc)
PRIORITY = frozenset({"@vip"})


def _post(n, source, text, group="", images=None, **kw) -> FeedItem:
    return FeedItem(
        source=source, source_type="x", title=text[:60], url=f"https://x.com/{source.lstrip('@')}/status/{n}",
        published=datetime(2026, 9, 19, 6, n % 60, tzinfo=timezone.utc), full_text=text, group=group,
        images=images or [], **kw,
    )


# a day whose brief is mostly pictures and priority tweets, as a real one is
ITEMS = (
    [_post(n, "@vip", f"priority news number {n}\n\nwith a second paragraph", group="finance") for n in range(1, 11)]
    + [_post(n, f"@pic{n % 3}", f"chart {n}", group="biz", images=[f"https://img.example/{n}.jpg"] * 2)
       for n in range(11, 41)]
    + [_post(n, f"@pic{n % 2}", f"photo {n}", group="news", images=[f"https://img.example/{n}.jpg"])
       for n in range(41, 51)]
    + [_post(n, f"@r{n}", f"regular post {n} about the markets", group="biz") for n in range(51, 61)]
)
LEVELS = {"biz": "high", "news": "low"}
EXPORT_TEXT, MAPPING = build_export(ITEMS, NOW, priority_sources=PRIORITY)
POSTS, REPEATED = parse_export(EXPORT_TEXT, MAPPING)
MEDIA = [i for i in ITEMS if i.images]


def _build(cuts=None):
    records = [{"chunk": "c", "tier": "regular", "ids": [p.n for p in POSTS], "ok": True, "reply": {"items": []}}]
    return build_brief(ITEMS, NOW, records, MAPPING, {p.n: p.text for p in POSTS}, REPEATED,
                       priority_sources=PRIORITY, levels=LEVELS, cuts=cuts)


def _minutes(brief):
    return brief.stats["minutes_at_your_pace"]


def test_a_reading_time_is_minutes_with_or_without_the_unit():
    assert parse_minutes("10") == 10 and parse_minutes(" 7.5min ") == 7.5
    for bad in ("0", "-3", "ten", ""):
        with pytest.raises(ValueError):
            parse_minutes(bad)


LINES = lambda c: round(800 * c)  # 240 lines of summaries at 0.30: 6 minutes; 80 at 0.10: 2 minutes


def test_the_compression_is_the_usual_one_when_the_whole_brief_fits():
    sizing = choose_compression(30, 20, 3, LINES)  # 20 minutes of tweets and 6 of summaries
    assert (sizing.compression, sizing.whole_minutes, sizing.summary_minutes) == (MAX_COMPRESSION, 26, 6)
    assert sizing.fits


def test_a_brief_too_long_keeps_the_usual_compression_while_its_summaries_take_no_more_than_half_the_time():
    # 26 minutes for 20, or for 12: the image tweets are cut, not the summaries
    assert choose_compression(20, 20, 3, LINES).compression == MAX_COMPRESSION
    assert choose_compression(12, 20, 3, LINES).compression == MAX_COMPRESSION
    assert choose_compression(12, 20, 3, LINES).whole_minutes == 26


def test_summaries_that_would_take_more_than_half_the_time_get_the_highest_compression_that_does_not():
    sizing = choose_compression(10, 20, 3, LINES)  # 6 minutes of summaries for 10: they may take 5
    assert sizing.compression == 0.25 and sizing.summary_minutes == 5 and sizing.room == 5 and sizing.fits
    assert choose_compression(8, 20, 3, LINES).compression == 0.20


def test_summaries_take_no_more_than_what_the_barest_brief_leaves():
    sizing = choose_compression(10, 20, 7, LINES)  # priority tweets and trending are 7 of the 10 minutes
    assert sizing.compression == 0.15 and sizing.room == 3 and sizing.fits


def test_the_compression_is_never_under_the_floor_and_the_sizing_says_when_that_is_too_long():
    sizing = choose_compression(3, 20, 2, LINES)  # 1 minute left, and the thinnest summaries take 2
    assert sizing.compression == MIN_COMPRESSION and sizing.summary_minutes == 2 and not sizing.fits
    assert choose_compression(4, 20, 2, LINES).fits  # 2 minutes for 2: the floor, and it fits


def test_summary_lines_count_a_bullet_per_allowed_item_and_the_headings_of_their_sections():
    chunks = make_chunks(POSTS, REPEATED, tier_shares(0.3), 3750, levels=LEVELS)
    assert summary_lines(chunks) == sum(c.max_items for c in chunks) + 4 * len({p.section for c in chunks for p in c.posts})
    assert summary_lines(make_chunks(POSTS, REPEATED, tier_shares(0.1), 3750, levels=LEVELS)) <= summary_lines(chunks)


def test_image_tweets_take_turns_across_sections_by_level_and_across_sources():
    ranked = rank_media(MEDIA, LEVELS)
    first = ranked[:12]
    biz = [i for i in first if i.group == "biz"]
    assert len(biz) >= 10  # high and three times as many image tweets as the low group
    assert {i.source for i in biz[:3]} == {"@pic0", "@pic1", "@pic2"}  # every source before any gets a second
    assert sorted(i.id for i in ranked) == sorted(i.id for i in MEDIA)


def test_a_brief_that_fits_is_not_cut():
    cuts, brief = plan_cuts(_minutes(_build()) + 1, _build, MEDIA, LEVELS)
    assert cuts is None and brief.markdown == _build().markdown


def test_a_tight_time_cuts_pictures_to_lines_and_links_and_the_brief_fits():
    full = _minutes(_build())
    cuts, brief = plan_cuts(full / 2, _build, MEDIA, LEVELS)
    assert _minutes(brief) <= full / 2
    kept = [i for i in MEDIA if cuts.keeps_pictures(i)]
    lined = [i for i in MEDIA if not cuts.keeps_pictures(i) and cuts.keeps_caption(i)]
    assert kept and lined and len(kept) < len(MEDIA)
    md = brief.markdown
    assert md.count("![](") == sum(len(i.images) for i in kept)
    assert "- 🖼 **@pic" in md  # a line instead of the pictures
    assert f"fitted to {full / 2:g} min: {len(kept)} of 40 image tweets with pictures" in md


def test_priority_tweets_become_one_liners_only_when_in_full_they_take_more_than_half_the_time():
    priority = _build().stats["band_minutes"]["priority"]
    cuts, brief = plan_cuts(priority * 2 + 0.5, _build, MEDIA, LEVELS)
    assert not cuts.priority_one_liners and "second paragraph" in brief.markdown
    cuts, brief = plan_cuts(priority * 2 - 0.5, _build, MEDIA, LEVELS)
    assert cuts.priority_one_liners and "Cut to one line each" in brief.markdown
    assert "priority news number 3 with a second paragraph" in brief.markdown  # one line, both paragraphs


def test_priority_tweets_under_half_the_time_still_become_one_liners_when_in_full_the_brief_cannot_fit():
    full = _build()
    priority = full.stats["band_minutes"]["priority"]
    minutes = priority * 2 + 0.1  # under their share: in full by the rule
    assert not bare_cuts(minutes, full).priority_one_liners

    def crowded(cuts=None):  # a brief whose other parts leave no room for the priority tweets in full
        brief = _build(cuts)
        brief.stats["minutes_at_your_pace"] += priority + 0.2
        return brief

    cuts, brief = plan_cuts(minutes, crowded, MEDIA, LEVELS)
    assert cuts.priority_one_liners and "Cut to one line each" in brief.markdown
    assert _minutes(brief) <= minutes


def test_a_time_shorter_than_the_barest_brief_gives_the_barest_brief():
    cuts, brief = plan_cuts(0.5, _build, MEDIA, LEVELS)
    assert cuts == Cuts(0.5, True, frozenset(), frozenset())
    assert "![](" not in brief.markdown and "🖼 30 more:" in brief.markdown
