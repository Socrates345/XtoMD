from datetime import datetime, timezone

from xmd.core.config import Config, Source
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


def _tweet(n: int, title: str, root: str = "") -> FeedItem:
    """@carol's tweet `n`, posted at minute `n`; `root` = the first tweet of the thread it continues."""
    return FeedItem(
        source="@carol", source_type="x", title=title, author="carol", url=f"https://x.com/carol/status/{n}",
        published=datetime(2026, 10, 15, 6, n, tzinfo=timezone.utc), thread_root=root,
    )


def _nothing_stored(ids: set[str]) -> set[str]:
    return set()


def test_a_thread_keeps_only_its_first_tweet(caplog):
    items = [  # newest first, as the adapters hand them over
        _tweet(3, "R to @carol: 3/ and that's it", root="1"),
        _tweet(2, "R to @carol: 2/ more", root="1"),
        _tweet(1, "1/ a thread"),
    ]
    with caplog.at_level("INFO", logger="xmd"):
        kept = apply_filters(items, _cfg(), known=_nothing_stored)
    assert [i.title for i in kept] == ["1/ a thread"]
    assert "filtered out 2 of 3 tweet(s): 2 thread replies" in caplog.text


def test_a_thread_whose_first_tweet_is_hidden_goes_whole():
    items = [_tweet(2, "R to @carol: details", root="1"), _tweet(1, "a giveaway thread")]
    assert apply_filters(items, _cfg(block_keywords=["giveaway"])) == []


def test_a_thread_reply_goes_when_its_first_tweet_came_in_an_earlier_fetch():
    asked = []

    def stored(ids: set[str]) -> set[str]:
        asked.append(ids)
        return ids & {"1"}

    items = [_tweet(3, "R to @carol: 3/", root="1"), _tweet(2, "R to @carol: 2/", root="1"), _tweet(9, "new post")]
    kept = apply_filters(items, _cfg(), known=stored)
    assert [i.title for i in kept] == ["new post"]
    assert asked == [{"1"}]


def test_the_oldest_reply_stands_in_for_a_first_tweet_never_seen():
    """A late update to an old tweet that was never fetched still shows, once."""
    items = [_tweet(5, "R to @carol: correction", root="1"), _tweet(4, "R to @carol: UPDATE: it shipped", root="1")]
    kept = apply_filters(items, _cfg(), known=_nothing_stored)
    assert [i.title for i in kept] == ["R to @carol: UPDATE: it shipped"]


def test_thread_replies_go_even_when_replies_are_kept():
    items = [_tweet(2, "R to @carol: 2/", root="1"), _tweet(1, "1/ a thread"), _tweet(3, "R to @dan: hi")]
    kept = apply_filters(items, _cfg(drop_replies=False))
    assert [i.title for i in kept] == ["1/ a thread", "R to @dan: hi"]


def test_keyword_blocklist_case_insensitive():
    items = [
        _item("HUGE GIVEAWAY inside!!"),
        _item("Serious analysis of giveaways in economics"),
        _item("normal tweet"),
    ]
    cfg = _cfg(block_keywords=["giveaway"])
    kept = apply_filters(items, cfg)
    # a keyword is the start of a word, not the whole of it: both giveaway titles go
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


def test_a_keyword_counts_from_the_start_of_a_word_only():
    items = [_item("we made it"), _item("three ads today"), _item("sponsored #ad"), _item("AD: buy now")]
    kept = apply_filters(items, _cfg(block_keywords=["ad"]))
    assert [i.title for i in kept] == ["we made it"]  # "made" holds "ad", but no word starts with it


def test_a_keyword_in_a_script_written_without_spaces_is_found_anywhere():
    assert apply_filters([_item("领取空投奖励"), _item("领取airdrop奖励")], _cfg(block_keywords=["空投", "airdrop"])) == []


def _from(handle: str, title: str, **kw) -> FeedItem:
    return FeedItem(source=f"@{handle}", source_type="x", title=title, url=f"https://x.com/{handle}/{abs(hash(title))}", **kw)


def _following(**options) -> Config:
    """@carol with these options of her own, and @dave with none."""
    return Config(sources=[
        Source(name="@carol", type="twitterapi", handle="carol", **options),
        Source(name="@dave", type="twitterapi", handle="dave"),
    ])


