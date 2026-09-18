from datetime import datetime, timedelta, timezone

from xmd.config import Source


class FakeJsonResponse:
    def __init__(self, data: dict, status_code: int = 200):
        self._data = data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


def test_twitterapi_fetch(monkeypatch):
    """Response shape as actually returned by the live API: tweets nest
    under data.data.tweets, not top-level."""
    from xmd.adapters import twitterapi as twitterapi_adapter

    page = {
        "status": "success",
        "has_next_page": False,
        "next_cursor": "",
        "data": {
            "tweets": [
                {
                    "url": "https://x.com/someone/status/1",
                    "text": "plain tweet",
                    "createdAt": "Tue Dec 10 07:00:30 +0000 2024",
                    "isReply": False,
                    "retweeted_tweet": None,
                },
                {
                    "url": "https://x.com/someone/status/2",
                    "text": "reply text",
                    "createdAt": "Tue Dec 10 07:01:30 +0000 2024",
                    "isReply": True,
                    "inReplyToUsername": "other",
                    "retweeted_tweet": None,
                },
                {
                    "url": "https://x.com/someone/status/3",
                    "text": "quote-tweet commentary",
                    "createdAt": "Tue Dec 10 07:02:30 +0000 2024",
                    "isReply": False,
                    "retweeted_tweet": {
                        "text": "original content", "author": {"userName": "orig"}
                    },
                },
            ],
        },
    }
    monkeypatch.setattr(
        twitterapi_adapter.httpx, "get", lambda *a, **kw: FakeJsonResponse(page)
    )

    items = twitterapi_adapter.fetch(
        Source("@someone", "twitterapi", handle="someone"), api_key="k", max_age_hours=0
    )

    assert [i.title for i in items] == [
        "plain tweet",
        "R to @other: reply text",
        "RT @orig: original content",
    ]
    assert items[0].published == datetime(2024, 12, 10, 7, 0, 30, tzinfo=timezone.utc)

    assert items[0].retweet_of_author == "" and items[0].text == "plain tweet"
    assert items[1].retweet_of_author == "" and items[1].text == "R to @other: reply text"

    retweet = items[2]
    assert retweet.retweet_of_author == "orig"
    assert retweet.retweet_of_text == "original content"
    assert retweet.text == "quote-tweet commentary"
    assert retweet.full_text == "quote-tweet commentary"
    assert retweet.display_body() == (
        "quote-tweet commentary\n\n🔁 Retweeted @orig: original content"
    )
    assert items[0].source_type == "x"


def test_bare_retweet_echo_text_is_not_treated_as_added_comment(monkeypatch):
    """twitterapi.io doesn't distinguish a bare retweet from a quote-tweet:
    a bare retweet's own `text` field carries X's auto-generated, truncated
    "RT @author: text…" echo of the original, not real commentary — it must
    not be kept as text/full_text, or the rendered body duplicates the 🔁
    quoted line (see render.py's display_body caller)."""
    from xmd.adapters import twitterapi as twitterapi_adapter

    page = {
        "status": "success",
        "has_next_page": False,
        "next_cursor": "",
        "data": {
            "tweets": [
                {
                    "url": "https://x.com/someone/status/4",
                    "text": "RT @ruthbenghiat: The conversion of the GOP into an openly authoritarian party…",
                    "createdAt": "Tue Dec 10 07:03:30 +0000 2024",
                    "isReply": False,
                    "retweeted_tweet": {
                        "text": "The conversion of the GOP into an openly authoritarian party "
                        "in its domestic and foreign policies is one of the biggest stories.",
                        "author": {"userName": "ruthbenghiat"},
                    },
                },
            ],
        },
    }
    monkeypatch.setattr(
        twitterapi_adapter.httpx, "get", lambda *a, **kw: FakeJsonResponse(page)
    )

    items = twitterapi_adapter.fetch(
        Source("@someone", "twitterapi", handle="someone"), api_key="k", max_age_hours=0
    )
    item = items[0]
    assert item.retweet_of_author == "ruthbenghiat"
    assert item.text == "" and item.full_text == ""
    assert item.display_body() == (
        "🔁 Retweeted @ruthbenghiat: The conversion of the GOP into an openly "
        "authoritarian party in its domestic and foreign policies is one of "
        "the biggest stories."
    )


