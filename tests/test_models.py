from xmd.models import FeedItem, is_echo


def _retweet(own: str, original: str) -> FeedItem:
    return FeedItem(
        source="@a", source_type="x", title="t", url="https://x.com/a/status/1",
        full_text=own, retweet_of_author="orig", retweet_of_text=original,
    )


def test_word_count_is_the_visible_words_without_urls():
    post = FeedItem(source="@a", source_type="x", title="t", url="https://x.com/a/status/1",
                    full_text="two words https://t.co/abc &amp; more")
    assert post.word_count == 4  # two, words, &, more


def test_word_count_of_a_bare_retweet_includes_the_retweet_marker():
    assert _retweet("", "the original").word_count == 5  # 🔁 Retweeted @orig: the original


def test_echo_ignores_case_urls_entities_and_whitespace():
    assert is_echo("Same  text &amp; more https://t.co/abc", "same text & more")


def test_echo_of_a_long_original_cut_off_without_a_marker():
    """X cuts a retweet's copy at ~280 characters with no ellipsis when the
    original is longer — the bulk of what stored data actually looks like."""
    original = "word " * 80
    assert is_echo(original[:280], original)


def test_echo_cut_off_with_an_ellipsis():
    assert is_echo("Breaking: the thing happened in the…", "Breaking: the thing happened in the capital today")
    assert is_echo("Breaking: the thing happened in the...", "breaking: the thing happened in the capital today")


def test_a_short_prefix_is_not_an_echo():
    """A real comment that merely starts with the original's words survives."""
    assert not is_echo("lol that's true", "lol")
    assert not is_echo("short", "short and more")


def test_the_original_followed_by_more_words_is_a_real_comment():
    original = "a" * 150
    assert not is_echo(original + " and I agree", original)


def test_empty_sides_are_never_echoes():
    assert not is_echo("", "x" * 30)
    assert not is_echo("x" * 30, "")


def test_own_comment_drops_the_echo_so_the_text_prints_once():
    item = _retweet("original text goes here", "original text goes here")
    assert item.own_comment == ""
    assert item.is_pure_retweet
    assert item.display_body() == "🔁 Retweeted @orig: original text goes here"


def test_own_comment_keeps_real_commentary():
    item = _retweet("my take", "original text")
    assert item.own_comment == "my take"
    assert not item.is_pure_retweet
    assert item.display_body() == "my take\n\n🔁 Retweeted @orig: original text"


def test_a_bare_url_is_not_a_comment():
    """A retweet's own text is often just the link to the retweeted post's
    media: it still shows in the full digest, but adds no words."""
    item = _retweet("https://x.com/orig/status/9/photo/1", "original text")
    assert item.is_pure_retweet
    assert item.display_body() == "https://x.com/orig/status/9/photo/1\n\n🔁 Retweeted @orig: original text"


def test_plain_posts_are_never_retweets():
    item = FeedItem(source="@a", source_type="x", title="t", url="u", text="hi there")
    assert item.own_comment == "hi there"  # falls back to `text` like display_body always did
    assert not item.is_pure_retweet
