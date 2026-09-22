import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from xmd.cli import (
    PROGRESS_BAR_WIDTH, SINCE_RUN_MAX_HOURS, _companions, _digest, _fetch_to_store, _render_progress,
    _report_failures,
)
from xmd.core.config import Config, Source
from xmd.core.models import FeedItem
from xmd.core.store import Store
from xmd.ingest.fetcher import FetchResult
from xmd.summary.brief import digest_item_ids, load_item_ids


def _config(tmp_path, **kw) -> Config:
    return Config(
        sources=[Source(name="@dave", type="twitterapi", handle="dave", platform="x")],
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
        return FetchResult([])

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


def test_render_progress_shows_an_eta_once_a_source_has_answered(capsys):
    _render_progress(5, 10, started=time.perf_counter() - 10)
    assert "s left" in capsys.readouterr().out  # 10s for 5/10 -> ~10s left for the other 5


def test_render_progress_shows_no_eta_before_anything_finishes(capsys):
    _render_progress(0, 10, started=time.perf_counter())
    assert "left" not in capsys.readouterr().out


def test_fetch_to_store_dedupes_and_records_last_run(tmp_path, monkeypatch):
    from xmd import cli as cli_module

    async def fake_fetch_all(config, progress=None, x_max_age_hours_override=None):
        return FetchResult([FeedItem(source="@dave", source_type="x", title="t", url="https://x.com/k/1")])

    monkeypatch.setattr(cli_module, "fetch_all", fake_fetch_all)
    config = _config(tmp_path)

    new = asyncio.run(_fetch_to_store(config))
    assert new == 1

    store = Store(config.storage)
    assert store.get_meta("last_run_at") is not None
    store.close()

    new_again = asyncio.run(_fetch_to_store(config))  # same item -> deduped
    assert new_again == 0


def test_fetch_to_store_reports_failed_sources_and_still_stores_the_rest(tmp_path, monkeypatch, capsys):
    from xmd import cli as cli_module

    async def fake_fetch_all(config, progress=None, x_max_age_hours_override=None):
        return FetchResult(
            [FeedItem(source="@dave", source_type="x", title="t", url="https://x.com/k/1")],
            [(Source(name="@alice", type="twitterapi", handle="alice"), "could not resolve @alice to a user id")],
        )

    monkeypatch.setattr(cli_module, "fetch_all", fake_fetch_all)

    assert asyncio.run(_fetch_to_store(_config(tmp_path))) == 1  # dave's item is kept
    assert "  @alice: could not resolve @alice to a user id\n" in capsys.readouterr().out


def test_report_failures_names_each_handle_and_why(capsys):
    _report_failures([
        (Source(name="@alice", type="twitterapi", handle="alice"), "could not resolve @alice to a user id"),
        (Source(name="Bob Wire", type="twitterapi", handle="bob"), "Client error '404 Not Found'"),
    ])
    out = capsys.readouterr().out
    assert out.startswith("2 source(s) failed to fetch")
    assert "renamed" in out  # says what to do about it
    assert "  @alice: could not resolve @alice to a user id\n" in out  # no "(name)" when the name is just the handle
    assert "  @bob (Bob Wire): Client error '404 Not Found'\n" in out


def test_report_failures_is_silent_when_nothing_failed(capsys):
    _report_failures([])
    assert capsys.readouterr().out == ""


def test_fetch_to_store_widens_lookback_after_a_long_gap(tmp_path, monkeypatch):
    from xmd import cli as cli_module

    seen = {}

    async def fake_fetch_all(config, progress=None, x_max_age_hours_override=None):
        seen["override"] = x_max_age_hours_override
        return FetchResult([])

    monkeypatch.setattr(cli_module, "fetch_all", fake_fetch_all)
    config = _config(tmp_path, x_max_age_hours=48)

    store = Store(config.storage)
    stale = datetime.now(timezone.utc) - timedelta(hours=200)  # older than 48h and the 168h cap
    store.set_meta("last_run_at", stale.isoformat())
    store.close()

    asyncio.run(_fetch_to_store(config))
    assert seen["override"] == SINCE_RUN_MAX_HOURS + 1  # capped, not the full 200h gap


def test_fetch_to_store_narrows_lookback_for_a_recent_gap(tmp_path, monkeypatch):
    from xmd import cli as cli_module

    seen = {}

    async def fake_fetch_all(config, progress=None, x_max_age_hours_override=None):
        seen["override"] = x_max_age_hours_override
        return FetchResult([])

    monkeypatch.setattr(cli_module, "fetch_all", fake_fetch_all)
    config = _config(tmp_path, x_max_age_hours=48)

    store = Store(config.storage)
    recent = datetime.now(timezone.utc) - timedelta(hours=2)
    store.set_meta("last_run_at", recent.isoformat())
    store.close()

    asyncio.run(_fetch_to_store(config))
    assert seen["override"] == 3  # sized to the ~2h gap, not the full 48h default


def test_fetch_to_store_no_override_on_first_run(tmp_path, monkeypatch):
    from xmd import cli as cli_module

    seen = {}

    async def fake_fetch_all(config, progress=None, x_max_age_hours_override=None):
        seen["override"] = x_max_age_hours_override
        return FetchResult([])

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

    out_path = _digest(config, "since-run", full=True)
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

    out_path = _digest(config, "since-run", full=True)  # no prior last_digest_at -> first-run default
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

    out_path = _digest(config, "since-run", full=True)  # fetched after the first call's cursor -> shows up
    assert out_path is not None
    assert "https://x.com/k/1" in out_path.read_text(encoding="utf-8")


def test_the_since_run_cursor_moves_only_once_the_digest_is_written(tmp_path, monkeypatch):
    """A failed write must not use up the window: its items would be missing from every later since-run digest."""
    from xmd import cli as cli_module

    config = _config(tmp_path)
    store = Store(config.storage)
    store.add_items([FeedItem(source="@k", source_type="x", title="t", url="https://x.com/k/1", full_text="t")])
    store.close()

    def disk_full(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(cli_module, "write_digest", disk_full)
    with pytest.raises(OSError):
        _digest(config, "since-run")
    store = Store(config.storage)
    assert store.get_meta("last_digest_at") is None  # not moved: the item is still waiting for its digest
    store.close()

    monkeypatch.undo()
    out_path = _digest(config, "since-run", full=True)
    assert out_path is not None and "https://x.com/k/1" in out_path.read_text(encoding="utf-8")
    store = Store(config.storage)
    assert store.get_meta("last_digest_at") is not None  # and now it has moved
    store.close()


def test_digest_24h_window_ignores_since_run_cursor(tmp_path):
    config = _config(tmp_path)
    store = Store(config.storage)
    within_24h = datetime.now(timezone.utc) - timedelta(hours=10)
    store.add_items([
        FeedItem(source="@k", source_type="x", title="t", url="https://x.com/k/1", published=within_24h),
    ])
    store.set_meta("last_digest_at", datetime.now(timezone.utc).isoformat())  # would exclude it under since-run
    store.close()

    out_path = _digest(config, "24h", full=True)
    assert out_path is not None
    assert "https://x.com/k/1" in out_path.read_text(encoding="utf-8")


def test_digest_returns_none_when_nothing_new(tmp_path):
    config = _config(tmp_path)
    Store(config.storage).close()  # empty store
    assert _digest(config, "since-run") is None


def test_digest_writes_a_quick_digest_and_llm_export_next_to_the_full_one(tmp_path):
    config = Config(
        sources=[
            Source(name="@vip", type="twitterapi", handle="vip", priority=True),
            Source(name="@chat", type="twitterapi", handle="chat", group="chatter"),
        ],
        x_api_key="k", storage=tmp_path / "t.db", digest_dir=tmp_path / "digests",
        recap_groups=frozenset({"chatter"}),
    )
    when = datetime.now(timezone.utc) - timedelta(hours=1)
    store = Store(config.storage)
    store.add_items([
        FeedItem(source="@vip", source_type="x", title="v", url="https://x.com/vip/1", published=when,
                 full_text="the priority post"),
        FeedItem(source="@chat", source_type="x", title="c", url="https://x.com/chat/1", published=when,
                 full_text="recap chatter", group="chatter"),
        FeedItem(source="@reg", source_type="x", title="r", url="https://x.com/reg/1", published=when,
                 full_text="a regular post"),
    ])
    store.close()

    full = _digest(config, "24h", full=True, quick=True)
    quick, export, export_map, export_items = _companions(full)

    assert full.name.endswith(".md") and quick.name == f"{full.stem}-quick.md"
    assert export.parent == full.parent / ".export" and export_map.name == f"{full.stem}.map.json"
    assert export_items.name == f"{full.stem}.items.json"
    quick_text = quick.read_text(encoding="utf-8")
    assert f"full text: [{full.name}]({full.name})" in quick_text
    assert "### ★ Priority — 1" in quick_text and "the priority post" in quick_text
    assert "recap chatter" not in quick_text  # the recap group is collapsed
    assert "Recap group: 1 item from 1 source" in quick_text
    assert "recap chatter" in full.read_text(encoding="utf-8")  # the archive still has it
    export_text = export.read_text(encoding="utf-8")
    assert "a regular post" in export_text and "recap chatter" in export_text
    assert "the priority post" not in export_text  # priority passes through verbatim, not via the model
    assert {m["tier"] for m in json.loads(export_map.read_text(encoding="utf-8")).values()} == {"regular", "recap"}


def _one_post_of_every_kind(tmp_path) -> Config:
    """A store with a priority post, a recap-group post, an image tweet, a plain retweet and a regular post."""
    config = Config(
        sources=[
            Source(name="@vip", type="twitterapi", handle="vip", priority=True),
            Source(name="@chat", type="twitterapi", handle="chat", group="chatter"),
        ],
        x_api_key="k", storage=tmp_path / "t.db", digest_dir=tmp_path / "digests",
        recap_groups=frozenset({"chatter"}),
    )
    when = datetime.now(timezone.utc) - timedelta(hours=1)
    store = Store(config.storage)
    store.add_items([
        FeedItem(source="@vip", source_type="x", title="v", url="https://x.com/vip/1", published=when,
                 full_text="the priority post"),
        FeedItem(source="@chat", source_type="x", title="c", url="https://x.com/chat/1", published=when,
                 full_text="recap chatter", group="chatter"),
        FeedItem(source="@pic", source_type="x", title="p", url="https://x.com/pic/1", published=when,
                 full_text="a post with a picture", images=["https://img.example/p1.jpg"]),
        FeedItem(source="@rt", source_type="x", title="t", url="https://x.com/rt/1", published=when, full_text="",
                 retweet_of_author="orig", retweet_of_text="what rt retweeted"),
        FeedItem(source="@reg", source_type="x", title="r", url="https://x.com/reg/1", published=when,
                 full_text="a regular post"),
    ])
    store.close()
    return config


def test_digest_writes_only_what_the_brief_needs_by_default(tmp_path):
    config = _one_post_of_every_kind(tmp_path)

    anchor = _digest(config, "24h")
    quick, export, export_map, export_items = _companions(anchor)

    assert not anchor.exists() and not quick.exists()  # the full and quick digests are opt-in
    assert export.exists() and export_map.exists() and export_items.exists()
    assert [p.name for p in anchor.parent.iterdir()] == [".export"]  # nothing else lands in digests/
    # the export leaves out the priority and image tweets; the brief still needs to find them again
    assert len(json.loads(export_map.read_text(encoding="utf-8"))) == 3
    assert len(load_item_ids(export, anchor)) == 5


def test_the_full_digest_alone_on_request(tmp_path):
    full = _digest(_one_post_of_every_kind(tmp_path), "24h", full=True)
    quick = _companions(full)[0]

    assert full.exists() and not quick.exists()


def test_the_quick_digest_alone_on_request_links_no_full_digest(tmp_path):
    full = _digest(_one_post_of_every_kind(tmp_path), "24h", quick=True)
    quick = _companions(full)[0]

    assert quick.exists() and not full.exists()
    text = quick.read_text(encoding="utf-8")
    assert "Recap group: 1 item from 1 source" in text
    assert "full text" not in text.lower()  # neither the header nor the recap note points at a file not written


def test_the_items_file_lists_what_the_full_digest_lists_and_assemble_finds_it(tmp_path):
    full = _digest(_one_post_of_every_kind(tmp_path), "24h", full=True)
    _quick, export, _export_map, export_items = _companions(full)

    ids = json.loads(export_items.read_text(encoding="utf-8"))
    assert set(ids) == set(digest_item_ids(full.read_text(encoding="utf-8")))
    assert load_item_ids(export, full) == ids


def test_run_digest_returns_the_export_the_brief_is_made_from(tmp_path, capsys):
    from xmd.cli import run_digest

    export = run_digest(_one_post_of_every_kind(tmp_path), "24h")

    assert export.parent.name == ".export" and export.suffix == ".txt" and export.exists()
    assert capsys.readouterr().out.startswith(f"export: {export}")


def test_run_digest_returns_none_and_says_so_when_nothing_is_new(tmp_path, capsys):
    from xmd.cli import run_digest

    config = _config(tmp_path)
    Store(config.storage).close()  # empty store

    assert run_digest(config, "since-run") is None
    assert "nothing new" in capsys.readouterr().out


def test_a_poster_back_after_more_than_15_days_is_shown_in_full_but_a_plain_retweet_is_not(tmp_path):
    config = Config(
        sources=[Source(name="@dave", type="twitterapi", handle="dave"), Source(name="@erin", type="twitterapi", handle="erin")],
        x_api_key="k", storage=tmp_path / "t.db", digest_dir=tmp_path / "digests",
    )
    now = datetime.now(timezone.utc)
    hour, long_ago = now - timedelta(hours=1), now - timedelta(days=20)
    long_text = "a long quote-tweet comment " * 20
    store = Store(config.storage)
    store.add_items([
        FeedItem(source="@dave", source_type="x", title="d", url="https://x.com/dave/old", published=long_ago, full_text="old post"),
        FeedItem(source="@dave", source_type="x", title="d", url="https://x.com/dave/quote", published=hour,
                 full_text=long_text, retweet_of_author="orig", retweet_of_text="the post dave is quoting"),
        FeedItem(source="@erin", source_type="x", title="e", url="https://x.com/erin/old", published=long_ago, full_text="old post"),
        FeedItem(source="@erin", source_type="x", title="e", url="https://x.com/erin/rt", published=hour, full_text="",
                 retweet_of_author="orig", retweet_of_text="what erin retweeted"),
        FeedItem(source="@erin", source_type="x", title="e", url="https://x.com/erin/recent", published=now - timedelta(days=2),
                 full_text="a post two days ago"),
    ])
    store.close()

    full = _digest(config, "24h", quick=True)
    quick, export, export_map, _export_items = _companions(full)
    quick_text = quick.read_text(encoding="utf-8")
    priority = quick_text[quick_text.index("### ★ Priority"):]
    assert "### ★ Priority — 1" in priority and long_text.strip() in priority  # dave: whole, not clipped to a line
    assert "the post dave is quoting" in priority
    assert "🔁 **@erin** → @orig" in quick_text  # erin's plain retweet stays a one-liner
    assert long_text.strip() not in export.read_text(encoding="utf-8")  # and dave never reaches a model
    assert "what erin retweeted" in export.read_text(encoding="utf-8")
    assert "dave" not in {m["source"].lstrip("@") for m in json.loads(export_map.read_text(encoding="utf-8")).values()}


def _stored_hello(tmp_path, monkeypatch) -> None:
    from xmd import cli as cli_module

    config = _config(tmp_path)
    store = Store(config.storage)
    store.add_items([FeedItem(source="@k", source_type="x", title="t", url="https://x.com/k/1",
                              published=datetime.now(timezone.utc), full_text="hello world")])
    store.close()
    monkeypatch.setattr(cli_module, "load_config", lambda path: config)


def test_main_prints_where_each_digest_file_went(tmp_path, monkeypatch, capsys):
    from xmd import cli as cli_module

    _stored_hello(tmp_path, monkeypatch)

    cli_module.main(["digest", "--window", "24h", "--full", "--quick"])
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("saved: ") and out[1].startswith("quick: ") and out[2].startswith("export: ")


def test_main_names_only_the_export_when_neither_digest_is_asked_for(tmp_path, monkeypatch, capsys):
    from xmd import cli as cli_module

    _stored_hello(tmp_path, monkeypatch)

    cli_module.main(["digest", "--window", "24h"])
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1 and out[0].startswith("export: ")
