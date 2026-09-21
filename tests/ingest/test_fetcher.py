import asyncio
import logging
import time

import pytest

from xmd.ingest import fetcher
from xmd.core.config import Config, Source
from xmd.core.models import FeedItem


def _x_source(handle: str = "dave") -> Source:
    return Source(name=f"@{handle}", type="twitterapi", handle=handle, platform="x")


def _fetched(n: int, handle: str = "alice") -> list[FeedItem]:
    """`n` distinct tweets, as an adapter would hand them back."""
    return [
        FeedItem(source=f"@{handle}", source_type="x", title=f"tweet {i}", text=f"tweet {i}", author=handle,
                 url=f"https://x.example/{handle}/status/{i}")
        for i in range(n)
    ]


def _spy_on_max_items(monkeypatch, returns=lambda max_items: []) -> dict:
    """Both backends' fetch(), replaced by one that notes the `max_items` it was asked for."""
    seen = {}

    def fake_fetch(source, api_key, max_items, max_age_hours=48, fallback_latest=True, rate_limit_per_minute=0):
        seen["max_items"] = max_items
        return returns(max_items)

    monkeypatch.setattr(fetcher.twitterapi_adapter, "fetch", fake_fetch)
    monkeypatch.setattr(fetcher.tweetapi_adapter, "fetch", fake_fetch)
    return seen


def test_fetch_all_uses_configured_max_age_hours_by_default(monkeypatch):
    seen = {}

    def fake_fetch(source, api_key, max_items, max_age_hours=48, fallback_latest=True):
        seen["max_age_hours"] = max_age_hours
        return []

    monkeypatch.setattr(fetcher.twitterapi_adapter, "fetch", fake_fetch)
    config = Config(sources=[_x_source()], x_api_key="k", x_max_age_hours=48)
    asyncio.run(fetcher.fetch_all(config))
    assert seen["max_age_hours"] == 48


def test_fetch_all_override_widens_lookback_without_changing_config(monkeypatch):
    seen = {}

    def fake_fetch(source, api_key, max_items, max_age_hours=48, fallback_latest=True):
        seen["max_age_hours"] = max_age_hours
        return []

    monkeypatch.setattr(fetcher.twitterapi_adapter, "fetch", fake_fetch)
    config = Config(sources=[_x_source()], x_api_key="k", x_max_age_hours=48)
    asyncio.run(fetcher.fetch_all(config, x_max_age_hours_override=168))

    assert seen["max_age_hours"] == 168
    assert config.x_max_age_hours == 48  # override is call-scoped, never persisted


def test_fetch_all_routes_to_tweetapi_backend_when_configured(monkeypatch):
    seen = {}

    def fake_tweetapi_fetch(
        source, api_key, max_items, max_age_hours=48, fallback_latest=True,
        rate_limit_per_minute=0,
    ):
        seen["api_key"] = api_key
        seen["rate_limit_per_minute"] = rate_limit_per_minute
        return []

    def boom(*a, **kw):
        raise AssertionError("twitterapi_adapter should not be called when x_backend is tweetapi")

    monkeypatch.setattr(fetcher.tweetapi_adapter, "fetch", fake_tweetapi_fetch)
    monkeypatch.setattr(fetcher.twitterapi_adapter, "fetch", boom)
    config = Config(
        sources=[_x_source()], x_backend="tweetapi", tweetapi_api_key="tw-k", x_api_key="tw-io-k",
        tweetapi_rate_limit_per_minute=42,
    )
    asyncio.run(fetcher.fetch_all(config))
    assert seen["api_key"] == "tw-k"  # the active backend's own key, not twitterapi.io's
    assert seen["rate_limit_per_minute"] == 42


def test_fetch_all_does_not_pass_rate_limit_to_twitterapi_backend(monkeypatch):
    """twitterapi.io's fetch() has no rate_limit_per_minute parameter —
    passing it unconditionally would break the default (twitterapi.io) path."""
    def fake_fetch(source, api_key, max_items, max_age_hours=48, fallback_latest=True):
        return []

    monkeypatch.setattr(fetcher.twitterapi_adapter, "fetch", fake_fetch)
    config = Config(sources=[_x_source()], x_api_key="k")
    asyncio.run(fetcher.fetch_all(config))  # must not raise TypeError


def test_fetch_all_defaults_to_twitterapi_backend(monkeypatch):
    seen = {}

    def fake_fetch(source, api_key, max_items, max_age_hours=48, fallback_latest=True):
        seen["api_key"] = api_key
        return []

    monkeypatch.setattr(fetcher.twitterapi_adapter, "fetch", fake_fetch)
    config = Config(sources=[_x_source()], x_api_key="k")
    asyncio.run(fetcher.fetch_all(config))
    assert seen["api_key"] == "k"


def test_fetch_all_tolerates_a_failing_source(monkeypatch):
    def flaky_fetch(source, api_key, max_items, max_age_hours=48, fallback_latest=True):
        if source.handle == "broken":
            raise RuntimeError("boom")
        return []

    monkeypatch.setattr(fetcher.twitterapi_adapter, "fetch", flaky_fetch)
    config = Config(sources=[_x_source("broken"), _x_source("fine")], x_api_key="k")
    result = asyncio.run(fetcher.fetch_all(config))  # must not raise
    assert result.items == []


