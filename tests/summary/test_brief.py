import json
import re
from datetime import datetime, timezone

import pytest

from xmd.summary.brief import (
    brief_stamp, build_brief, digest_item_ids, digest_time, load_item_ids, load_run, measure, reading_minutes,
    window_kind,
)
from xmd.summary.chunk import parse_export
from xmd.digest.export import build_export
from xmd.core.models import FeedItem
from xmd.summary.topics import Topic

NOW = datetime(2026, 9, 19, 16, 0, tzinfo=timezone.utc)
PRIORITY = frozenset({"@vip"})
RECAP = frozenset({"chat"})
LEVELS = {"business": "high", "news": "low"}


def _post(n, source, text, group="", images=None, **kw) -> FeedItem:
    return FeedItem(
        source=source, source_type="x", title=text[:60], url=f"https://x.com/{source.lstrip('@')}/status/{n}",
        published=datetime(2026, 9, 19, 6, n, tzinfo=timezone.utc), full_text=text, group=group,
        images=images or [], **kw,
    )


ITEMS = [
    _post(1, "@vip", "priority post text", group="finance"),
    _post(2, "@pic", "an image tweet", group="business", images=["https://img.example/2.jpg"]),
    _post(3, "@b1", "Acme revenue hit $4.2 billion", group="business"),
    _post(4, "@b2", "Acme revenue up 35% on the year", group="business"),
    _post(5, "@n1", "the council voted today", group="news"),
    _post(6, "@u1", "an ungrouped regular post"),
    _post(7, "@r1", "", group="business", retweet_of_author="orig", retweet_of_text="worth amplifying"),
    _post(8, "@c1", "chatter one", group="chat"),
    _post(9, "@c2", "chatter two", group="chat"),
    _post(10, "@s1", "a new paint set was released", group="arts & crafts"),
]
EXPORT_TEXT, MAPPING = build_export(ITEMS, NOW, priority_sources=PRIORITY, recap_groups=RECAP)
POSTS, _ = parse_export(EXPORT_TEXT, MAPPING)
POST_TEXT = {p.n: p.text for p in POSTS}
NUM = {m["url"]: int(n) for n, m in MAPPING.items()}


def N(post: int) -> int:
    """The export's number for the post created as `post` above."""
    return NUM[next(i.url for i in ITEMS if i.url.endswith(f"/{post}"))]


def _record(chunk, tier, posts, items, ok=True):
    return {"chunk": chunk, "tier": tier, "level": "normal", "ids": [N(p) for p in posts], "ok": ok,
            "reply": {"items": items} if ok else None}


def _item(headline, detail, *posts):
    return {"headline": headline, "detail": detail, "ids": [N(p) for p in posts]}


RECORDS = [
    _record("regular-high-01", "regular", [3, 4], [_item("Acme revenue jumps", "It hit $4.2 billion, up 35%", 3, 4)]),
    _record("regular-02", "regular", [5, 6, 10], [
        _item("Council votes", "The council voted on 63 motions", 5),
        _item("Ungrouped news", "An ordinary post", 6),
        _item("Invented", "cites a post that is in another chunk", 3),
        _item("A paint set ships", "A new paint set was released", 10),
    ]),
    _record("retweets-01", "retweet", [7], [_item("Worth amplifying", "Accounts pushed it", 7)]),
    _record("recap-01", "recap", [8, 9], [_item("Chatter about chatter", "Both accounts posted", 8, 9)]),
]


def _brief(records=RECORDS, repeated=(), **kw):
    return build_brief(
        ITEMS, NOW, records, MAPPING, POST_TEXT, list(repeated), priority_sources=PRIORITY, recap_groups=RECAP,
        levels=LEVELS, full_name="full.md", model_line="test model", **kw,
    )


def _headings(markdown):
    return [line for line in markdown.splitlines() if line.startswith("## ")]


def test_priority_tweets_come_first_and_in_full_and_appear_once():
    md = _brief().markdown
    assert md.index("## ★ Priority") < md.index("## X / business")
    assert md.count("priority post text") == 1