def test_reposts_off_drops_that_source_s_plain_retweets_and_nothing_else():
    quote = _from("carol", "my take on this", retweet_of_author="orig", retweet_of_text="the post she quotes")
    items = [
        _from("carol", "RT @orig: something reshared"),
        quote,  # her own words on someone's post: the adapters give it no RT title
        _from("carol", "her own post"),
        _from("dave", "RT @orig: reshared by someone else"),
    ]
    kept = apply_filters(items, _following(reposts=False))
    assert [i.title for i in kept] == ["my take on this", "her own post", "RT @orig: reshared by someone else"]


def test_hide_drops_that_source_s_tweets_with_the_keyword():
    items = [_from("carol", "new airdrop live"), _from("carol", "market notes"), _from("dave", "airdrop season")]
    kept = apply_filters(items, _following(hide=("airdrop",)))
    assert [i.title for i in kept] == ["market notes", "airdrop season"]


def test_only_keeps_that_source_s_tweets_with_one_of_the_keywords():
    items = [
        _from("carol", "new AIRDROP live"),
        _from("carol", "market notes"),
        _from("carol", "points program update"),
        _from("carol", "look at this", retweet_of_author="orig", retweet_of_text="an airdrop was announced"),
        _from("dave", "market notes from dave"),
    ]
    kept = apply_filters(items, _following(only=("airdrop", "points")))
    assert [i.title for i in kept] == ["new AIRDROP live", "points program update", "look at this", "market notes from dave"]


def test_only_and_hide_together_keep_what_matches_the_first_and_none_of_the_second():
    items = [_from("carol", "airdrop is live"), _from("carol", "fake airdrop scam warning"), _from("carol", "hello")]
    kept = apply_filters(items, _following(only=("airdrop",), hide=("scam",)))
    assert [i.title for i in kept] == ["airdrop is live"]


def test_a_fetch_says_how_many_tweets_were_filtered_out_and_why(caplog):
    items = [
        _from("carol", "RT @orig: reshared"),
        _from("carol", "a giveaway"),
        _from("carol", "nothing she is followed for"),
        _from("carol", "airdrop one"),
        _from("dave", "R to @stranger: a reply"),
        _from("dave", "same words", author="dave", full_text="same words"),
        FeedItem(source="@dave", source_type="x", title="same words", author="dave", full_text="same words",
                 url="https://x.com/dave/again"),
    ]
    with caplog.at_level("INFO", logger="xmd"):
        kept = apply_filters(items, _following(reposts=False, hide=("giveaway",), only=("airdrop", "giveaway")))
    assert [i.title for i in kept] == ["airdrop one", "same words"]
    assert ("filtered out 5 of 7 tweet(s): 1 retweets, 1 replies, 1 hidden keywords, 1 outside only:, 1 duplicates"
            in caplog.text)


def test_a_fetch_that_filters_nothing_out_says_nothing(caplog):
    with caplog.at_level("INFO", logger="xmd"):
        apply_filters([_from("dave", "a post")], _following())
    assert "filtered out" not in caplog.text


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


def _post(n: int, text: str, images=(), author: str = "ondo") -> FeedItem:
    """`author`'s tweet `n`: `text` and the pictures `images`."""
    return FeedItem(source=f"@{author}", source_type="x", title=text[:200], author=author, full_text=text,
                    url=f"https://x.com/{author}/status/{n}", images=list(images))


def _pictures(**digests):
    """An image_digest that knows the pictures named (`a` is https://pbs.twimg.com/media/a.png) and counts its
    calls; any other picture can't be had."""
    calls = []

    def digest(url: str):
        calls.append(url)
        return digests.get(url.rsplit("/", 1)[-1].split(".")[0])

    return digest, calls


def _url(name: str) -> str:
    return f"https://pbs.twimg.com/media/{name}.png"


def test_a_post_the_account_already_posted_in_an_earlier_fetch_is_a_repeat():
    stored = [_post(1, "Daily reminder: not financial advice")]
    items = [_post(2, "Daily reminder: not financial advice"), _post(3, "Something new")]
    kept = apply_filters(items, _cfg(), earlier=lambda: stored)
    assert [i.url for i in kept] == ["https://x.com/ondo/status/3"]


def test_a_stored_tweet_fetched_again_is_not_its_own_repeat():
    stored = [_post(1, "the same tweet")]
    assert len(apply_filters([_post(1, "the same tweet")], _cfg(), earlier=lambda: stored)) == 1


def test_another_account_posting_the_same_words_is_no_repeat():
    stored = [_post(1, "ACME earnings beat", author="alice")]
    assert len(apply_filters([_post(2, "ACME earnings beat", author="bob")], _cfg(), earlier=lambda: stored)) == 1


