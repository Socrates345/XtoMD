from datetime import datetime, timezone

from xmd.models import FeedItem
from xmd.render import build_markdown

NOW = datetime(2026, 7, 8, 8, 0)


def _tweet(n: int, text: str, images=None, group: str = "") -> FeedItem:
    return FeedItem(
        source="@someone",
        source_type="x",
        title=text[:60],
        url=f"https://x.com/someone/status/{n}",
        author="@someone",
        published=datetime(2026, 7, 8, 6, n, tzinfo=timezone.utc),
        full_text=text,
        images=images or [],
        group=group,
    )


def test_markdown_structure():
    items = [
        _tweet(2, "part two of the thread"),
        _tweet(1, "oil hits $120 — thread", images=["https://pbs.twimg.com/media/G1.jpg"]),
    ]
    md = build_markdown(items, NOW, label="today")

    assert md.startswith("# X to MD — 2026-07-08 08:00 (today)")
    assert "## X" in md
    for item in items:
        assert f"#### @someone <!-- item:{item.id} --> [Post Link]({item.url})" in md
    assert "\n### @someone\n" not in md  # no separate per-source grouping header
    assert "![](https://pbs.twimg.com/media/G1.jpg)" in md
    # chronological within a source: the thread reads top-down
    assert md.index("oil hits $120") < md.index("part two")
    assert "2026-07-08 06:01 UTC" in md


def test_item_heading_combines_source_anchor_link_and_date():
    item = _tweet(1, "hello world")
    md = build_markdown([item], NOW)
    assert (
        f"#### @someone <!-- item:{item.id} --> "
        "[Post Link](https://x.com/someone/status/1) · 2026-07-08 06:01 UTC"
    ) in md


def test_falls_back_to_text_and_is_uncapped():
    items = [
        FeedItem(
            source="@someone", source_type="x", title=f"post {n}",
            url=f"https://x.com/someone/{n}", text=f"summary only {n}",
        )
        for n in range(30)
    ]
    md = build_markdown(items, NOW)
    assert md.count("<!-- item:") == 30  # never capped
    assert all(f"summary only {n}" in md for n in range(30))  # empty full_text -> text


def test_empty():
    assert "Nothing new" in build_markdown([], NOW)


def test_retweet_shows_original_alongside_own_comment():
    item = FeedItem(
        source="@someone", source_type="x", title="RT @orig: original content",
        url="https://x.com/someone/status/1", full_text="my added take",
        retweet_of_author="orig", retweet_of_text="original content",
    )
    md = build_markdown([item], NOW)
    assert "my added take" in md
    assert "🔁 Retweeted @orig: original content" in md


def test_groups_get_their_own_section():
    items = [
        _tweet(1, "oil hits $120", group="finance"),
        _tweet(2, "a general tweet"),
    ]
    md = build_markdown(items, NOW)
    assert "## X / finance" in md
    # main section (no group) sorts before its groups
    assert md.index("## X\n") < md.index("## X / finance")