def test_a_priority_tweet_is_shown_whole_but_its_text_cannot_inject_markup():
    """The ★ Priority section is the one place the brief writes a tweet's own words as markdown, and a priority
    source's quote-tweet carries the words of a stranger: neither may bring an image, a link, a tag or an embed."""
    hostile = _post(
        1, "@vip", "look ![](https://tracker.example/p.gif) [x](https://phish.example) <img src=https://t.example/x.png>",
        retweet_of_author="mal_lory", retweet_of_text="![](https://tracker.example/q.gif) [[a private note]]",
    )
    lines = build_brief([hostile], NOW, [], {}, {}, [], priority_sources=PRIORITY).markdown.splitlines()
    body = next(ln for ln in lines if "p.gif" in ln)
    quoted = next(ln for ln in lines if ln.startswith("🔁 Retweeted"))
    assert "![](" not in body and "<img" not in body
    assert re.search(r"(?<!\\)[\[\]]", body) is None  # no unescaped bracket: no link and no image
    assert "!\\[\\](https://tracker.example/p.gif)" in body
    assert quoted.startswith("🔁 Retweeted @mal\\_lory: !\\[\\](https://tracker.example/q.gif)")
    assert "[[" not in quoted


def test_sections_follow_the_importance_of_their_group_and_the_recap_group_comes_last():
    assert _headings(_brief().markdown) == [
        "## ★ Priority", "## X / business", "## X", "## X / arts and crafts", "## X / news", "## X / chat",
    ]  # high, then normal (ungrouped before groups), then low, then recap


def test_a_bullet_links_to_the_posts_it_cites_by_source():
    md = _brief().markdown
    line = next(l for l in md.splitlines() if "Acme revenue jumps" in l)
    assert "[@b1](https://x.com/b1/status/3)" in line and "[@b2](https://x.com/b2/status/4)" in line
    assert line.startswith("- **Acme revenue jumps** — It hit $4.2 billion, up 35% (")


def test_an_item_that_cites_nothing_of_its_own_chunk_is_dropped_and_counted():
    brief = _brief()
    assert "Invented" not in brief.markdown and brief.stats["dropped_items"] == 1
    assert "1 summary item dropped: no cited post was in its chunk" in brief.markdown


def test_a_figure_in_none_of_the_cited_posts_is_marked_and_a_supported_one_is_not():
    md = _brief().markdown
    assert "⚠ not in the cited posts: 63" in next(l for l in md.splitlines() if "Council votes" in l)
    assert "⚠" not in next(l for l in md.splitlines() if "Acme revenue jumps" in l)
    assert _brief().stats["flagged_items"] == 1


def test_two_posts_of_one_repeated_story_make_a_bullet_trending():
    assert "🔥 **Acme revenue jumps**" in _brief(repeated=[(N(3), N(4))]).markdown
    assert "🔥 **" not in _brief().markdown  # no repeated story given, so nothing is trending


def test_a_chunk_that_failed_falls_back_to_one_liners_and_says_so():
    records = [r if r["chunk"] != "regular-02" else _record("regular-02", "regular", [5, 6, 10], [], ok=False)
               for r in RECORDS]
    brief = _brief(records)
    assert "### Not summarized — 1 post" in brief.markdown  # one per section: news, ungrouped, arts and crafts
    assert brief.markdown.count("### Not summarized") == 3
    assert "the council voted today" in brief.markdown and "Council votes" not in brief.markdown
    assert brief.stats["posts_without_summary"] == 3
    assert "3 posts without a summary (chunk failed or not run)" in brief.markdown


def test_a_post_whose_chunk_was_never_run_is_treated_like_a_failure():
    partial = [r for r in RECORDS if r["chunk"] != "retweets-01"]
    assert "### Not summarized" in _brief(partial).markdown


def test_the_recap_group_is_only_a_picture_with_its_themes():
    md = _brief().markdown
    recap = md[md.index("## X / chat"):]
    assert "Recap group: 2 items from 2 sources, only the picture: 1 theme.* [full digest](full.md)" in recap  # a short link, not a file name
    assert "**Chatter about chatter**" in recap and "chatter one" not in recap  # the posts themselves stay out


def test_image_tweets_are_kept_whole_in_their_own_section_and_counted():
    brief = _brief()
    section = brief.markdown[brief.markdown.index("## X / business"):brief.markdown.index("## X\n")]
    assert "an image tweet" in section and "![](https://img.example/2.jpg)" in section
    assert brief.stats["images"] == 1