def test_twitterapi_quote_tweet_includes_quoted_content(monkeypatch):
    """twitterapi.io's schema (unlike its title-prefixing) does distinguish a
    quote tweet from a plain retweet: it nests the quoted post under
    `quoted_tweet`, not `retweeted_tweet`. Without reading that field, a
    quote-tweeted response (including someone quoting a *retweet* to comment
    on it) would render with only the wrapper's own commentary and none of
    the content it's responding to."""
    from xmd.adapters import twitterapi as twitterapi_adapter

    page = {
        "status": "success",
        "has_next_page": False,
        "next_cursor": "",
        "data": {
            "tweets": [
                {
                    "url": "https://x.com/someone/status/5",
                    "text": "wild that this is even a debate",
                    "createdAt": "Tue Dec 10 07:04:30 +0000 2024",
                    "isReply": False,
                    "retweeted_tweet": None,
                    "quoted_tweet": {
                        "text": "original post being quoted",
                        "author": {"userName": "orig"},
                    },
                },
            ],
        },
    }
    monkeypatch.setattr(
        twitterapi_adapter.httpx, "get", lambda *a, **kw: FakeJsonResponse(page)
    )

    items = twitterapi_adapter.fetch(
        Source("@someone", "twitterapi", handle="someone"), api_key="k", max_age_hours=0
    )
    item = items[0]
    assert item.retweet_of_author == "orig"
    assert item.retweet_of_text == "original post being quoted"
    assert item.text == "wild that this is even a debate"
    assert item.display_body() == (
        "wild that this is even a debate\n\n🔁 Retweeted @orig: original post being quoted"
    )
    # quote tweets aren't title-prefixed "RT @…" — they carry genuine
    # commentary and must not be dropped by config.drop_retweets
    assert item.title == "wild that this is even a debate"


def test_twitterapi_fetch_falls_back_to_top_level_tweets(monkeypatch):
    """Defensive fallback in case the shape ever varies by endpoint/version."""
    from xmd.adapters import twitterapi as twitterapi_adapter

    page = {
        "status": "success",
        "has_next_page": False,
        "next_cursor": "",
        "tweets": [
            {
                "url": "https://x.com/someone/status/1",
                "text": "top-level shape",
                "createdAt": "Tue Dec 10 07:00:30 +0000 2024",
                "isReply": False,
                "retweeted_tweet": None,
            },
        ],
    }
    monkeypatch.setattr(
        twitterapi_adapter.httpx, "get", lambda *a, **kw: FakeJsonResponse(page)
    )

    items = twitterapi_adapter.fetch(
        Source("@someone", "twitterapi", handle="someone"), api_key="k", max_age_hours=0
    )
    assert [i.title for i in items] == ["top-level shape"]


def test_twitterapi_retries_on_429(monkeypatch):
    from xmd.adapters import twitterapi as twitterapi_adapter

    ok_page = {
        "status": "success",
        "has_next_page": False,
        "next_cursor": "",
        "data": {
            "tweets": [
                {
                    "url": "https://x.com/someone/status/1",
                    "text": "made it through",
                    "createdAt": "Tue Dec 10 07:00:30 +0000 2024",
                    "isReply": False,
                    "retweeted_tweet": None,
                },
            ],
        },
    }
    responses = iter(
        [
            FakeJsonResponse({}, status_code=429),
            FakeJsonResponse({}, status_code=429),
            FakeJsonResponse(ok_page, status_code=200),
        ]
    )
    monkeypatch.setattr(twitterapi_adapter.httpx, "get", lambda *a, **kw: next(responses))
    slept = []
    monkeypatch.setattr(twitterapi_adapter.time, "sleep", lambda s: slept.append(s))

    items = twitterapi_adapter.fetch(
        Source("@someone", "twitterapi", handle="someone"), api_key="k", max_age_hours=0
    )
    assert [i.title for i in items] == ["made it through"]
    assert slept == [2.0, 4.0]  # backoff doubled between the two 429s


def test_twitterapi_gives_up_after_max_retries(monkeypatch):
    from xmd.adapters import twitterapi as twitterapi_adapter

    monkeypatch.setattr(
        twitterapi_adapter.httpx, "get", lambda *a, **kw: FakeJsonResponse({}, status_code=429)
    )
    monkeypatch.setattr(twitterapi_adapter.time, "sleep", lambda s: None)

    try:
        twitterapi_adapter.fetch(Source("@someone", "twitterapi", handle="someone"), api_key="k")
        assert False, "expected failure"
    except RuntimeError as e:
        assert "429" in str(e)


def test_twitterapi_keeps_partial_results_when_later_page_fails(monkeypatch):
    """Page 1 succeeds and asks for more (has_next_page); page 2 exhausts its
    retries. The items from page 1 must survive, not be thrown away."""
    from xmd.adapters import twitterapi as twitterapi_adapter

    page1 = {
        "status": "success",
        "has_next_page": True,
        "next_cursor": "next",
        "data": {
            "tweets": [
                {
                    "url": "https://x.com/someone/status/1",
                    "text": "from page one",
                    "createdAt": "Tue Dec 10 07:00:30 +0000 2024",
                    "isReply": False,
                    "retweeted_tweet": None,
                },
            ],
        },
    }
    responses = iter([FakeJsonResponse(page1)] + [FakeJsonResponse({}, status_code=429)] * 10)
    monkeypatch.setattr(twitterapi_adapter.httpx, "get", lambda *a, **kw: next(responses))
    monkeypatch.setattr(twitterapi_adapter.time, "sleep", lambda s: None)

    items = twitterapi_adapter.fetch(
        Source("@someone", "twitterapi", handle="someone"), api_key="k", max_items=20,
        max_age_hours=0,
    )
    assert [i.title for i in items] == ["from page one"]


