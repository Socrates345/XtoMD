from datetime import datetime, timedelta, timezone

from xmd.core.models import FeedItem
from xmd.core.store import Store


def _item(url: str, published=None, source="src") -> FeedItem:
    return FeedItem(
        source=source,
        source_type="x",
        title=f"item {url}",
        url=url,
        published=published,
    )


def test_dedupe(tmp_path):
    store = Store(tmp_path / "t.db")
    items = [_item("https://a/1"), _item("https://a/2")]
    assert store.add_items(items) == 2
    assert store.add_items(items) == 0  # same URLs -> ignored
    assert store.add_items([_item("https://a/3")]) == 1
    store.close()


def test_published_between_is_a_fixed_window_start_inclusive_end_exclusive(tmp_path):
    store = Store(tmp_path / "t.db")
    start = datetime(2026, 9, 18, 16, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 19, 16, 0, tzinfo=timezone.utc)
    store.add_items([
        _item("https://a/before", datetime(2026, 9, 18, 15, 59, tzinfo=timezone.utc)),
        _item("https://a/at-start", start),
        _item("https://a/inside", datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc)),
        _item("https://a/at-end", end),
        _item("https://a/after", datetime(2026, 9, 20, tzinfo=timezone.utc)),
    ])
    urls = [i.url for i in store.published_between(start, end)]
    assert urls == ["https://a/inside", "https://a/at-start"]  # newest first
    store.close()


def test_published_between_can_ignore_what_a_later_fetch_added(tmp_path):
    store = Store(tmp_path / "t.db")
    start = datetime(2026, 9, 18, 16, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 19, 16, 0, tzinfo=timezone.utc)
    store.add_items([
        _item("https://a/early", datetime(2026, 9, 19, 1, 0, tzinfo=timezone.utc)),
        _item("https://a/backfilled", datetime(2026, 9, 19, 2, 0, tzinfo=timezone.utc)),
    ])
    for url, fetched in (("https://a/early", "2026-09-19T11:45:10+00:00"),
                         ("https://a/backfilled", "2026-09-20T11:52:03+00:00")):
        store.conn.execute("UPDATE items SET fetched_at = ? WHERE url = ?", (fetched, url))
    fetched_by = datetime(2026, 9, 19, 11, 46, tzinfo=timezone.utc)
    assert [i.url for i in store.published_between(start, end, fetched_by)] == ["https://a/early"]
    assert len(store.published_between(start, end)) == 2  # without the bound, the back-fill is in
    store.close()


def test_recent_window(tmp_path):
    store = Store(tmp_path / "t.db")
    old = _item("https://a/old", datetime(2026, 7, 1, tzinfo=timezone.utc))
    new = _item("https://a/new", datetime(2026, 7, 7, 12, 0, tzinfo=timezone.utc))
    undated = _item("https://a/undated")  # falls back to fetched_at (now)
    store.add_items([old, new, undated])

    since = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    urls = [i.url for i in store.recent(since)]
    assert "https://a/new" in urls
    assert "https://a/undated" in urls
    assert "https://a/old" not in urls
    store.close()


def test_meta_roundtrip(tmp_path):
    store = Store(tmp_path / "t.db")
    assert store.get_meta("last_run_at") is None
    store.set_meta("last_run_at", "2026-07-08T00:00:00+00:00")
    assert store.get_meta("last_run_at") == "2026-07-08T00:00:00+00:00"
    store.set_meta("last_run_at", "2026-07-09T00:00:00+00:00")  # upsert
    assert store.get_meta("last_run_at") == "2026-07-09T00:00:00+00:00"
    store.close()


