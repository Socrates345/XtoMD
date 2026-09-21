import logging
from datetime import datetime, timedelta, timezone

import pytest

from xmd.ingest.adapters import MAX_PAGES, UNLIMITED
from xmd.ingest.adapters import tweetapi as tweetapi_adapter
from xmd.core.config import Source


class FakeJsonResponse:
    def __init__(self, data: dict, status_code: int = 200):
        self._data = data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


def _by_username(user_id: str = "42") -> dict:
    return {"data": {"id": user_id, "username": "someone"}}


def _tweets_page(tweets: list[dict], next_cursor: str = "") -> dict:
    page = {"data": tweets}
    if next_cursor:
        page["pagination"] = {"nextCursor": next_cursor}
    return page


def _fake_calls(monkeypatch, responses: list[dict]):
    """Patches httpx.get to return one FakeJsonResponse per call, in order,
    and records (url, params, headers) for every call made."""
    calls = []
    it = iter(responses)

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params, "headers": headers})
        return FakeJsonResponse(next(it))

    monkeypatch.setattr(tweetapi_adapter.httpx, "get", fake_get)
    return calls


def test_resolves_username_then_fetches_tweets_with_browser_user_agent(monkeypatch):
    calls = _fake_calls(monkeypatch, [
        _by_username("42"),
        _tweets_page([
            {
                "id": "1", "text": "hello world",
                "createdAt": "2026-07-08T06:00:00.000Z",
                "author": {"username": "someone"},
            },
        ]),
    ])

    items = tweetapi_adapter.fetch(
        Source("@someone", "twitterapi", handle="someone"), api_key="k", max_age_hours=0
    )

    assert [c["url"] for c in calls] == [
        f"{tweetapi_adapter.BASE_URL}/user/by-username",
        f"{tweetapi_adapter.BASE_URL}/user/tweets",
    ]
    assert calls[0]["params"] == {"username": "someone"}
    assert calls[1]["params"] == {"userId": "42"}
    for c in calls:
        assert c["headers"]["X-API-Key"] == "k"
        assert "Mozilla" in c["headers"]["User-Agent"]  # past Cloudflare's bot fingerprinting

    assert len(items) == 1
    item = items[0]
    assert item.title == "hello world"
    assert item.text == "hello world"
    assert item.url == "https://x.com/someone/status/1"
    assert item.author == "someone"
    assert item.source_type == "x"
    assert item.published == datetime(2026, 7, 8, 6, 0, tzinfo=timezone.utc)
    assert item.retweet_of_author == ""


def test_reply_gets_prefixed_title(monkeypatch):
    _fake_calls(monkeypatch, [
        _by_username(),
        _tweets_page([
            {
                "id": "1", "text": "reply text", "createdAt": "2026-07-08T06:00:00.000Z",
                "author": {"username": "someone"},
                "replyTo": {"author": {"username": "other"}},
            },
        ]),
    ])

    items = tweetapi_adapter.fetch(
        Source("@someone", "twitterapi", handle="someone"), api_key="k", max_age_hours=0
    )
    assert items[0].title == "R to @other: reply text"


def test_retweet_keeps_retweeter_comment_and_original_separately(monkeypatch):
    _fake_calls(monkeypatch, [
        _by_username(),
        _tweets_page([
            {
                "id": "1", "text": "", "createdAt": "2026-07-08T06:00:00.000Z",
                "author": {"username": "someone"},
                "retweetedTweet": {"text": "original content", "author": {"username": "orig"}},
            },
        ]),
    ])

    items = tweetapi_adapter.fetch(
        Source("@someone", "twitterapi", handle="someone"), api_key="k", max_age_hours=0
    )
    item = items[0]
    assert item.title == "RT @orig: original content"
    assert item.retweet_of_author == "orig"
    assert item.retweet_of_text == "original content"
    assert item.text == ""  # bare retweet, no added commentary


