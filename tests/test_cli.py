import asyncio
from datetime import datetime, timedelta, timezone

from xmd.cli import PROGRESS_BAR_WIDTH, SINCE_RUN_MAX_HOURS, _digest, _fetch_to_store, _render_progress
from xmd.config import Config, Source
from xmd.models import FeedItem
from xmd.store import Store


def _config(tmp_path, **kw) -> Config:
    return Config(
        sources=[Source(name="@karpathy", type="twitterapi", handle="karpathy", platform="x")],
        x_api_key="k",
        storage=tmp_path / "t.db",
        digest_dir=tmp_path / "digests",
        **kw,
    )


def test_fetch_to_store_forwards_progress_callback(tmp_path, monkeypatch):
    from xmd import cli as cli_module

    seen = {}

    async def fake_fetch_all(config, progress=None, x_max_age_hours_override=None):
        seen["progress"] = progress
        return []

    monkeypatch.setattr(cli_module, "fetch_all", fake_fetch_all)
    marker = lambda done, total: None
    asyncio.run(_fetch_to_store(_config(tmp_path), progress=marker))
    assert seen["progress"] is marker


def test_render_progress_fills_bar_proportionally(capsys):
    _render_progress(5, 10)
    out = capsys.readouterr().out
    assert out.startswith("\rfetching sources [")
    assert "5/10" in out
    filled = out.count("#")
    assert filled == PROGRESS_BAR_WIDTH // 2
    assert not out.endswith("\n")  # not done yet -> no trailing newline


def test_render_progress_appends_newline_when_done(capsys):
    _render_progress(10, 10)
    out = capsys.readouterr().out
    assert out.endswith("\n")
    assert out.count("#") == PROGRESS_BAR_WIDTH


def test_render_progress_ignores_zero_total(capsys):
    _render_progress(0, 0)
    assert capsys.readouterr().out == ""


def test_fetch_to_store_dedupes_and_records_last_run(tmp_path, monkeypatch):
    from xmd import cli as cli_module

    async def fake_fetch_all(config, progress=None, x_max_age_hours_override=None):
        return [FeedItem(source="@karpathy", source_type="x", title="t", url="https://x.com/k/1")]

    monkeypatch.setattr(cli_module, "fetch_all", fake_fetch_all)
    config = _config(tmp_path)

    new = asyncio.run(_fetch_to_store(config))
    assert new == 1

    store = Store(config.storage)
    assert store.get_meta("last_run_at") is not None
    store.close()

    new_again = asyncio.run(_fetch_to_store(config))  # same item -> deduped
    assert new_again == 0


def test_fetch_to_store_widens_lookback_after_a_long_gap(tmp_path, monkeypatch):
    from xmd import cli as cli_module

    seen = {}

    async def fake_fetch_all(config, progress=None, x_max_age_hours_override=None):
        seen["override"] = x_max_age_hours_override
        return []

    monkeypatch.setattr(cli_module, "fetch_all", fake_fetch_all)
    config = _config(tmp_path, x_max_age_hours=48)

    store = Store(config.storage)
    stale = datetime.now(timezone.utc) - timedelta(hours=200)  # older than 48h and the 168h cap
    store.set_meta("last_run_at", stale.isoformat())
    store.close()

    asyncio.run(_fetch_to_store(config))
    assert seen["override"] == SINCE_RUN_MAX_HOURS + 1  # capped, not the full 200h gap


def test_fetch_to_store_no_override_on_first_run(tmp_path, monkeypatch):
    from xmd import cli as cli_module

    seen = {}

    async def fake_fetch_all(config, progress=None, x_max_age_hours_override=None):
        seen["override"] = x_max_age_hours_override
        return []

    monkeypatch.setattr(cli_module, "fetch_all", fake_fetch_all)
    asyncio.run(_fetch_to_store(_config(tmp_path)))
    assert seen["override"] is None


def test_digest_since_run_filters_by_fetch_time_not_published(tmp_path):
    """since-run's cursor is `last_digest_at` vs. `fetched_at` -- published
    date must be irrelevant to which side of the cursor an item falls on."""
    config = _config(tmp_path)
    store = Store(config.storage)
    store.add_items([
        FeedItem(source="@k", source_type="x", title="old", url="https://x.com/k/1", full_text="old"),
        FeedItem(source="@k", source_type="x", title="new", url="https://x.com/k/2", full_text="new"),
    ])
    old_fetch = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
    store.conn.execute("UPDATE items SET fetched_at = ? WHERE title = 'old'", (old_fetch,))
    store.conn.commit()
    store.set_meta("last_digest_at", (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat())
    store.close()

    out_path = _digest(config, "since-run")
    assert out_path is not None
    text = out_path.read_text(encoding="utf-8")
    assert "new" in text and "old" not in text


def test_digest_since_run_shows_items_published_before_they_were_fetched(tmp_path):
    """Regression: a tweet's published date is (almost) always before the
    moment it's fetched. A digest run right after a fetch must not come up
    empty just because every item's published date predates the run."""
    config = _config(tmp_path)
    store = Store(config.storage)
    days_ago = datetime.now(timezone.utc) - timedelta(days=3)  # tweet posted days before this fetch
    store.add_items([
        FeedItem(
            source="@k", source_type="x", title="just fetched", url="https://x.com/k/1",
            published=days_ago, full_text="just fetched",
        ),
    ])
    store.close()

    out_path = _digest(config, "since-run")  # no prior last_digest_at -> first-run default
    assert out_path is not None
    assert "just fetched" in out_path.read_text(encoding="utf-8")


def test_digest_since_run_cursor_advances_even_when_empty(tmp_path):
    config = _config(tmp_path)
    Store(config.storage).close()
    assert _digest(config, "since-run") is None  # nothing yet -> cursor still advances

    store = Store(config.storage)
    new = datetime.now(timezone.utc) - timedelta(minutes=1)
    store.add_items([
        FeedItem(source="@k", source_type="x", title="t", url="https://x.com/k/1", published=new),
    ])
    store.close()

    out_path = _digest(config, "since-run")  # fetched after the first call's cursor -> shows up
    assert out_path is not None
    assert "https://x.com/k/1" in out_path.read_text(encoding="utf-8")


def test_digest_24h_window_ignores_since_run_cursor(tmp_path):
    config = _config(tmp_path)
    store = Store(config.storage)
    within_24h = datetime.now(timezone.utc) - timedelta(hours=10)
    store.add_items([
        FeedItem(source="@k", source_type="x", title="t", url="https://x.com/k/1", published=within_24h),
    ])
    store.set_meta("last_digest_at", datetime.now(timezone.utc).isoformat())  # would exclude it under since-run
    store.close()

    out_path = _digest(config, "24h")
    assert out_path is not None
    assert "https://x.com/k/1" in out_path.read_text(encoding="utf-8")


def test_digest_returns_none_when_nothing_new(tmp_path):
    config = _config(tmp_path)
    Store(config.storage).close()  # empty store
    assert _digest(config, "since-run") is None
