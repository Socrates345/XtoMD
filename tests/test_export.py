from datetime import datetime, timezone

from xmd.export import build_export
from xmd.models import FeedItem

NOW = datetime(2026, 7, 8, 8, 0)
PRIORITY = frozenset({"@vip"})
RECAP = frozenset({"chat"})


def _post(n, source="@a", text="a plain post", group="", images=None, **kw) -> FeedItem:
    return FeedItem(
        source=source, source_type="x", title=text[:60], url=f"https://x.com/{source.lstrip('@')}/status/{n}",
        published=datetime(2026, 7, 8, 6, n, tzinfo=timezone.utc), full_text=text, group=group,
        images=images or [], **kw,
    )


def _export(items, **kw):
    return build_export(items, NOW, priority_sources=PRIORITY, recap_groups=RECAP, **kw)


def test_priority_and_media_are_kept_out_of_the_model_input():
    text, mapping = _export([
        _post(1, "@vip", "priority post text"),
        _post(2, text="a meme", images=["https://pbs.twimg.com/media/1.jpg"]),
        _post(3, text="a regular post"),
    ])
    assert "priority post text" not in text and "a meme" not in text
    assert "a regular post" in text
    assert [m["tier"] for m in mapping.values()] == ["regular"]
    assert "shown verbatim, not included (priority 1 · media 1)" in text


def test_lines_are_numbered_and_the_map_points_back_to_the_post():
    text, mapping = _export([_post(1, text="first"), _post(2, source="@b", text="second")])
    assert "[1] @a first" in text and "[2] @b second" in text
    assert mapping["1"] == {
        "url": "https://x.com/a/status/1", "source": "@a", "tier": "regular", "group": "", "words": 1,
    }
    assert mapping["2"]["url"] == "https://x.com/b/status/2"


def test_the_map_counts_full_text_words_even_when_the_line_is_clipped():
    long_post = _post(1, text=" ".join(["word"] * 200) + " https://t.co/abc")
    text, mapping = _export([long_post], limits={"regular": 40})
    assert mapping["1"]["words"] == 200  # the URL is not a word
    assert len(text.split("[1] @a ")[1].split()) < 20  # while the exported line was cut short


def test_groups_and_tiers_are_headed_and_retweets_and_recap_are_separate_bands():
    retweet = _post(2, "@b", "", retweet_of_author="orig", retweet_of_text="the original")
    text, mapping = _export([
        _post(1, text="a regular post", group="finance"),
        retweet,
        _post(3, "@c", "recap chatter", group="chat"),
    ])
    # numbered in section order (the ungrouped section first, then groups alphabetically)
    assert "## X\n### retweets\n[1] @b RT@orig the original" in text
    assert "## X / chat\n### recap\n[2] @c recap chatter" in text
    assert "## X / finance\n### regular\n[3] @a a regular post" in text
    assert [m["tier"] for m in mapping.values()] == ["retweet", "recap", "regular"]


def test_quotes_keep_the_commentary_and_the_quoted_post_and_urls_are_stripped():
    quote = _post(1, text="this is huge https://t.co/abc", retweet_of_author="orig", retweet_of_text="the &amp; original")
    text, _ = _export([quote])
    assert "[1] @a QT@orig this is huge ⟪the & original⟫" in text
    assert "t.co" not in text


def test_a_retweets_echoed_copy_is_not_repeated():
    echo = _post(1, text="an original post worth reading", retweet_of_author="orig",
                 retweet_of_text="an original post worth reading")
    text, _ = _export([echo])
    assert text.count("an original post worth reading") == 1
    assert "RT@orig an original post worth reading" in text


def test_each_tier_is_clipped_to_its_own_limit():
    long = "word " * 200
    retweet = _post(2, "@b", "", retweet_of_author="orig", retweet_of_text=long)
    text, _ = _export([_post(1, text=long), retweet], limits={"regular": 50, "retweet": 20, "recap": 20})
    lines = {ln.split("]")[0] + "]": ln for ln in text.splitlines() if ln.startswith("[")}
    assert lines["[1]"].endswith("…") and len(lines["[1]"]) < 80
    assert lines["[2]"].endswith("…") and len(lines["[2]"]) < 60


def test_repeated_stories_are_listed_by_number_and_only_when_two_are_exported():
    story = "the central bank surprised markets with an emergency rate cut on friday morning"
    text, _ = _export([
        _post(1, "@a", story),
        _post(2, "@b", story),
        _post(3, "@vip", story),  # kept back: must not show up as a hint
        _post(4, "@c", "unrelated remarks about something else entirely different today"),
    ])
    assert "## repeated stories\nrepeated: [1] [2]" in text


def test_no_repeated_section_when_nothing_repeats():
    text, _ = _export([_post(1, text="only one post here")])
    assert "repeated" not in text


def test_header_counts_the_window():
    text, _ = _export([_post(1), _post(2, "@vip")], label="past 24h")
    assert text.startswith("# X to MD export — 2026-07-08 08:00 (past 24h)\n# 2 items: 1 below for the model")