def test_bare_retweet_echo_text_is_not_treated_as_added_comment(monkeypatch):
    """Defensive: even though tweetapi.com's bare retweets are observed to
    leave `text` empty (see test above), guard against the same RT-echo
    quirk seen on twitterapi.io in case a live response ever carries it."""
    _fake_calls(monkeypatch, [
        _by_username(),
        _tweets_page([
            {
                "id": "1", "text": "RT @orig: original content", "createdAt": "2026-07-08T06:00:00.000Z",
                "author": {"username": "someone"},
                "retweetedTweet": {"text": "original content", "author": {"username": "orig"}},
            },
        ]),
    ])

    items = tweetapi_adapter.fetch(
        Source("@someone", "twitterapi", handle="someone"), api_key="k", max_age_hours=0
    )
    item = items[0]
    assert item.retweet_of_author == "orig"
    assert item.text == ""  # RT-echo text, not real commentary


def test_retweet_text_that_copies_the_original_is_not_added_comment(monkeypatch):
    """Stored data shows retweets whose own text is the original's text with
    no "RT @" prefix; keeping it printed every such retweet twice."""
    _fake_calls(monkeypatch, [
        _by_username(),
        _tweets_page([
            {
                "id": "1", "text": "original content", "createdAt": "2026-07-08T06:00:00.000Z",
                "author": {"username": "someone"},
                "retweetedTweet": {"text": "original content", "author": {"username": "orig"}},
            },
        ]),
    ])

    (item,) = tweetapi_adapter.fetch(
        Source("@someone", "twitterapi", handle="someone"), api_key="k", max_age_hours=0
    )
    assert item.text == "" and item.full_text == ""
    assert item.display_body() == "🔁 Retweeted @orig: original content"


def test_quote_tweet_keeps_commentary_and_original_separately(monkeypatch):
    _fake_calls(monkeypatch, [
        _by_username(),
        _tweets_page([
            {
                "id": "1", "text": "this is huge", "createdAt": "2026-07-08T06:00:00.000Z",
                "author": {"username": "someone"},
                "quotedTweet": {"text": "original content", "author": {"username": "orig"}},
            },
        ]),
    ])

    items = tweetapi_adapter.fetch(
        Source("@someone", "twitterapi", handle="someone"), api_key="k", max_age_hours=0
    )
    item = items[0]
    assert item.retweet_of_author == "orig"
    assert item.retweet_of_text == "original content"
    assert item.text == "this is huge"  # quote-tweet commentary preserved
    assert item.display_body() == "this is huge\n\n🔁 Retweeted @orig: original content"


def test_pagination_follows_next_cursor(monkeypatch):
    calls = _fake_calls(monkeypatch, [
        _by_username(),
        _tweets_page(
            [{"id": str(n), "text": f"tweet {n}", "createdAt": "2026-07-08T06:00:00.000Z",
              "author": {"username": "someone"}} for n in range(20)],
            next_cursor="page2",
        ),
        _tweets_page(
            [{"id": "20", "text": "tweet 20", "createdAt": "2026-07-08T05:00:00.000Z",
              "author": {"username": "someone"}}],
        ),
    ])

    items = tweetapi_adapter.fetch(
        Source("@someone", "twitterapi", handle="someone"),
        api_key="k", max_items=21, max_age_hours=0,
    )

    assert len(items) == 21
    assert calls[1]["params"] == {"userId": "42"}
    assert calls[2]["params"] == {"userId": "42", "cursor": "page2"}


