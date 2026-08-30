from datetime import datetime, timedelta, timezone

import pytest

from xmd.adapters import tweetapi as tweetapi_adapter
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
