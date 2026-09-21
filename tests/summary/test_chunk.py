from datetime import datetime, timezone

import pytest

from xmd.summary.chunk import (
    DEFAULT_COMPRESSION, MIN_TARGET_WORDS, THEME_SHARES, Chunk, Post, estimate_tokens, make_chunks, parse_compression,
    parse_export, tier_shares,
)
from xmd.digest.export import build_export
from xmd.core.models import FeedItem
from xmd.core.tiers import RECAP, REGULAR, RETWEET

NOW = datetime(2026, 7, 8, 8, 0)
RATIOS = {REGULAR: 0.5, RETWEET: 0.5, RECAP: 0.5}


def _post(n, source="@a", text="a plain post", group="", **kw) -> FeedItem:
    return FeedItem(
        source=source, source_type="x", title=text[:60], url=f"https://x.com/{source.lstrip('@')}/status/{n}",
        published=datetime(2026, 7, 8, 6, n % 60, tzinfo=timezone.utc), full_text=text, group=group, **kw,
    )


def _parsed(items, **kw):
    text, mapping = build_export(items, NOW, recap_groups=frozenset({"chatA", "chatB"}), **kw)
    return parse_export(text, mapping)


def _p(n, tier=REGULAR, section="X", text="word " * 10, words=None, group="") -> Post:
    return Post(n, section, tier, text.strip(), words if words is not None else len(text.split()), group)


def test_parse_reads_back_what_build_export_wrote():
    retweet = _post(2, "@b", "", retweet_of_author="orig", retweet_of_text="the original")
    posts, repeated = _parsed([
        _post(1, text="a regular post", group="finance"),
        retweet,
        _post(3, "@c", "recap chatter", group="chatA"),
    ])
    assert [(p.n, p.section, p.tier) for p in posts] == [
        (1, "X", RETWEET), (2, "X / chatA", RECAP), (3, "X / finance", REGULAR),
    ]
    assert posts[0].text == "@b RT@orig the original"
    assert posts[0].line == "[1] @b RT@orig the original"
    assert repeated == []


def test_parse_takes_full_words_from_the_map_not_from_the_clipped_line():
    long_post = _post(1, text=" ".join(["word"] * 200))
    text, mapping = build_export([long_post], NOW, limits={"regular": 40})
    (post,), _ = parse_export(text, mapping)
    assert post.words == 200 and len(post.text.split()) < 20


def test_a_post_with_zero_words_stays_at_zero_and_an_old_map_without_words_uses_the_line():
    text = "## X\n### regular\n[1] @a\n[2] @b two words here\n"
    posts, _ = parse_export(text, {"1": {"tier": REGULAR, "words": 0}, "2": {"tier": REGULAR}})
    assert [p.words for p in posts] == [0, 4]


def test_parse_reads_the_repeated_story_groups():
    text = "# header\n\n## X\n### regular\n[1] @a one\n[2] @b two\n[3] @c three\n\n## repeated stories\nrepeated: [1] [3]\nrepeated: [2] [3]\n"
    mapping = {str(n): {"tier": REGULAR, "words": 1} for n in (1, 2, 3)}
    posts, repeated = parse_export(text, mapping)
    assert [p.n for p in posts] == [1, 2, 3] and repeated == [(1, 3), (2, 3)]


def test_a_post_missing_from_the_map_is_an_error():
    with pytest.raises(ValueError, match=r"\[7\]"):
        parse_export("## X\n### regular\n[7] @a text\n", {})


def test_tiers_are_never_mixed_and_come_in_reading_order():
    posts = [_p(1, RECAP, "X / chatA"), _p(2, RETWEET), _p(3, REGULAR), _p(4, REGULAR)]
    chunks = make_chunks(posts, [], RATIOS)
    assert [(c.id, [p.n for p in c.posts]) for c in chunks] == [
        ("regular-01", [3, 4]), ("retweets-01", [2]), ("recap-01", [1]),
    ]


def test_every_post_lands_in_exactly_one_chunk_in_order_and_within_the_budget():
    posts = [_p(n, section="X" if n < 30 else "X / finance") for n in range(1, 61)]
    chunks = make_chunks(posts, [], RATIOS, max_input_tokens=100, chars_per_token=3.0)  # 300 characters
    assert len(chunks) > 3
    assert [p.n for c in chunks for p in c.posts] == list(range(1, 61))
    for chunk in chunks:  # what follows the two header lines is exactly what the budget measures
        rendered = chunk.render()
        header = "\n".join(rendered.splitlines()[:2]) + "\n"
        assert len(rendered) - len(header) <= 300


