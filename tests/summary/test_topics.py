from datetime import datetime, timedelta, timezone

from xmd.core.models import FeedItem
from xmd.summary.topics import Topic, count_names, hot_topics, post_text, topics_by_section, usage_baseline


def _item(source, text, day=0, group="", **kw) -> FeedItem:
    when = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc) - timedelta(days=day)
    return FeedItem(source=source, source_type="x", title=text[:40], url=f"https://x.com/{source}/{abs(hash((text, day)))}",
                    published=when, full_text=text, group=group, **kw)


def _names(*texts):
    """The names found across some posts, each written by its own account."""
    return sorted(count_names([(f"acct{k}", text) for k, text in enumerate(texts)])[0])


def test_names_are_cashtags_allcaps_and_capitalized_words_wherever_they_stand():
    found = _names("Big move: $ACME up, GLOBEX launches with Initech listing. Then Hooli followed",
                   "a big deal today", "such a big week")  # the window writes "big" in lowercase, so "Big" is no name
    assert found == ["acme", "globex", "hooli", "initech"]


def test_a_word_written_in_lowercase_all_over_the_window_is_not_a_name_even_when_capitalized():
    texts = ["a bright day at the council", "sitting in bright light", "Bright and early they left", "Globex opens a market"]
    assert _names(*texts) == ["globex"]  # "Bright" is only a sentence start; "Globex" is never written "globex"
    assert _names("I like BRIGHT, bright days") == ["bright"]  # written in capitals it is the ticker


def test_a_capitalized_word_opening_a_post_counts_when_it_is_a_name():
    assert _names("Globex launches a product", "Globex is live") == ["globex"]  # tweets often start with the name


def test_urls_and_generic_words_are_ignored():
    assert _names("so check https://Example.com/Globex THIS is BREAKING news") == []


def test_a_name_needs_several_different_accounts_not_one_loud_account():
    posts = [("a", "ACME is moving"), ("a", "ACME again"), ("a", "ACME ACME ACME")] * 3 + [("b", "Globex update Globex")]
    assert hot_topics(posts) == []  # ACME is nine posts from one account, Globex one post from one


def test_a_name_needs_enough_posts_and_the_number_of_posts_ranks_topics():
    posts = ([(s, f"$ACME jumps says {s}") for s in "abcde"] * 2 + [(s, f"🚨 Globex is live, {s}") for s in "fgh"]
             + [("i", "acme")])
    assert hot_topics(posts) == [Topic("ACME", 10, 5, 0.0)]  # Globex has three posts, below the six a topic needs
    assert hot_topics(posts, min_posts=3) == [Topic("ACME", 10, 5, 0.0), Topic("Globex", 3, 3, 0.0)]


def test_a_name_that_is_always_loud_is_not_news_but_a_sudden_rise_is():
    posts = [(s, f"markets react as Umbrella said something {s}") for s in "abcdefgh"] * 2 + [(s, f"ACME listing {s}") for s in "abcdefg"]
    assert {t.term for t in hot_topics(posts)} == {"Umbrella", "ACME"}  # with no history, both look like news
    usual = {"umbrella": 18.0, "acme": 1.0}  # sixteen posts against eighteen on an ordinary day; seven against one
    assert [t.term for t in hot_topics(posts, usual)] == ["ACME"]


def test_the_baseline_is_the_mean_accounts_per_day_over_days_with_enough_posts():
    def day(n, name, accounts):
        return [_item(f"acct{k}", f"{name} news {k}", day=n) for k in range(accounts)]

    filler = lambda n: [_item(f"z{k}", f"plain words here {k}", day=n) for k in range(120)]
    history = filler(1) + filler(2) + day(1, "Globex", 2) + day(2, "Globex", 4)
    baseline = usage_baseline(history)
    assert baseline["globex"] == 3.0  # (2 + 4) posts over 2 days
    thin = [_item(f"q{k}", f"Rare thing {k}", day=5) for k in range(20)]  # a day with too few posts does not count
    assert "rare" not in usage_baseline(history + thin) and usage_baseline(thin) == {}


def test_a_retweet_counts_the_post_it_shares_without_the_retweet_marker():
    retweet = _item("r1", "", retweet_of_author="orig", retweet_of_text="ACME just listed")
    assert post_text(retweet) == "ACME just listed"
    assert _names(post_text(retweet)) == ["acme"]


def test_a_name_is_counted_over_the_whole_feed_and_filed_under_the_section_most_of_its_posts_are_in():
    # three accounts, none with three in one section: only the feed as a whole shows ACME
    items = ([_item("a", "$ACME is rising", group="business"), _item("a", "$ACME again", group="business"),
              _item("a", "$ACME still", group="business"), _item("b", "$ACME listing", group="business"),
              _item("c", "$ACME on the news", group="news"), _item("c", "$ACME follow-up", group="news")]
             + [_item("z", "nothing here", group="news")])
    result = topics_by_section(items, min_posts=3)
    assert list(result) == ["X / business"] and result["X / business"][0] == Topic("ACME", 6, 3, 0.0)
    assert topics_by_section(items[:4], min_posts=3) == {}  # two accounts only: not enough


def test_a_name_whose_posts_split_evenly_goes_to_the_section_that_sorts_first():
    items = [_item(s, f"$ACME jumps {s}", group=g) for s, g in (("a", "business"), ("b", "business"), ("c", "news"), ("d", "news"))]
    assert list(topics_by_section(items, min_posts=3)) == ["X / business"]
