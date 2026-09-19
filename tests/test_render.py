import re
from datetime import datetime, timezone
from html import unescape
from urllib.parse import unquote

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


def test_retweets_sort_after_normal_tweets_within_section():
    retweet = FeedItem(
        source="@aaa", source_type="x", title="RT @orig: something",
        url="https://x.com/aaa/status/9",
        published=datetime(2026, 7, 8, 5, 0, tzinfo=timezone.utc),
        retweet_of_author="orig", retweet_of_text="something",
    )
    normal = _tweet(1, "a plain tweet")  # source "@someone", sorts after "@aaa" alphabetically
    md = build_markdown([retweet, normal], NOW)
    assert md.index("a plain tweet") < md.index("🔁 Retweeted @orig: something")


def test_priority_source_sorts_first_within_section():
    low = FeedItem(
        source="@aaa", source_type="x", title="from aaa",
        url="https://x.com/aaa/status/1", full_text="from aaa",
        published=datetime(2026, 7, 8, 6, 0, tzinfo=timezone.utc),
    )
    high = FeedItem(
        source="@zzz", source_type="x", title="from zzz",
        url="https://x.com/zzz/status/1", full_text="from zzz",
        published=datetime(2026, 7, 8, 6, 0, tzinfo=timezone.utc),
    )
    md = build_markdown([low, high], NOW, priority_sources=frozenset({"@zzz"}))
    assert md.index("from zzz") < md.index("from aaa")


def test_priority_lookup_is_live_not_baked_into_the_item():
    """Priority is resolved from `priority_sources` at render time, not
    stored on the FeedItem — the same item renders differently depending on
    what's passed in, so re-marking a source priority in sources/x.md
    reorders already-fetched items on the next digest, no re-fetch needed."""
    item = FeedItem(
        source="@aaa", source_type="x", title="from aaa",
        url="https://x.com/aaa/status/1", full_text="from aaa",
    )
    other = FeedItem(
        source="@zzz", source_type="x", title="from zzz",
        url="https://x.com/zzz/status/1", full_text="from zzz",
    )
    without = build_markdown([item, other], NOW)
    with_priority = build_markdown([item, other], NOW, priority_sources=frozenset({"@zzz"}))
    assert without.index("from aaa") < without.index("from zzz")  # alphabetical by default
    assert with_priority.index("from zzz") < with_priority.index("from aaa")  # flipped by priority


def test_groups_get_their_own_section():
    items = [
        _tweet(1, "oil hits $120", group="finance"),
        _tweet(2, "a general tweet"),
    ]
    md = build_markdown(items, NOW)
    assert "## X / finance" in md
    # main section (no group) sorts before its groups
    assert md.index("## X\n") < md.index("## X / finance")


def test_toc_lists_sections_with_item_counts_when_multiple_sections():
    items = [
        _tweet(1, "oil hits $120", group="finance"),
        _tweet(2, "a general tweet"),
    ]
    md = build_markdown(items, NOW)
    assert "**2 items · 1 source**" in md
    assert "- [X](#X) — 1 item" in md
    assert "- [X / finance](#X%20/%20finance) — 1 item" in md


def test_single_section_digest_has_stats_line_but_no_toc_bullets():
    items = [_tweet(1, "a"), _tweet(2, "b")]  # both default group -> one section
    md = build_markdown(items, NOW)
    assert "**2 items · 1 source**" in md
    assert "- [" not in md  # nothing to jump between with only one section


def _toc_targets_resolve(md: str) -> None:
    """Every contents link must resolve both ways a viewer might try: Obsidian
    matches the decoded fragment against a heading's text, a browser preview
    (VS Code) matches it against an element id."""
    fragments = [unquote(f) for f in re.findall(r"^- \[[^\n]*?\]\(#(\S+?)\) — ", md, re.M)]
    headings = re.findall(r"^## (.+)$", md, re.M)
    ids = [unescape(i) for i in re.findall(r'<a id="([^"]*)"></a>', md)]
    assert fragments and sorted(fragments) == sorted(headings) == sorted(ids)


def test_section_link_targets_match_heading_text_and_anchor_id():
    items = [
        _tweet(1, "oil hits $120", group="finance"),
        _tweet(2, "a general tweet"),
    ]
    md = build_markdown(items, NOW)
    assert '<a id="X"></a>\n## X\n' in md
    assert '<a id="X / finance"></a>\n## X / finance\n' in md
    _toc_targets_resolve(md)


def test_section_link_targets_survive_awkward_group_names():
    items = [
        _tweet(1, "a", group='AI & ML (beta) "quoted" 100%'),
        _tweet(2, "b", group="Ünïcode 日本"),
        _tweet(3, "c"),
    ]
    md = build_markdown(items, NOW)
    _toc_targets_resolve(md)
    # a link destination with no raw space or parenthesis, so it parses as one link
    assert "(#X / AI" not in md
    assert "[X / AI & ML (beta) \"quoted\" 100%](#X%20/%20AI%20%26%20ML%20%28beta%29%20%22quoted%22%20100%25)" in md
    assert '<a id="X / AI &amp; ML (beta) &quot;quoted&quot; 100%"></a>' in md


