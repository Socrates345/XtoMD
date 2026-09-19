from xmd.models import FeedItem
from xmd.tiers import MEDIA, PRIORITY, RECAP, REGULAR, RETWEET, tier_of

PRIORITY_SOURCES = frozenset({"@p"})
RECAP_GROUPS = frozenset({"chat"})


def _item(source="@a", group="", images=None, retweet_of="", own="hello there", original="an original post"):
    return FeedItem(
        source=source, source_type="x", title="t", url=f"https://x.com/{source}/1",
        full_text=own, group=group, images=images or [],
        retweet_of_author=retweet_of, retweet_of_text=original if retweet_of else "",
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
