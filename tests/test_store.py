from datetime import datetime, timezone

from xmd.models import FeedItem
from xmd.store import Store


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