def test_regular_posts_pack_across_sections_but_recap_groups_stay_apart():
    posts = [
        _p(1, section="X"), _p(2, section="X / finance"),
        _p(3, RECAP, "X / chatA"), _p(4, RECAP, "X / chatB"),
    ]
    chunks = make_chunks(posts, [], RATIOS)
    regular = [c for c in chunks if c.tier == REGULAR]
    recap = [c for c in chunks if c.tier == RECAP]
    assert len(regular) == 1 and "## X\n" in regular[0].render() and "## X / finance\n" in regular[0].render()
    assert [[p.n for p in c.posts] for c in recap] == [[3], [4]]


def test_a_recap_group_too_big_for_one_call_is_split_but_stays_within_its_group():
    posts = [_p(n, RECAP, "X / chatA") for n in range(1, 21)] + [_p(21, RECAP, "X / chatB")]
    chunks = make_chunks(posts, [], RATIOS, max_input_tokens=50, chars_per_token=3.0)
    assert len(chunks) > 2
    assert {p.section for p in chunks[-1].posts} == {"X / chatB"}
    assert all(len({p.section for p in c.posts}) == 1 for c in chunks)


def test_the_target_is_the_tier_ratio_of_the_full_words_with_a_floor():
    posts = [_p(1, words=400), _p(2, words=200), _p(3, RETWEET, words=10)]
    regular, retweets = make_chunks(posts, [], {REGULAR: 0.30, RETWEET: 0.10, RECAP: 0.03})
    assert regular.target_words == 180  # 30% of 600, not of the clipped lines
    assert retweets.target_words == MIN_TARGET_WORDS  # 10% of 10 would be 1


def test_the_regular_share_is_the_compression_and_the_other_tiers_keep_their_fixed_shares():
    assert tier_shares(0.30)[REGULAR] == 0.30 and tier_shares(0.10)[REGULAR] == 0.10  # the plan's scenarios A and B
    assert DEFAULT_COMPRESSION == 0.30  # the daily run is scenario A
    for compression in (0.30, 0.10):
        assert tier_shares(compression)[RETWEET] == 0.04  # retweets rank below the handles' own posts
        assert tier_shares(compression)[RECAP] == 0.06  # the recap group: 2% was "way too short"
    assert THEME_SHARES[RECAP] > THEME_SHARES[RETWEET]  # but retweets stay the smaller share


def test_a_compression_is_a_ratio_or_a_percentage():
    assert parse_compression("0.30") == 0.3 and parse_compression("30%") == 0.3 and parse_compression(" 10% ") == 0.1
    assert parse_compression("1") == 1.0 and parse_compression("100%") == 1.0  # keep everything: the edge
    assert parse_compression("12.5%") == 0.125


@pytest.mark.parametrize("text", ["30", "0", "0%", "150%", "-0.1", "nan", "much", ""])
def test_anything_else_is_refused_with_what_to_write(text):
    with pytest.raises(ValueError, match=r"write 0\.30, or 30% for a percentage"):  # "30" is not guessed at
        parse_compression(text)


def test_repeated_stories_are_cut_down_to_the_chunk_and_singletons_dropped():
    posts = [_p(n, section="X") for n in range(1, 41)]
    chunks = make_chunks(posts, [(1, 2, 40), (3, 39)], RATIOS, max_input_tokens=60, chars_per_token=3.0)
    first, last = chunks[0], chunks[-1]
    assert (1, 2) in first.repeated and all(40 not in g for g in first.repeated)
    assert (40,) not in last.repeated  # a lone survivor is no longer a repeat
    assert all(len(g) >= 2 for c in chunks for g in c.repeated)


def test_render_is_a_header_then_posts_under_section_headings_then_the_repeats():
    chunk = Chunk(
        id="regular-01", tier=REGULAR, target_words=50, repeated=((1, 3),),
        posts=(_p(1, text="@a one"), _p(2, section="X / finance", text="@b two"), _p(3, section="X / finance", text="@c three")),
    )
    assert chunk.render() == (
        "Band: regular\n"
        "Length: at most 2 items and about 50 words in total, all headlines and details together; shorter is fine.\n"
        "\n## X\n[1] @a one\n"
        "\n## X / finance\n[2] @b two\n[3] @c three\n"
        "\n## repeated stories\nrepeated: [1] [3]\n"
    )
    assert Chunk("retweets-01", RETWEET, (_p(1),), (), 20).band == "retweets"


