import itertools
from datetime import datetime, timedelta, timezone

from xmd.core.models import FeedItem
from xmd.core.tiers import MEDIA, PRIORITY, RECAP, REGULAR, RETWEET, SILENCE_DAYS, mark_after_silence, tier_of

PRIORITY_SOURCES = frozenset({"@p"})
RECAP_GROUPS = frozenset({"chat"})


def _item(source="@a", group="", images=None, retweet_of="", own="hello there", original="an original post",
          after_silence=False):
    return FeedItem(
        source=source, source_type="x", title="t", url=f"https://x.com/{source}/1",
        full_text=own, group=group, images=images or [],
        retweet_of_author=retweet_of, retweet_of_text=original if retweet_of else "", after_silence=after_silence,
    )


def _tier(item):
    return tier_of(item, PRIORITY_SOURCES, RECAP_GROUPS)


def test_first_matching_tier_wins():
    everything = _item("@p", group="chat", images=["i.jpg"], retweet_of="x", own="")
    assert _tier(everything) == PRIORITY
    assert _tier(_item(group="chat", images=["i.jpg"], retweet_of="x", own="")) == RECAP
    assert _tier(_item(images=["i.jpg"], retweet_of="x", own="")) == MEDIA
    assert _tier(_item(retweet_of="x", own="")) == RETWEET
    assert _tier(_item()) == REGULAR


def test_a_quote_tweet_with_commentary_is_a_regular_post():
    assert _tier(_item(retweet_of="x", own="my take")) == REGULAR


def test_a_retweet_whose_own_text_is_a_copy_is_a_plain_retweet():
    assert _tier(_item(retweet_of="x", own="an original post")) == RETWEET


def test_ungrouped_items_are_never_recap():
    assert _tier(_item(group="")) == REGULAR


def test_no_priority_or_recap_configured_means_only_the_content_tiers():
    assert tier_of(_item("@p", group="chat")) == REGULAR


def test_a_post_after_silence_is_priority_even_as_a_quote_tweet_but_not_as_a_plain_retweet():
    silent = dict(after_silence=True)
    assert _tier(_item(**silent)) == PRIORITY
    assert _tier(_item(retweet_of="x", own="my take", **silent)) == PRIORITY  # retweet with a comment: counts
    assert _tier(_item(retweet_of="x", own="", **silent)) == RETWEET  # plain retweet: doesn't
    assert _tier(_item(retweet_of="x", own="an original post", **silent)) == RETWEET  # a copy is still plain
    assert _tier(_item(group="chat", images=["i.jpg"], **silent)) == PRIORITY  # ahead of recap and media too


DAY = timedelta(days=1)
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


_numbers = itertools.count(1)


def _at(when, source="@a"):
    return FeedItem(source=source, source_type="x", title="t", url=f"https://x.com/{source}/{next(_numbers)}", published=when)


def _mark(items, times):
    mark_after_silence(items, {k: sorted(v) for k, v in times.items()})
    return [i.after_silence for i in items]


def test_silence_means_more_than_the_limit_since_the_previous_post():
    last = T0
    just_over = _at(last + timedelta(days=SILENCE_DAYS, minutes=1))
    exactly = _at(last + timedelta(days=SILENCE_DAYS))
    under = _at(last + timedelta(days=SILENCE_DAYS - 1))
    assert _mark([just_over], {"@a": [last, just_over.published]}) == [True]
    assert _mark([exactly], {"@a": [last, exactly.published]}) == [False]
    assert _mark([under], {"@a": [last, under.published]}) == [False]


def test_only_the_first_post_back_is_flagged_and_the_one_before_it_is_the_clock():
    back, next_day = _at(T0 + 30 * DAY), _at(T0 + 31 * DAY)
    times = {"@a": [T0, back.published, next_day.published]}
    assert _mark([back, next_day], times) == [True, False]


def test_any_post_resets_the_clock_a_plain_retweet_included():
    retweet = FeedItem(source="@a", source_type="x", title="t", url="https://x.com/a/rt", published=T0 + 20 * DAY,
                       retweet_of_author="x", retweet_of_text="orig")
    later = _at(T0 + 30 * DAY)  # 10 days after the retweet, 30 after the last real post
    assert _mark([later], {"@a": [T0, retweet.published, later.published]}) == [False]


def test_a_poster_with_no_earlier_post_is_left_alone():
    first = _at(T0)
    assert _mark([first], {"@a": [first.published]}) == [False]
    assert _mark([first], {}) == [False]  # not in the history at all
    assert _mark([_at(None)], {"@a": [T0]}) == [False]  # an undated post is never judged


def test_each_poster_is_judged_on_their_own_history():
    a, b = _at(T0 + 40 * DAY, "@a"), _at(T0 + 40 * DAY, "@b")
    times = {"@a": [T0, a.published], "@b": [T0 + 39 * DAY, b.published]}
    assert _mark([a, b], times) == [True, False]


def test_the_count_returned_is_how_many_were_flagged():
    a, b = _at(T0 + 40 * DAY, "@a"), _at(T0 + 40 * DAY, "@b")
    times = {"@a": [T0, a.published], "@b": [T0 + 39 * DAY, b.published]}
    assert mark_after_silence([a, b], times) == 1
