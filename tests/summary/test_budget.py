from datetime import datetime, timezone

import pytest

from xmd.core.models import FeedItem
from xmd.digest.export import build_export
from xmd.summary.brief import Cuts, build_brief
from xmd.summary.budget import (
    MAX_COMPRESSION, MIN_COMPRESSION, SUMMARY_FILL, bare_cuts, choose_compression, parse_keep, plan_cuts,
    rank_media, summary_lines,
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
    return build_brief(ITEMS, NOW, records, MAPPING, REPEATED,
                       priority_sources=PRIORITY, levels=LEVELS, cuts=cuts)


def _length(brief):
    return brief.stats["length"]


def test_a_share_to_keep_is_a_percentage_with_or_without_the_sign():
    assert parse_keep("30") == 30 and parse_keep(" 7.5% ") == 7.5 and parse_keep("100") == 100
    for bad in ("0", "-3", "101", "ten", "", "10min"):
        with pytest.raises(ValueError):
            parse_keep(bad)


LINES = lambda c: round(800 * c)  # 240 lines of summaries at 0.30; 80 at 0.10


USUAL = LINES(MAX_COMPRESSION)  # lines of summaries at the usual compression, 200 at most


def test_the_compression_is_the_usual_one_when_the_whole_brief_fits():
    sizing = choose_compression(1200, 800, 120, LINES)  # 800 lines of tweets and the summaries
    assert (sizing.compression, sizing.whole, sizing.summary) == (MAX_COMPRESSION, 800 + USUAL, USUAL)
    assert sizing.fits


def test_a_brief_too_long_keeps_the_usual_compression_while_its_summaries_take_no_more_than_half_of_it():
    # 1,000 lines at most for 800, or for 480: the image tweets are cut, not the summaries
    assert choose_compression(800, 800, 120, LINES).compression == MAX_COMPRESSION
    assert choose_compression(480, 800, 120, LINES).compression == MAX_COMPRESSION
    assert choose_compression(480, 800, 120, LINES).whole == 800 + USUAL


def test_summaries_that_would_take_more_than_half_of_it_get_the_highest_compression_that_does_not():
    sizing = choose_compression(400, 800, 120, LINES)  # 200 lines of summaries for 400: they may take 200
    assert sizing.compression == 0.25 and sizing.summary == 200 and sizing.room == 200 and sizing.fits
    assert choose_compression(320, 800, 120, LINES).compression == 0.20


def test_summaries_take_no_more_than_what_the_barest_brief_leaves():
    sizing = choose_compression(400, 800, 280, LINES)  # priority tweets and trending are 280 of the 400
    assert sizing.compression == 0.15 and sizing.room == 120 and sizing.fits


def test_the_compression_is_never_under_the_floor_and_the_sizing_says_when_that_is_too_long():
    sizing = choose_compression(120, 800, 80, LINES)  # 40 lines left, and the thinnest summaries take 80
    assert sizing.compression == MIN_COMPRESSION and sizing.summary == 80 and not sizing.fits
    assert choose_compression(160, 800, 80, LINES).fits  # 80 lines for 80: the floor, and it fits


def test_summary_lines_count_a_bullet_per_allowed_item_and_the_headings_of_their_sections():
    chunks = make_chunks(POSTS, REPEATED, tier_shares(0.3), 3750, levels=LEVELS)
    assert summary_lines(chunks) == sum(c.max_items for c in chunks) + 4 * len({p.section for c in chunks for p in c.posts})
    assert summary_lines(make_chunks(POSTS, REPEATED, tier_shares(0.1), 3750, levels=LEVELS)) <= summary_lines(chunks)
    sections = 4 * len({p.section for c in chunks for p in c.posts})
    assert summary_lines(chunks, SUMMARY_FILL) == SUMMARY_FILL * sum(c.max_items for c in chunks) + sections


def test_image_tweets_take_turns_across_sections_by_level_and_across_sources():
    ranked = rank_media(MEDIA, LEVELS)
    first = ranked[:12]
    biz = [i for i in first if i.group == "biz"]
    assert len(biz) >= 10  # high and three times as many image tweets as the low group
    assert {i.source for i in biz[:3]} == {"@pic0", "@pic1", "@pic2"}  # every source before any gets a second
    assert sorted(i.id for i in ranked) == sorted(i.id for i in MEDIA)


def test_a_recap_groups_image_tweets_take_turns_as_a_low_groups_do_whatever_its_level():
    chat = [_post(n, f"@c{n % 2}", f"meme {n}", group="chat", images=[f"https://img.example/{n}.jpg"])
            for n in range(61, 71)]
    news = [i for i in MEDIA if i.group == "news"]  # level low, as many image tweets
    ranked = rank_media(chat + news, {**LEVELS, "chat": "high"}, frozenset({"chat"}))
    assert sum(1 for i in ranked[:10] if i.group == "chat") == 5
    assert sum(1 for i in rank_media(chat + news, LEVELS)[:10] if i.group == "chat") > 5  # not a recap group: normal


def test_a_brief_that_fits_is_not_cut():
    cuts, brief = plan_cuts(100, _build, MEDIA, LEVELS)
    assert cuts is None and brief.markdown == _build().markdown
    assert brief.stats["whole_length"] == _length(_build())


def test_the_whole_day_given_is_the_one_the_share_is_taken_of():
    full = _length(_build())
    cuts, brief = plan_cuts(50, _build, MEDIA, LEVELS, whole=2 * full)  # half of twice the brief: all of it
    assert cuts is None and brief.stats["whole_length"] == round(2 * full, 1)
    cuts, brief = plan_cuts(50, _build, MEDIA, LEVELS, whole=full)
    assert cuts is not None and _length(brief) <= full / 2


def test_a_small_share_cuts_pictures_to_lines_and_links_and_the_brief_fits():
    full = _length(_build())
    cuts, brief = plan_cuts(50, _build, MEDIA, LEVELS)
    assert _length(brief) <= full / 2
    kept = [i for i in MEDIA if cuts.keeps_pictures(i)]
    lined = [i for i in MEDIA if not cuts.keeps_pictures(i) and cuts.keeps_caption(i)]
    assert kept and lined and len(kept) < len(MEDIA)
    md = brief.markdown
    assert md.count("![](") == sum(len(i.images) for i in kept)
    assert "- 🖼 **@pic" in md  # a line instead of the pictures
    assert f"kept 50%: {len(kept)} of 40 image tweets with pictures" in md


def test_priority_tweets_become_one_liners_only_when_in_full_they_take_more_than_half_of_what_is_kept():
    full = _length(_build())
    priority = _build().stats["band_lengths"]["priority"]
    cuts, brief = plan_cuts(100 * (priority * 2 + 20) / full, _build, MEDIA, LEVELS)
    assert not cuts.priority_one_liners and "second paragraph" in brief.markdown
    cuts, brief = plan_cuts(100 * (priority * 2 - 20) / full, _build, MEDIA, LEVELS)
    assert cuts.priority_one_liners and "Cut to one line each" in brief.markdown
    assert "priority news number 3 with a second paragraph" in brief.markdown  # one line, both paragraphs


def test_priority_tweets_under_half_of_it_still_become_one_liners_when_in_full_the_brief_cannot_fit():
    full = _build()
    priority = full.stats["band_lengths"]["priority"]
    target = priority * 2 + 4  # under their share: in full by the rule
    keep = 100 * target / _length(full)
    assert not bare_cuts(target, full, keep).priority_one_liners

    def crowded(cuts=None):  # a brief whose other parts leave no room for the priority tweets in full
        brief = _build(cuts)
        brief.stats["length"] += priority + 8
        return brief

    cuts, brief = plan_cuts(keep, crowded, MEDIA, LEVELS, whole=_length(full))
    assert cuts.priority_one_liners and "Cut to one line each" in brief.markdown
    assert _length(brief) <= target + 1e-9


def test_a_share_smaller_than_the_barest_brief_gives_the_barest_brief():
    cuts, brief = plan_cuts(1, _build, MEDIA, LEVELS)
    assert cuts == Cuts(1, True, frozenset(), frozenset())
    assert "![](" not in brief.markdown and "🖼 30 more:" in brief.markdown