def test_the_item_limit_follows_the_target_at_each_tiers_item_length_and_never_exceeds_the_posts():
    many = tuple(_p(n) for n in range(1, 101))
    assert Chunk("r", REGULAR, many, (), 830).max_items == 28  # 830 / 30
    assert Chunk("t", RETWEET, many, (), 100).max_items == 3  # plain retweets are themes: 100 / 30
    assert Chunk("t", RETWEET, many, (), 393).max_items == 8  # and a handful at most
    assert Chunk("c", RECAP, many, (), 251).max_items == 8  # 251 / 30 = 8
    assert Chunk("c", RECAP, many, (), 900).max_items == 14  # a recap gets more themes than retweets, still capped
    assert Chunk("r", REGULAR, many[:5], (), 830).max_items == 5  # not more items than posts
    assert Chunk("r", REGULAR, many[:5], (), 1).max_items == 1  # at least one


def test_the_minimum_is_a_quarter_of_the_limit_and_at_least_one():
    many = tuple(_p(n) for n in range(1, 101))
    assert Chunk("r", REGULAR, many, (), 830).min_items == 7  # 28 items -> 7
    assert Chunk("t", RETWEET, many, (), 393).min_items == 2  # 8 items -> 2
    assert Chunk("c", RECAP, many, (), 251).min_items == 2
    assert Chunk("c", RECAP, many[:2], (), 20).min_items == 1


def test_the_token_estimate_is_calibrated_on_the_measured_ratio_and_rounds_up():
    assert estimate_tokens("x" * 360) == 100
    assert estimate_tokens("x" * 361) == 101
    assert estimate_tokens("x" * 300, chars_per_token=3.0) == 100


def test_the_group_of_each_post_is_read_from_the_map():
    posts, _ = _parsed([_post(1, text="a regular post", group="finance"), _post(2, text="no group")])
    # numbered in section order: the ungrouped post first, then the finance one
    assert [(p.n, p.section, p.group) for p in posts] == [(1, "X", ""), (2, "X / finance", "finance")]


def test_chunks_are_made_per_importance_level_most_important_first_and_never_mix_levels():
    posts = [_p(1, group="news"), _p(2, group="business"), _p(3, group=""), _p(4, group="business")]
    chunks = make_chunks(posts, [], RATIOS, levels={"business": "high", "news": "low"})
    assert [(c.id, c.level, [p.n for p in c.posts]) for c in chunks] == [
        ("regular-high-01", "high", [2, 4]), ("regular-02", "normal", [3]), ("regular-low-03", "low", [1]),
    ]


def test_the_target_follows_the_level_and_no_levels_means_everything_is_normal():
    posts = [_p(1, group="business", words=100), _p(2, group="", words=100), _p(3, group="news", words=100)]
    high, normal, low = make_chunks(posts, [], {REGULAR: 0.4, RETWEET: 0.4, RECAP: 0.4}, levels={"business": "high", "news": "low"})
    assert (high.target_words, normal.target_words, low.target_words) == (60, 40, 20)  # x1.5, x1, x0.5
    (only,) = make_chunks(posts, [], RATIOS)
    assert only.level == "normal" and len(only.posts) == 3


def test_the_recap_group_ignores_levels_so_a_low_written_on_it_cannot_shrink_it():
    posts = [_p(1, RECAP, "X / chat", group="chat", words=100), _p(2, RECAP, "X / chat", group="chat", words=100)]
    (chunk,) = make_chunks(posts, [], {REGULAR: 0.1, RETWEET: 0.1, RECAP: 0.1}, levels={"chat": "low"})
    assert (chunk.id, chunk.level, chunk.target_words) == ("recap-01", "normal", 20)  # 200 words x 10%, no x0.5


def test_a_plain_retweet_of_a_repeated_story_is_briefed_as_a_regular_post():
    posts = [_p(1, RETWEET, text="@a RT@x the story"), _p(2, RETWEET, text="@b RT@y another"), _p(3, REGULAR, text="@c the story too")]
    regular, retweets = make_chunks(posts, [(1, 3)], RATIOS)
    assert [p.n for p in regular.posts] == [1, 3] and regular.tier == REGULAR
    assert [p.n for p in retweets.posts] == [2] and retweets.tier == RETWEET
    assert regular.repeated == ((1, 3),)  # and the story is still flagged as repeated


def test_the_header_names_the_importance_only_when_it_is_not_normal():
    def render(level):
        return Chunk("regular-01", REGULAR, (_p(1),), (), 50, level).render()
    assert "Importance: low\nLength:" in render("low") and "Importance: high\nLength:" in render("high")
    assert "Importance" not in render("normal")