def test_roundtrip_fields_including_retweet_info(tmp_path):
    store = Store(tmp_path / "t.db")
    when = datetime(2026, 7, 6, 12, 30, tzinfo=timezone.utc)
    item = FeedItem(
        source="@someone",
        source_type="x",
        title="RT @orig: a tweet",
        url="https://x.com/someone/status/1",
        author="someone",
        published=when,
        text="my added comment",
        full_text="my added comment",
        images=["https://pbs.twimg.com/media/Gx1.jpg"],
        group="finance",
        retweet_of_author="orig",
        retweet_of_text="the original tweet text",
    )
    store.add_items([item])
    (got,) = store.recent(datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert got == item  # retweet_of_author/text must survive the round trip
    store.close()


def test_get_many_returns_the_items_with_those_ids_newest_first_and_ignores_unknown_ones(tmp_path):
    store = Store(tmp_path / "t.db")
    items = [_item(f"https://a/{k}", datetime(2026, 9, 19, k, 0, tzinfo=timezone.utc)) for k in range(1, 6)]
    store.add_items(items)
    wanted = [items[0].id, items[3].id, "0000000000000000"]
    assert [i.url for i in store.get_many(wanted)] == ["https://a/4", "https://a/1"]
    assert store.get_many([]) == []
    many = [_item(f"https://b/{k}", datetime(2026, 9, 18, tzinfo=timezone.utc)) for k in range(1200)]  # past SQLite's limit
    store.add_items(many)
    assert len(store.get_many([i.id for i in many])) == 1200
    store.close()


def test_post_times_are_per_source_oldest_first_with_the_last_post_before_the_bound(tmp_path):
    store = Store(tmp_path / "t.db")
    day = lambda d: datetime(2026, 9, d, 12, 0, tzinfo=timezone.utc)  # noqa: E731
    store.add_items([
        _item("https://a/1", day(9), "@a"), _item("https://a/2", day(3), "@a"), _item("https://a/3", day(1), "@a"),
        _item("https://a/4", day(2), "@a"), _item("https://b/1", day(5), "@b"), _item("https://b/2", day(1), "@b"),
        _item("https://c/undated", None, "@c"), _item("https://d/1", day(1), "@d"),
    ])
    assert store.post_times(day(3)) == {  # from day 3 on, plus the latest one before it; undated skipped
        "@a": [day(2), day(3), day(9)], "@b": [day(1), day(5)], "@d": [day(1)],
    }
    store.close()


def test_post_times_can_ignore_what_a_later_fetch_added(tmp_path):
    store = Store(tmp_path / "t.db")
    when = datetime(2026, 9, 1, tzinfo=timezone.utc)
    store.add_items([_item("https://a/early", when, "@a"), _item("https://a/backfilled", when + timedelta(days=1), "@a")])
    store.conn.execute("UPDATE items SET fetched_at = '2026-09-01T12:00:00+00:00' WHERE url = 'https://a/early'")
    store.conn.execute("UPDATE items SET fetched_at = '2026-09-20T12:00:00+00:00' WHERE url = 'https://a/backfilled'")
    assert len(store.post_times(when)["@a"]) == 2
    assert store.post_times(when, datetime(2026, 9, 10, tzinfo=timezone.utc)) == {"@a": [when]}
    assert store.post_times(when + timedelta(days=5), datetime(2026, 9, 10, tzinfo=timezone.utc)) == {"@a": [when]}
    store.close()


def test_flag_after_silence_marks_the_first_post_back_from_the_database_history(tmp_path):
    store = Store(tmp_path / "t.db")
    old = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    back = old + timedelta(days=30)
    store.add_items([
        _item("https://a/old", old, "@a"), _item("https://a/back", back, "@a"),
        _item("https://b/before", back - timedelta(days=4), "@b"), _item("https://b/now", back - timedelta(days=2), "@b"),
        _item("https://c/new", back, "@c"),
    ])
    window = store.published_between(back - timedelta(days=3), back + timedelta(days=1))
    assert store.flag_after_silence(window) == 1
    assert {i.url for i in window if i.after_silence} == {"https://a/back"}  # @b posted 2 days before, @c is unknown
    assert store.flag_after_silence([]) == 0
    store.close()


def test_the_flag_is_derived_and_never_stored(tmp_path):
    store = Store(tmp_path / "t.db")
    old = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    store.add_items([_item("https://a/old", old, "@a"), _item("https://a/back", old + timedelta(days=30), "@a")])
    items = store.recent(old)
    store.flag_after_silence(items)
    assert [i.after_silence for i in items] == [True, False]  # newest first
    assert not any(i.after_silence for i in store.recent(old))  # a fresh load starts unflagged
    store.close()