def test_retweets_collapsed_into_one_details_block_per_section():
    normal1 = FeedItem(
        source="@aaa", source_type="x", title="normal one",
        url="https://x.com/aaa/status/1", full_text="normal one text",
        published=datetime(2026, 7, 8, 6, 0, tzinfo=timezone.utc),
    )
    normal2 = FeedItem(
        source="@bbb", source_type="x", title="normal two",
        url="https://x.com/bbb/status/1", full_text="normal two text",
        published=datetime(2026, 7, 8, 6, 1, tzinfo=timezone.utc),
    )
    rt1 = FeedItem(
        source="@ccc", source_type="x", title="RT @x1: one",
        url="https://x.com/ccc/status/1",
        retweet_of_author="x1", retweet_of_text="retweet one text",
        published=datetime(2026, 7, 8, 5, 0, tzinfo=timezone.utc),
    )
    rt2 = FeedItem(
        source="@ddd", source_type="x", title="RT @y1: two",
        url="https://x.com/ddd/status/1",
        retweet_of_author="y1", retweet_of_text="retweet two text",
        published=datetime(2026, 7, 8, 5, 1, tzinfo=timezone.utc),
    )
    md = build_markdown([normal1, normal2, rt1, rt2], NOW)

    assert md.count("<details>") == 1  # one toggle for the whole section, not per source
    assert md.count("</details>") == 1
    assert "<summary>🔁 Retweets — 2 items</summary>" in md

    details_start = md.index("<details>")
    # normal tweets are never collapsed: fully visible, before the toggle
    assert md.index("normal one text") < details_start
    assert md.index("normal two text") < details_start
    # both retweets live inside the single collapsed block
    assert md.index("retweet one text") > details_start
    assert md.index("retweet two text") > details_start


def _retweet(n: int, source: str, original: str, comment: str = "", group: str = "") -> FeedItem:
    return FeedItem(
        source=source, source_type="x", title=f"RT @orig: {original}"[:60],
        url=f"https://x.com/{source.lstrip('@')}/status/{n}",
        published=datetime(2026, 7, 8, 5, n, tzinfo=timezone.utc),
        full_text=comment, retweet_of_author="orig", retweet_of_text=original, group=group,
        images=[f"https://pbs.twimg.com/media/R{n}.jpg"],
    )


def _details_blocks(md: str) -> list[str]:
    start = 0
    blocks = []
    while (i := md.find("<details>", start)) != -1:
        end = md.index("</details>", i) + len("</details>")
        blocks.append(md[i:end])
        start = end
    return blocks


def test_retweet_toggle_is_one_html_block_with_no_markdown_inside():
    """Markdown inside an HTML block isn't parsed by every viewer (Obsidian
    shows the raw `####` / `![]()` source), and a blank line is what makes a
    renderer stop treating the rest as HTML — so the toggle's content must be
    HTML throughout, with no blank line from <details> to </details>."""
    md = build_markdown(
        [_tweet(1, "a plain tweet"), _retweet(1, "@ccc", "first"), _retweet(2, "@ddd", "second")],
        NOW,
    )
    (block,) = _details_blocks(md)

    assert "\n\n" not in block
    for markdown_syntax in ("####", "![](", "](http", "\n---"):
        assert markdown_syntax not in block
    first = _retweet(1, "@ccc", "first")
    assert (
        f'<h4>@ccc <!-- item:{first.id} --> <a href="https://x.com/ccc/status/1">Post Link</a>'
        " · 2026-07-08 05:01 UTC</h4>"
    ) in block
    assert '<img src="https://pbs.twimg.com/media/R1.jpg">' in block
    assert block.count("<hr>") == 1  # between the two retweets, not around them
    assert md.endswith("</details>\n")  # the blank line that closes the HTML block


def test_content_after_retweet_toggle_is_unaffected():
    md = build_markdown(
        [
            _tweet(1, "a plain tweet"),
            _retweet(1, "@ccc", "first"),
            _tweet(2, "finance tweet", group="finance"),
            _retweet(2, "@ddd", "second", group="finance"),
        ],
        NOW,
    )
    first_block, second_block = _details_blocks(md)
    # the next section's anchor + heading sit after a blank line, outside the toggle
    assert f"{first_block}\n\n<a id=\"X / finance\"></a>\n## X / finance\n" in md
    assert "#### @someone" in md[md.index("## X / finance") : md.index(second_block)]


def test_retweet_toggle_escapes_tweet_text_and_keeps_urls_clickable():
    hostile = _retweet(
        1, "@r&d", 'x<y && </details> <script>alert(1)</script> "q" — see https://t.co/abc. and '
        "(https://example.com/a?x=1&y=2), or https://en.wikipedia.org/wiki/Foo_(bar)",
        comment="first paragraph\n\n \nsecond paragraph",
    )
    md = build_markdown([hostile], NOW)
    (block,) = _details_blocks(md)

    assert md.count("</details>") == 1  # the tweet's own </details> can't close the toggle early
    assert "<script>" not in md
    assert "&lt;script&gt;" in block and "x&lt;y &amp;&amp; &lt;/details&gt;" in block
    assert "<h4>@r&amp;d " in block
    # blank/whitespace-only lines between a comment's paragraphs never reach the block
    assert "\n\n" not in block
    assert "<p>first paragraph</p>\n<p>second paragraph</p>" in block
    # bare URLs are linked like a markdown renderer would; trailing punctuation and
    # an unbalanced ")" stay outside the link, a balanced one stays inside
    assert '<a href="https://t.co/abc">https://t.co/abc</a>. and' in block
    assert '(<a href="https://example.com/a?x=1&amp;y=2">https://example.com/a?x=1&amp;y=2</a>),' in block
    assert '<a href="https://en.wikipedia.org/wiki/Foo_(bar)">' in block
