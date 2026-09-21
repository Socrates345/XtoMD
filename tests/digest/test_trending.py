from datetime import datetime, timezone

from xmd.core.models import FeedItem
from xmd.digest.trending import find_stories

NEWS = "the central bank surprised markets with an emergency rate cut on friday morning"


def _post(n: int, source: str, text: str, **kw) -> FeedItem:
    return FeedItem(
        source=source, source_type="x", title=text[:60], url=f"https://x.com/{source.lstrip('@')}/status/{n}",
        published=datetime(2026, 7, 8, 6, n, tzinfo=timezone.utc), full_text=text, **kw,
    )


def test_the_same_story_from_two_sources_is_one_story():
    stories = find_stories([_post(1, "@a", NEWS), _post(2, "@b", NEWS + " sources say")])
    assert len(stories) == 1
    assert stories[0].sources == ["@a", "@b"]
    assert stories[0].text == NEWS  # the earliest post leads


def test_unrelated_posts_are_not_a_story():
    other = "completely different subject about football transfers and stadium renovation"
    assert find_stories([_post(1, "@a", NEWS), _post(2, "@b", other)]) == []


def test_a_source_repeating_itself_is_not_a_story():
    assert find_stories([_post(1, "@a", NEWS), _post(2, "@a", NEWS)]) == []


def test_similarity_threshold_is_adjustable():
    paraphrase = "emergency rate cut surprised markets says the central bank on friday"
    posts = [_post(1, "@a", NEWS), _post(2, "@b", paraphrase)]
    assert len(find_stories(posts, similarity=0.4)) == 1
    assert find_stories(posts, similarity=0.9) == []


def test_retweets_join_the_original_they_amplify():
    original = _post(1, "@a", NEWS)
    amplifier = _post(2, "@b", "", retweet_of_author="a", retweet_of_text=NEWS)
    unrelated_comment = _post(3, "@c", "wow", retweet_of_author="a", retweet_of_text=NEWS)
    (story,) = find_stories([original, amplifier, unrelated_comment])
    assert story.sources == ["@a", "@b", "@c"]  # a quote's own words don't matter, the story does


def test_a_loose_match_cannot_chain_unrelated_posts_together():
    """B resembles A and C resembles B, but C shares almost nothing with A —
    it must not be pulled into A's story through B."""
    a = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
    b = "echo foxtrot golf hotel india juliet kilo lima mike november"
    c = "india juliet kilo lima mike november oscar papa quebec romeo"
    stories = find_stories([_post(1, "@a", a), _post(2, "@b", b), _post(3, "@c", c)], similarity=0.4)
    assert [s.sources for s in stories] == [["@a", "@b"]]


def test_posts_too_short_to_match_on_are_ignored():
    assert find_stories([_post(1, "@a", "gm frens"), _post(2, "@b", "gm frens")]) == []


def test_most_widely_repeated_story_comes_first():
    other = "quarterly earnings beat expectations as cloud revenue climbed sharply across regions"
    posts = [
        _post(1, "@a", other), _post(2, "@b", other),
        _post(3, "@a", NEWS), _post(4, "@b", NEWS), _post(5, "@c", NEWS),
    ]
    assert [len(s.sources) for s in find_stories(posts)] == [3, 2]


def test_first_by_source_is_each_sources_earliest_post():
    posts = [_post(1, "@a", NEWS), _post(2, "@b", NEWS), _post(3, "@b", NEWS), _post(4, "@a", NEWS)]
    (story,) = find_stories(posts)
    assert [i.url.rsplit("/", 1)[1] for i in story.first_by_source] == ["1", "2"]


def test_undated_items_are_handled():
    posts = [
        FeedItem(source="@a", source_type="x", title="t", url="https://x.com/a/1", full_text=NEWS),
        _post(2, "@b", NEWS),
    ]
    (story,) = find_stories(posts)
    assert story.sources == ["@b", "@a"]  # dated first, undated last