def test_retweet_themes_sit_in_the_section_of_their_posts():
    md = _brief().markdown
    section = md[md.index("## X / business"):md.index("## X\n")]
    assert "### 🔁 Retweets — 1 theme" in section and "**Worth amplifying**" in section


def test_no_folds_and_no_ampersand_in_any_heading_because_obsidian_cannot_use_either():
    md = _brief().markdown
    assert "<details" not in md
    assert all("&" not in line for line in md.splitlines() if line.startswith("#"))
    assert "## X / arts and crafts" in md


def test_the_contents_link_every_section():
    md = _brief().markdown
    assert "- [★ Priority](#%E2%98%85%20Priority) — 1 tweet" in md
    assert "- [X / business](#X%20/%20business) — 3 entries" in md  # the image tweet, a bullet and a theme


def test_the_header_is_the_date_the_reading_time_and_the_full_digest_and_nothing_else():
    brief = _brief()
    lines = brief.markdown.splitlines()
    assert lines[0] == "# X brief — 2026-09-19"  # only the date: no time of day, no model
    assert lines[2] == "**~1 min read** · [full digest](full.md)"  # the test world is tiny
    for gone in ("items ·", "sources", "quick digest", "Brief by", "compression", "lines a minute", "priority · "):
        assert gone not in "\n".join(lines[:6])


def test_the_reading_time_is_lines_at_the_readers_pace_plus_the_pictures_and_the_stats_keep_the_detail():
    brief = _brief()
    header = next(line for line in brief.markdown.splitlines() if " min read**" in line)
    words, images = measure(brief.markdown)
    assert brief.stats["words"] == words - measure(header)[0] and brief.stats["images"] == images == 1  # not counting the header itself
    assert brief.stats["minutes_at_your_pace"] == round(brief.stats["lines"] / 40 + 4 / 60, 1)
    assert brief.stats["minutes_diagonal"] < brief.stats["minutes_normal"]
    assert set(brief.stats["bands"]) >= {"priority", "images", "summary", "retweet themes", "recap"}


def test_the_model_that_wrote_the_brief_is_only_an_invisible_comment_at_the_end():
    md = _brief().markdown
    assert md.rstrip().endswith("<!-- test model -->")  # provenance for debugging, invisible when reading
    assert "test model" not in md.replace("<!-- test model -->", "")
    assert measure(md)[0] == measure(md.replace("<!-- test model -->", ""))[0]  # and it costs the reader nothing


def test_a_bigger_brief_reports_a_bigger_number_of_minutes():
    tweets = [_post(10 + k % 49, f"@i{k}", f"picture tweet {k}", group="business", images=[f"https://img.example/{k}.jpg"])
              for k in range(90)]  # 90 pictures alone are 6 minutes at 4 seconds each
    items = ITEMS + tweets
    _text, mapping = build_export(items, NOW, priority_sources=PRIORITY, recap_groups=RECAP)
    brief = build_brief(items, NOW, [], mapping, {}, [], priority_sources=PRIORITY, recap_groups=RECAP, full_name="full.md")
    minutes = int(brief.markdown.splitlines()[2].split("~")[1].split(" ")[0])
    assert minutes >= 6 and minutes == round(brief.stats["minutes_at_your_pace"])


def test_a_digests_items_are_read_from_its_item_ids_in_both_markdown_and_html_forms():
    text = ("#### @a <!-- item:aaaa1111aaaa1111 --> [Post Link](u)\n\n<h4>@b <!-- item:bbbb2222bbbb2222 --> <a>x</a></h4>\n"
            "#### @a <!-- item:aaaa1111aaaa1111 --> again\n")
    assert digest_item_ids(text) == ["aaaa1111aaaa1111", "bbbb2222bbbb2222"]  # in order, once each
    assert digest_item_ids("no items here") == []


