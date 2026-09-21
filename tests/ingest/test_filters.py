from xmd.core.config import Config
from xmd.ingest.filters import apply_filters
from xmd.core.models import FeedItem


def _item(title: str) -> FeedItem:
    return FeedItem(
        source="@someone", source_type="x", title=title,
        url=f"https://x.com/s/{abs(hash(title))}",
    )


def _cfg(**kw) -> Config:
    return Config(sources=[], **kw)


def test_retweets_kept_by_default():
    # the brief this tool follows explicitly wants retweets included
    items = [
        _item("RT by @erin: something reshared"),  # legacy prefix form
        _item("RT @user: something reshared"),  # adapter form
        _item("an original tweet"),
    ]
    assert len(apply_filters(items, _cfg())) == 3


def test_retweets_dropped_when_enabled():
    items = [_item("RT by @erin: something reshared"), _item("an original tweet")]
    kept = apply_filters(items, _cfg(drop_retweets=True))
    assert [i.title for i in kept] == ["an original tweet"]


def test_replies_dropped_by_default():
    items = [_item("R to @somebody: my reply"), _item("a real post")]
    assert [i.title for i in apply_filters(items, _cfg())] == ["a real post"]


def test_self_replies_kept_as_threads():
    # a reply to your own account is a thread continuation, not noise
    items = [
        _item("R to @someone: part two of my thread"),  # source is "@someone"
        _item("R to @stranger: arguing in the replies"),
    ]
    kept = apply_filters(items, _cfg())
    assert [i.title for i in kept] == ["R to @someone: part two of my thread"]


def test_replies_toggle_off():
    items = [_item("R to @b: y")]
    assert len(apply_filters(items, _cfg(drop_replies=False))) == 1


def test_keyword_blocklist_case_insensitive():
    items = [
        _item("HUGE GIVEAWAY inside!!"),
        _item("Serious analysis of giveaways in economics"),
        _item("normal tweet"),
    ]
    cfg = _cfg(block_keywords=["giveaway"])
    kept = apply_filters(items, cfg)
    # substring match is deliberate and blunt: both giveaway titles go
    assert [i.title for i in kept] == ["normal tweet"]


def test_a_keyword_deep_in_a_long_tweet_is_still_found():
    """The adapters cut `title` at 200 characters, so a blocklist that read only the title missed the keywords
    in the last part of every long tweet (a third of a real feed)."""
    text = ("word " * 60)[:225] + "GIVEAWAY and some more words after it"
    item = FeedItem(source="@someone", source_type="x", title=text[:200], text=text, full_text=text, url="https://x.com/s/1")
    assert apply_filters([item], _cfg(block_keywords=["giveaway"])) == []
    assert apply_filters([item], _cfg(block_keywords=["something else"])) == [item]


def test_a_keyword_in_the_post_a_tweet_quotes_is_found_too():
    quote = FeedItem(
        source="@someone", source_type="x", title="this is huge", url="https://x.com/s/2", full_text="this is huge",
        retweet_of_author="orig", retweet_of_text="win a free GIVEAWAY today",
    )
    assert apply_filters([quote], _cfg(block_keywords=["giveaway"])) == []


def test_a_keyword_is_not_found_across_the_seam_between_two_parts_of_a_tweet():
    item = FeedItem(
        source="@someone", source_type="x", title="a give", full_text="away for free", url="https://x.com/s/3",
        retweet_of_author="orig", retweet_of_text="nothing",
    )
    assert apply_filters([item], _cfg(block_keywords=["give away"])) == [item]


def test_normal_items_untouched():
    items = [_item("Introducing our new project"), _item("weekly links")]
    assert apply_filters(items, _cfg()) == items


def _authored(title: str, author: str, full_text: str = "") -> FeedItem:
    return FeedItem(
        source=f"@{author}", source_type="x", title=title, author=author,
        url=f"https://x.com/{author}/{abs(hash((author, title, full_text)))}",
        full_text=full_text,
    )


def test_dedup_drops_same_author_identical_text():
    items = [
        _authored("t1", "erin", "Acme went from 4$ to 40$"),
        _authored("t2", "erin", "Acme went from 4$ to 40$"),  # same text, different tweet id
    ]
    kept = apply_filters(items, _cfg())
    assert len(kept) == 1
    assert kept[0].title == "t1"  # first occurrence wins


def test_dedup_keeps_same_author_different_text():
    items = [
        _authored("t1", "trader", "Acme hits $120"),
        _authored("t2", "trader", "Globex hits $2000"),
    ]
    assert len(apply_filters(items, _cfg())) == 2


def test_dedup_keeps_cross_author_repetition():
    # two different people posting the same thing is a trending signal,
    # not noise — must never be treated as a duplicate
    items = [
        _authored("t1", "alice", "ACME earnings beat expectations"),
        _authored("t2", "bob", "ACME earnings beat expectations"),
    ]
    assert len(apply_filters(items, _cfg())) == 2


def test_dedup_keeps_every_post_that_has_no_words_at_all():
    """Three image-only posts of one account have nothing to compare: they are three posts, not one 'duplicate'."""
    items = [
        FeedItem(source="@erin", source_type="x", title="", author="erin", url=f"https://x.com/erin/{n}",
                 images=[f"https://img.example/{n}.jpg"])
        for n in (1, 2, 3)
    ]
    assert len(apply_filters(items, _cfg())) == 3


def test_dedup_falls_back_to_title_when_no_body_text():
    # items with no full_text/text (e.g. a title-only feed) must not all
    # collide on an empty-string key
    items = [_authored("post one", "acct"), _authored("post two", "acct")]
    assert len(apply_filters(items, _cfg())) == 2