def test_the_same_picture_posted_again_with_no_words_is_a_repeat():
    """The disclaimer image an account posts every day: a new url each time, the same bytes."""
    digest, _ = _pictures(old="d1", new="d1", other="d2")
    stored = [_post(1, "https://x.com/ondo/status/1/photo/1", [_url("old")])]
    items = [_post(2, "https://x.com/ondo/status/2/photo/1", [_url("new")]),
             _post(3, "https://x.com/ondo/status/3/photo/1", [_url("other")])]
    kept = apply_filters(items, _cfg(), earlier=lambda: stored, image_digest=digest)
    assert [i.url for i in kept] == ["https://x.com/ondo/status/3"]


def test_the_same_words_with_another_picture_are_another_post():
    digest, _ = _pictures(monday="d1", tuesday="d2")
    stored = [_post(1, "Chart of the day", [_url("monday")])]
    items = [_post(2, "Chart of the day", [_url("tuesday")])]
    assert len(apply_filters(items, _cfg(), earlier=lambda: stored, image_digest=digest)) == 1


def test_a_picture_that_cant_be_had_keeps_its_post():
    digest, _ = _pictures(old="d1")  # the new one's download fails
    stored = [_post(1, "", [_url("old")])]
    assert len(apply_filters([_post(2, "", [_url("new")])], _cfg(), earlier=lambda: stored, image_digest=digest)) == 1


def test_without_picture_fingerprints_a_post_with_pictures_is_never_a_repeat():
    stored = [_post(1, "Chart of the day", [_url("monday")])]
    assert len(apply_filters([_post(2, "Chart of the day", [_url("tuesday")])], _cfg(), earlier=lambda: stored)) == 1


def test_pictures_are_fetched_only_when_the_words_match():
    digest, calls = _pictures(a="d1", b="d1")
    stored = [_post(1, "yesterday's words", [_url("a")])]
    apply_filters([_post(2, "today's words", [_url("b")])], _cfg(), earlier=lambda: stored, image_digest=digest)
    assert calls == []


def test_words_with_a_picture_and_words_alone_are_different_posts():
    stored = [_post(1, "Big news", [_url("a")])]
    assert len(apply_filters([_post(2, "Big news")], _cfg(), earlier=lambda: stored)) == 1


def test_links_to_other_sites_tell_two_posts_apart_and_links_to_x_do_not():
    stored = [_post(1, "New article 👇 https://blog.example/one https://x.com/ondo/status/1/photo/1"),
              _post(2, "Read this https://t.co/aaa")]
    items = [_post(3, "New article 👇 https://blog.example/two"),  # another article
             _post(4, "New article 👇 https://blog.example/one https://x.com/ondo/status/4/photo/1"),  # the same
             _post(5, "Read this https://t.co/bbb")]  # a short link may stand for another page
    kept = apply_filters(items, _cfg(), earlier=lambda: stored)
    assert [i.url for i in kept] == ["https://x.com/ondo/status/3", "https://x.com/ondo/status/5"]


def test_a_repeat_is_counted_apart_from_a_duplicate_in_the_same_fetch(caplog):
    stored = [_post(1, "Daily reminder")]
    items = [_post(2, "Daily reminder"), _post(3, "once"), _post(4, "once")]
    with caplog.at_level("INFO", logger="xmd"):
        kept = apply_filters(items, _cfg(), earlier=lambda: stored)
    assert [i.url for i in kept] == ["https://x.com/ondo/status/3"]
    assert "filtered out 2 of 3 tweet(s): 1 duplicates, 1 repeats" in caplog.text


def test_the_small_rendition_of_an_x_picture():
    from xmd.ingest.images import small

    assert small("https://pbs.twimg.com/media/a.png") == "https://pbs.twimg.com/media/a.png?name=small"
    assert small("https://pbs.twimg.com/media/a?format=jpg&name=large") == "https://pbs.twimg.com/media/a?format=jpg&name=small"
    assert small("https://img.example/a.jpg") == "https://img.example/a.jpg"


def test_a_picture_that_cant_be_downloaded_has_no_fingerprint():
    import httpx

    from xmd.ingest.images import digest

    def answer(request):
        return httpx.Response(200, content=b"png bytes") if "good" in str(request.url) else httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(answer)) as client:
        assert digest("https://pbs.twimg.com/media/good.png", client) == digest("https://img.example/good.png", client)
        assert digest("https://pbs.twimg.com/media/gone.png", client) is None