def test_a_digests_item_ids_come_from_its_items_file_or_else_from_its_full_digest(tmp_path):
    export = tmp_path / ".export" / "2026-09-20-1156.txt"
    full = tmp_path / "2026-09-20-1156.md"
    assert load_item_ids(export, full) is None  # neither is there

    full.write_text("#### @a <!-- item:aaaa1111aaaa1111 --> [Post Link](u)\n", encoding="utf-8")
    assert load_item_ids(export, full) == ["aaaa1111aaaa1111"]  # a digest from before the items file existed

    export.parent.mkdir()
    ids = ["bbbb2222bbbb2222", "cccc3333cccc3333"]
    export.with_suffix(".items.json").write_text(json.dumps(ids), encoding="utf-8")
    assert load_item_ids(export, full) == ids  # the items file wins over the full digest...
    full.unlink()
    assert load_item_ids(export, full) == ids  # ...and the full digest is not needed


def test_the_brief_links_no_full_digest_when_there_is_none():
    md = build_brief(
        ITEMS, NOW, RECORDS, MAPPING, POST_TEXT, [], priority_sources=PRIORITY, recap_groups=RECAP,
        levels=LEVELS, full_name="", model_line="test model",
    ).markdown
    assert md.splitlines()[2].startswith("**~") and md.splitlines()[2].endswith(" min read**")  # no `· [full digest]`
    assert "full digest" not in md  # not in the recap note either


def test_a_digests_time_comes_from_its_file_name_the_end_of_a_frozen_window_or_a_plain_stamp():
    assert digest_time("2026-09-20-1156") == datetime(2026, 9, 20, 11, 56, tzinfo=timezone.utc)
    assert digest_time("frozen-2026-09-18-1600--2026-09-19-1600") == datetime(2026, 9, 19, 16, 0, tzinfo=timezone.utc)
    assert digest_time("no stamp") is None


def test_window_kind_reads_the_labelled_export_header_or_says_neither():
    assert window_kind("# X to MD export — 2026-09-22 21:04 (past 24h)\n...") == "24h"
    assert window_kind("# X to MD export — 2026-09-22 06:00 (since last run)\n...") == "lastrun"
    assert window_kind("# X to MD export — 2026-09-22 06:00\n...") == ""


def test_brief_stamp_is_date_hour_ampm_and_window_kind():
    assert brief_stamp(datetime(2026, 9, 22, 21, 4, tzinfo=timezone.utc), "24h") == "2026-09-22-9pm-24h"
    assert brief_stamp(datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc), "lastrun") == "2026-09-22-6am-lastrun"
    assert brief_stamp(datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc), "24h") == "2026-09-22-12am-24h"
    assert brief_stamp(datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc), "24h") == "2026-09-22-12pm-24h"
    assert brief_stamp(datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc), "") == "2026-09-22-6am"


def test_measure_counts_link_text_and_images_but_not_urls_or_markup():
    assert measure("![](http://i/1.jpg) [text](https://u/1) https://u/2 word **bold** <!-- x --> <a id=\"y\"></a>") == (3, 1)
    assert reading_minutes(230, 0, 230) == 1.0 and reading_minutes(0, 15, 230) == 1.0  # 15 images x 4 s


def test_a_saved_run_is_loaded_with_its_records_in_run_order(tmp_path):
    (tmp_path / "recap-01.json").write_text(json.dumps({"chunk": "recap-01", "ok": True}), encoding="utf-8")
    (tmp_path / "regular-01.json").write_text(json.dumps({"chunk": "regular-01", "ok": False}), encoding="utf-8")
    (tmp_path / "run.json").write_text(
        json.dumps({"label": "t", "chunks": [{"chunk": "regular-01"}, {"chunk": "recap-01"}]}), encoding="utf-8")
    manifest, records = load_run(tmp_path)
    assert manifest["label"] == "t" and [r["chunk"] for r in records] == ["regular-01", "recap-01"]


@pytest.mark.parametrize("levels", [{}, {"business": "low", "news": "high"}])
def test_the_order_of_sections_follows_whatever_levels_the_run_used(levels):
    md = build_brief(ITEMS, NOW, RECORDS, MAPPING, POST_TEXT, [], priority_sources=PRIORITY, recap_groups=RECAP,
                     levels=levels).markdown
    headings = _headings(md)
    assert headings[0] == "## ★ Priority" and headings[-1] == "## X / chat"
    if levels:
        assert headings.index("## X / news") < headings.index("## X") < headings.index("## X / business")