def _page(first: int, count: int = 20, created: str = "", next_cursor: str = "") -> dict:
    """A page of `count` tweets numbered from `first`, posted an hour ago unless `created` says otherwise."""
    when = created or (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return _tweets_page(
        [{"id": str(n), "text": f"tweet {n}", "createdAt": when, "author": {"username": "someone"}}
         for n in range(first, first + count)],
        next_cursor,
    )


def test_with_no_item_ceiling_every_page_inside_the_window_is_fetched(monkeypatch):
    """`max_items` is UNLIMITED when fetch.max_per_source is 0 on this backend. The 100 the fetcher used to stop at cut
    busy handles off with most of their window never fetched."""
    pages = [_page(k * 20, next_cursor=f"p{k + 1}") for k in range(6)] + [_page(120)]  # 7 pages, 140 tweets
    calls = _fake_calls(monkeypatch, [_by_username(), *pages])

    items = tweetapi_adapter.fetch(
        Source("@someone", "twitterapi", handle="someone"), api_key="k", max_items=UNLIMITED, max_age_hours=48
    )

    assert len(items) == 140
    assert len(calls) == 1 + 7  # one id lookup, seven pages


def test_the_page_guard_stops_a_handle_whose_window_never_ends(monkeypatch, caplog):
    """If `createdAt` stops parsing (a schema change, and this backend's fields were inferred from one sample) the age
    cutoff never fires: unlimited must not mean paging through a handle's whole history."""
    pages = [_page(k * 20, created="not a date", next_cursor=f"p{k + 1}") for k in range(MAX_PAGES + 5)]
    calls = _fake_calls(monkeypatch, [_by_username(), *pages])

    with caplog.at_level(logging.WARNING, logger="xmd"):
        items = tweetapi_adapter.fetch(
            Source("@someone", "twitterapi", handle="someone"), api_key="k", max_items=UNLIMITED, max_age_hours=48
        )

    assert len(calls) == 1 + MAX_PAGES
    assert len(items) == 20 * MAX_PAGES
    assert "@someone" in caplog.text and "page guard" in caplog.text


def test_the_page_guard_never_cuts_below_what_an_explicit_item_ceiling_needs(monkeypatch):
    pages = [_page(k * 20, next_cursor=f"p{k + 1}") for k in range(29)] + [_page(580)]  # 30 pages, 600 tweets
    calls = _fake_calls(monkeypatch, [_by_username(), *pages])

    items = tweetapi_adapter.fetch(
        Source("@someone", "twitterapi", handle="someone"), api_key="k", max_items=600, max_age_hours=48
    )

    assert len(items) == 600 and len(calls) == 1 + 30  # 30 pages is more than MAX_PAGES, and allowed


def test_max_age_hours_cutoff_stops_pagination(monkeypatch):
    fresh = datetime.now(timezone.utc) - timedelta(hours=1)
    old = datetime.now(timezone.utc) - timedelta(hours=100)
    fmt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    _fake_calls(monkeypatch, [
        _by_username(),
        _tweets_page([
            {"id": "1", "text": "fresh tweet", "createdAt": fmt(fresh),
             "author": {"username": "someone"}},
            {"id": "2", "text": "old tweet", "createdAt": fmt(old),
             "author": {"username": "someone"}},
        ], next_cursor="page2"),
    ])

    items = tweetapi_adapter.fetch(
        Source("@someone", "twitterapi", handle="someone"), api_key="k", max_age_hours=48
    )
    assert [i.title for i in items] == ["fresh tweet"]


def test_falls_back_to_newest_when_everything_is_stale(monkeypatch):
    old = datetime.now(timezone.utc) - timedelta(hours=100)
    fmt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    _fake_calls(monkeypatch, [
        _by_username(),
        _tweets_page([
            {"id": "1", "text": "stale tweet", "createdAt": fmt(old),
             "author": {"username": "someone"}},
        ]),
    ])

    items = tweetapi_adapter.fetch(
        Source("@someone", "twitterapi", handle="someone"), api_key="k", max_age_hours=48
    )
    assert [i.title for i in items] == ["stale tweet"]  # kept anyway: fallback_latest default


def test_missing_api_key_raises():
    with pytest.raises(ValueError, match="XMD_TWEETAPI_KEY"):
        tweetapi_adapter.fetch(Source("@someone", "twitterapi", handle="someone"), api_key="")


def test_retries_on_429(monkeypatch):
    calls = []
    responses = iter([
        FakeJsonResponse(_by_username(), status_code=429),
        FakeJsonResponse(_by_username()),
        FakeJsonResponse(_tweets_page([
            {"id": "1", "text": "made it through", "createdAt": "2026-07-08T06:00:00.000Z",
             "author": {"username": "someone"}},
        ])),
    ])

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        return next(responses)

    monkeypatch.setattr(tweetapi_adapter.httpx, "get", fake_get)
    monkeypatch.setattr(tweetapi_adapter.time, "sleep", lambda *_: None)

    items = tweetapi_adapter.fetch(
        Source("@someone", "twitterapi", handle="someone"), api_key="k", max_age_hours=0
    )
    assert [i.title for i in items] == ["made it through"]
    assert len(calls) == 3  # one retried by-username call + one tweets call


def test_retries_on_403(monkeypatch):
    """A 403 on /user/by-username has been observed in practice even while
    pacing exactly at the documented per-minute cap — treated as
    retryable, the same as 429, rather than failing the source outright."""
    calls = []
    responses = iter([
        FakeJsonResponse({}, status_code=403),
        FakeJsonResponse(_by_username()),
        FakeJsonResponse(_tweets_page([
            {"id": "1", "text": "made it through", "createdAt": "2026-07-08T06:00:00.000Z",
             "author": {"username": "someone"}},
        ])),
    ])

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        return next(responses)

    monkeypatch.setattr(tweetapi_adapter.httpx, "get", fake_get)
    monkeypatch.setattr(tweetapi_adapter.time, "sleep", lambda *_: None)

    items = tweetapi_adapter.fetch(
        Source("@someone", "twitterapi", handle="someone"), api_key="k", max_age_hours=0
    )
    assert [i.title for i in items] == ["made it through"]
    assert len(calls) == 3  # one retried by-username call + one tweets call


def test_gives_up_after_max_retries_on_403(monkeypatch):
    monkeypatch.setattr(
        tweetapi_adapter.httpx, "get",
        lambda *a, **kw: FakeJsonResponse({}, status_code=403),
    )
    monkeypatch.setattr(tweetapi_adapter.time, "sleep", lambda *_: None)

    with pytest.raises(RuntimeError, match="403"):
        tweetapi_adapter.fetch(Source("@someone", "twitterapi", handle="someone"), api_key="k")


def test_rate_limiter_paces_calls_with_safety_margin(monkeypatch):
    """_RateLimiter spaces calls 60/per_minute * RATE_LIMIT_SAFETY_MARGIN
    seconds apart — the first call goes through immediately, each one
    after waits for its slot."""
    clock = [0.0]
    monkeypatch.setattr(tweetapi_adapter.time, "monotonic", lambda: clock[0])

    slept = []

    def fake_sleep(seconds):
        slept.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(tweetapi_adapter.time, "sleep", fake_sleep)

    limiter = tweetapi_adapter._RateLimiter()
    limiter.configure(60)  # nominal 1 request/second
    limiter.wait()
    limiter.wait()
    limiter.wait()

    expected = 60.0 / 60 * tweetapi_adapter.RATE_LIMIT_SAFETY_MARGIN
    assert slept == [expected, expected]


def test_rate_limiter_disabled_at_zero(monkeypatch):
    monkeypatch.setattr(
        tweetapi_adapter.time, "sleep",
        lambda s: (_ for _ in ()).throw(AssertionError("should never sleep when disabled")),
    )
    limiter = tweetapi_adapter._RateLimiter()
    limiter.configure(0)
    for _ in range(5):
        limiter.wait()  # must return immediately every time


def test_fetch_paces_every_http_call(monkeypatch):
    """fetch() configures the shared limiter from rate_limit_per_minute and
    _get() waits on it before every call — resolve-id plus each tweets
    page, not just the first request of a fetch."""
    configured = []
    monkeypatch.setattr(tweetapi_adapter._rate_limiter, "configure", configured.append)
    waits = []
    monkeypatch.setattr(tweetapi_adapter._rate_limiter, "wait", lambda: waits.append(1))

    _fake_calls(monkeypatch, [
        _by_username(),
        _tweets_page([
            {"id": "1", "text": "hello from tweetapi.com", "createdAt": "2026-07-08T06:00:00.000Z",
             "author": {"username": "someone"}},
        ]),
    ])

    items = tweetapi_adapter.fetch(
        Source("@someone", "twitterapi", handle="someone"), api_key="k",
        max_age_hours=0, rate_limit_per_minute=10,
    )

    assert configured == [10]
    assert len(waits) == 2  # resolve_user_id + the one tweets page
    assert [i.title for i in items] == ["hello from tweetapi.com"]