def test_fetch_all_names_the_failed_sources_in_config_order(monkeypatch):
    def flaky_fetch(source, api_key, max_items, max_age_hours=48, fallback_latest=True):
        if source.handle == "carol":
            time.sleep(0.05)  # finishes after bob's failure, yet is listed first
        if source.handle in ("carol", "bob"):
            raise RuntimeError(f"could not resolve @{source.handle} to a user id")
        return []  # alice answered with nothing to say: not a failure

    monkeypatch.setattr(fetcher.twitterapi_adapter, "fetch", flaky_fetch)
    config = Config(sources=[_x_source("carol"), _x_source("alice"), _x_source("bob")], x_api_key="k")
    result = asyncio.run(fetcher.fetch_all(config))
    assert [(s.handle, why) for s, why in result.failed] == [
        ("carol", "could not resolve @carol to a user id"),
        ("bob", "could not resolve @bob to a user id"),
    ]


def test_fetch_all_reports_nothing_failed_when_every_source_answers(monkeypatch):
    def fake_fetch(source, api_key, max_items, max_age_hours=48, fallback_latest=True):
        return []

    monkeypatch.setattr(fetcher.twitterapi_adapter, "fetch", fake_fetch)
    config = Config(sources=[_x_source("alice")], x_api_key="k")
    assert asyncio.run(fetcher.fetch_all(config)).failed == []


def test_fetch_all_failure_reason_is_one_non_empty_line(monkeypatch):
    def flaky_fetch(source, api_key, max_items, max_age_hours=48, fallback_latest=True):
        if source.handle == "alice":
            raise TimeoutError()  # no message at all
        raise RuntimeError("Client error '404 Not Found' for url 'https://api.example/x'\nFor more information: ...")

    monkeypatch.setattr(fetcher.twitterapi_adapter, "fetch", flaky_fetch)
    config = Config(sources=[_x_source("alice"), _x_source("bob")], x_api_key="k")
    reasons = {s.handle: why for s, why in asyncio.run(fetcher.fetch_all(config)).failed}
    assert reasons == {
        "alice": "TimeoutError",
        "bob": "Client error '404 Not Found' for url 'https://api.example/x'",
    }


@pytest.mark.parametrize("backend, max_per_source, age_cutoff_hours, expected", [
    ("tweetapi", 0, 48, fetcher.UNLIMITED),  # billed per call, not per tweet: the age cutoff is the only brake
    ("tweetapi", 0, 0, 100),  # no age cutoff either: a safety ceiling stays
    ("twitterapi", 0, 48, 100),  # billed per tweet returned: a safety ceiling stays
    ("twitterapi", 0, 0, 100),
    ("tweetapi", 15, 48, 30),  # the user's own number: twice it, room for the replies and retweets the filters drop
    ("twitterapi", 15, 48, 30),
])
def test_the_most_items_asked_of_a_backend_for_one_handle(monkeypatch, backend, max_per_source, age_cutoff_hours, expected):
    seen = _spy_on_max_items(monkeypatch)
    config = Config(sources=[_x_source()], x_backend=backend, x_api_key="k", tweetapi_api_key="k",
                    max_per_source=max_per_source, x_max_age_hours=age_cutoff_hours)
    asyncio.run(fetcher.fetch_all(config))
    assert seen["max_items"] == expected


def test_a_widened_lookback_counts_as_an_age_cutoff_for_the_unlimited_ceiling(monkeypatch):
    """After a long break `xmd fetch` widens the lookback for one call; that is still a cutoff to stop the paging."""
    seen = _spy_on_max_items(monkeypatch)
    config = Config(sources=[_x_source()], x_backend="tweetapi", tweetapi_api_key="k", max_per_source=0, x_max_age_hours=0)
    asyncio.run(fetcher.fetch_all(config, x_max_age_hours_override=168))
    assert seen["max_items"] == fetcher.UNLIMITED


def test_a_handle_cut_short_by_the_safety_ceiling_is_named_in_a_warning(monkeypatch, caplog):
    """`0` used to mean 'unlimited' in every doc while quietly stopping at 100 tweets a handle: nothing said so."""
    _spy_on_max_items(monkeypatch, returns=_fetched)
    config = Config(sources=[_x_source("alice")], x_api_key="k", max_per_source=0)
    with caplog.at_level(logging.WARNING, logger="xmd"):
        asyncio.run(fetcher.fetch_all(config))
    assert "@alice" in caplog.text and "safety ceiling of 100" in caplog.text


@pytest.mark.parametrize("backend, max_per_source, returned", [
    ("twitterapi", 15, 30),  # a ceiling the user chose is theirs: no warning
    ("twitterapi", 0, 99),  # under the safety ceiling: nothing was cut
    ("tweetapi", 0, 500),  # no ceiling on this backend: nothing was cut
])
def test_no_warning_when_nothing_was_cut_or_the_ceiling_is_the_users_own(monkeypatch, caplog, backend, max_per_source, returned):
    _spy_on_max_items(monkeypatch, returns=lambda max_items: _fetched(returned))
    config = Config(sources=[_x_source("alice")], x_backend=backend, x_api_key="k", tweetapi_api_key="k",
                    max_per_source=max_per_source)
    with caplog.at_level(logging.WARNING, logger="xmd"):
        asyncio.run(fetcher.fetch_all(config))
    assert "safety ceiling" not in caplog.text