def _images_band(images_per_tweet):
    """The lines of the Images band for image tweets with the given numbers of pictures (and nothing else)."""
    items = [_post(20 + k, f"@p{k}", f"caption number {k}", group="business",
                   images=[f"https://img.example/{k}-{j}.jpg" for j in range(n)]) for k, n in enumerate(images_per_tweet)]
    _text, mapping = build_export(items, NOW, priority_sources=PRIORITY, recap_groups=RECAP)
    md = build_brief(items, NOW, [], mapping, {}, [], priority_sources=PRIORITY, recap_groups=RECAP, levels=LEVELS).markdown
    lines = md.splitlines()
    start = next(k for k, line in enumerate(lines) if line.startswith("### 🖼 Images"))
    stop = next((k for k in range(start + 1, len(lines)) if lines[k].startswith(("### ", "---", "<a id="))), len(lines))
    return lines[start:stop]


def test_the_caption_comes_below_its_picture_and_every_picture_is_followed_by_a_blank_line():
    lines = _images_band([2])
    first, second = (k for k, line in enumerate(lines) if line.startswith("![]("))
    caption = next(k for k, line in enumerate(lines) if "caption number 0" in line)
    assert second == first + 2 and lines[first + 1] == ""  # a blank line between the two pictures of one tweet
    assert caption == second + 2 and lines[second + 1] == ""  # the caption comes after both, past a blank line
    assert not lines[caption].startswith("- ") and "[↗](https://x.com/p0/status/20)" in lines[caption]  # a caption, with its link


def test_consecutive_image_tweets_are_separated_by_a_blank_line_and_pictures_are_never_adjacent():
    lines = _images_band([1, 1])
    pictures = [k for k, line in enumerate(lines) if line.startswith("![](")]
    captions = [k for k, line in enumerate(lines) if "caption number" in line]
    assert len(pictures) == 2 and len(captions) == 2
    assert pictures[0] < captions[0] < pictures[1] < captions[1]  # picture, its caption, then the next tweet's picture
    assert lines[captions[0] + 1] == ""  # a blank line between the tweets
    assert all(not (lines[k].startswith("![](") and lines[k + 1].startswith("![](")) for k in range(len(lines) - 1))


def test_a_sections_summary_and_louder_names_open_it_as_quotes_above_its_bullets():
    topics = {"X / business": [Topic("ACME", 25, 3, 7.3)]}
    brief = _brief(topics=topics, summaries={"X / business": "ACME dominated, posted about by 3 accounts."})
    section = brief.markdown[brief.markdown.index("## X / business"):brief.markdown.index("## X\n")]
    lead = section.split("### ")[0]
    assert "> **In short:** ACME dominated, posted about by 3 accounts." in lead
    assert "> 🔥 **Louder than usual:** **ACME** (25 posts from 3 accounts, usually ~7)" in lead
    assert lead.index("In short") < lead.index("Louder") and ">\n" in lead  # two quoted paragraphs
    assert brief.stats["section_summaries"] == 1 and brief.stats["louder_topics"] == 1
    assert "section summary" in brief.stats["bands"]


def test_a_section_without_a_summary_or_topics_has_no_lead_and_the_priority_section_never_has_one():
    md = _brief(topics={"X / news": [Topic("Foo", 7, 3, 1.0)]}).markdown
    assert "In short" not in md and md.count("Louder than usual") == 1
    assert "Louder than usual" not in md[md.index("## ★ Priority"):md.index("## X / business")]


def test_a_priority_tweet_shows_its_pictures_first_with_a_blank_line_after_each_and_its_text_below():
    tweet = _post(30, "@vip", "the priority caption", group="finance", images=["https://img.example/a.jpg", "https://img.example/b.jpg"])
    items = [tweet]
    _text, mapping = build_export(items, NOW, priority_sources=PRIORITY, recap_groups=RECAP)
    md = build_brief(items, NOW, [], mapping, {}, [], priority_sources=PRIORITY, recap_groups=RECAP).markdown
    lines = md.splitlines()
    heading = next(k for k, line in enumerate(lines) if line.startswith("#### @vip"))
    assert lines[heading + 2] == "![](https://img.example/a.jpg)" and lines[heading + 3] == ""
    assert lines[heading + 4] == "![](https://img.example/b.jpg)" and lines[heading + 5] == ""
    assert lines[heading + 6] == "the priority caption"  # the text is the caption, below both pictures