def test_twitterapi_stops_paginating_past_the_age_cutoff(monkeypatch):
    """last_tweets is newest-first: once a tweet older than max_age_hours is
    seen, older tweets on the same page are dropped and no further page is
    requested — this is the API-cost saving."""
    from xmd.adapters import twitterapi as twitterapi_adapter

    now = datetime.now(timezone.utc)
    fresh = now - timedelta(hours=1)
    old = now - timedelta(hours=72)

    def fmt(dt: datetime) -> str:
        return dt.strftime("%a %b %d %H:%M:%S +0000 %Y")

    page1 = {
        "status": "success",
        "has_next_page": True,
        "next_cursor": "next",
        "data": {
            "tweets": [
                {
                    "url": "https://x.com/someone/status/1",
                    "text": "fresh tweet",
                    "createdAt": fmt(fresh),
                    "isReply": False,
                    "retweeted_tweet": None,
                },
                {
                    "url": "https://x.com/someone/status/2",
                    "text": "old tweet",
                    "createdAt": fmt(old),
                    "isReply": False,
                    "retweeted_tweet": None,
                },
            ],
        },
    }
    calls = []

    def fake_get(*a, **kw):
        calls.append(1)
        return FakeJsonResponse(page1)

    monkeypatch.setattr(twitterapi_adapter.httpx, "get", fake_get)

    items = twitterapi_adapter.fetch(
        Source("@someone", "twitterapi", handle="someone"), api_key="k", max_age_hours=48
    )
    assert [i.title for i in items] == ["fresh tweet"]
    assert len(calls) == 1  # page 2 was never requested


def test_twitterapi_falls_back_to_latest_when_all_past_cutoff(monkeypatch):
    """Even the newest tweet is past max_age_hours: fallback_latest keeps it
    anyway instead of returning nothing for this source."""
    from xmd.adapters import twitterapi as twitterapi_adapter

    old = datetime.now(timezone.utc) - timedelta(days=10)

    def fmt(dt: datetime) -> str:
        return dt.strftime("%a %b %d %H:%M:%S +0000 %Y")

    page = {
        "status": "success",
        "has_next_page": False,
        "next_cursor": "",
        "data": {
            "tweets": [
                {
                    "url": "https://x.com/someone/status/1",
                    "text": "stale tweet",
                    "createdAt": fmt(old),
                    "isReply": False,
                    "retweeted_tweet": None,
                },
            ],
        },
    }
    monkeypatch.setattr(
        twitterapi_adapter.httpx, "get", lambda *a, **kw: FakeJsonResponse(page)
    )

    items = twitterapi_adapter.fetch(
        Source("@someone", "twitterapi", handle="someone"), api_key="k", max_age_hours=24
    )
    assert [i.title for i in items] == ["stale tweet"]


def test_twitterapi_fallback_disabled_returns_nothing(monkeypatch):
    from xmd.adapters import twitterapi as twitterapi_adapter

    old = datetime.now(timezone.utc) - timedelta(days=10)

    def fmt(dt: datetime) -> str:
        return dt.strftime("%a %b %d %H:%M:%S +0000 %Y")

    page = {
        "status": "success",
        "has_next_page": False,
        "next_cursor": "",
        "data": {
            "tweets": [
                {
                    "url": "https://x.com/someone/status/1",
                    "text": "stale tweet",
                    "createdAt": fmt(old),
                    "isReply": False,
                    "retweeted_tweet": None,
                },
            ],
        },
    }
    monkeypatch.setattr(
        twitterapi_adapter.httpx, "get", lambda *a, **kw: FakeJsonResponse(page)
    )

    items = twitterapi_adapter.fetch(
        Source("@someone", "twitterapi", handle="someone"), api_key="k",
        max_age_hours=24, fallback_latest=False,
    )
    assert items == []


def test_twitterapi_keeps_items_with_unparseable_date(monkeypatch):
    from xmd.adapters import twitterapi as twitterapi_adapter

    page = {
        "status": "success",
        "has_next_page": False,
        "next_cursor": "",
        "data": {
            "tweets": [
                {
                    "url": "https://x.com/someone/status/1",
                    "text": "no date",
                    "createdAt": "",
                    "isReply": False,
                    "retweeted_tweet": None,
                },
            ],
        },
    }
    monkeypatch.setattr(
        twitterapi_adapter.httpx, "get", lambda *a, **kw: FakeJsonResponse(page)
    )

    items = twitterapi_adapter.fetch(
        Source("@someone", "twitterapi", handle="someone"), api_key="k", max_age_hours=48
    )
    assert [i.title for i in items] == ["no date"]


def test_twitterapi_requires_api_key():
    from xmd.adapters import twitterapi as twitterapi_adapter

    try:
        twitterapi_adapter.fetch(Source("@someone", "twitterapi", handle="someone"), api_key="")
        assert False, "expected failure"
    except ValueError as e:
        assert "api_key" in str(e)
