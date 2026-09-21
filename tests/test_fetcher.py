import asyncio

from xmd import fetcher
from xmd.config import Config, Source


def _x_source(handle: str = "dave") -> Source:
    return Source(name=f"@{handle}", type="twitterapi", handle=handle, platform="x")


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
    items = asyncio.run(fetcher.fetch_all(config))  # must not raise
    assert items == []
