"""SQLite persistence: dedupe fetched items, track the last successful run.

Deliberately keeps every row forever — no purge, no delivered/undelivered
flag. This is a personal tweet archive, not a delivery queue: `xmd digest`
just re-queries a time window every time, so there's nothing to "mark seen."
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import FeedItem
from .tiers import SILENCE_DAYS, mark_after_silence

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id                TEXT PRIMARY KEY,
    source            TEXT NOT NULL,
    source_type       TEXT NOT NULL,
    title             TEXT NOT NULL,
    url               TEXT NOT NULL,
    author            TEXT,
    published         TEXT,
    text              TEXT,
    full_text         TEXT,
    images            TEXT,
    grp               TEXT,
    retweet_of_author TEXT,
    retweet_of_text   TEXT,
    fetched_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_ITEM_COLUMNS = (
    "id, source, source_type, title, url, author, published, text, full_text,"
    " images, grp, retweet_of_author, retweet_of_text"
)


class Store:
    """SQLite persistence: dedupes items, answers time-window queries."""

    def __init__(self, path: str | Path = "xmd.db") -> None:
        self.conn = sqlite3.connect(str(path))
        self.conn.executescript(_SCHEMA)

    def add_items(self, items: list[FeedItem]) -> int:
        """Insert items, ignoring ones already seen (by id = sha256(url)).
        Returns count of actually-new rows."""
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.executemany(
            f"INSERT OR IGNORE INTO items ({_ITEM_COLUMNS}, fetched_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    i.id,
                    i.source,
                    i.source_type,
                    i.title,
                    i.url,
                    i.author,
                    i.published.isoformat() if i.published else None,
                    i.text,
                    i.full_text,
                    json.dumps(i.images) if i.images else None,
                    i.group,
                    i.retweet_of_author,
                    i.retweet_of_text,
                    now,
                )
                for i in items
            ],
        )
        self.conn.commit()
        return cur.rowcount

    def recent(self, since: datetime) -> list[FeedItem]:
        """All items published (or, when undated, fetched) at/after `since`,
        newest first. Powers the "24h" window: a content-time filter."""
        cutoff = since.isoformat()
        rows = self.conn.execute(
            f"SELECT {_ITEM_COLUMNS} FROM items"
            " WHERE COALESCE(published, fetched_at) >= ?"
            " ORDER BY published DESC",
            (cutoff,),
        ).fetchall()
        return [_row_to_item(r) for r in rows]

    def published_between(
        self, since: datetime, until: datetime, fetched_by: datetime | None = None
    ) -> list[FeedItem]:
        """Items published (or, when undated, fetched) in [since, until), newest
        first: a fixed window that can be cut again later, unlike `recent`'s
        open-ended one. `fetched_by` also drops anything stored after that
        moment, so a later fetch that back-fills the window can't change it.
        Bounds should be timezone-aware UTC, like what's stored."""
        sql = (
            f"SELECT {_ITEM_COLUMNS} FROM items"
            " WHERE COALESCE(published, fetched_at) >= ? AND COALESCE(published, fetched_at) < ?"
        )
        params = [since.isoformat(), until.isoformat()]
        if fetched_by is not None:
            sql += " AND fetched_at <= ?"
            params.append(fetched_by.isoformat())
        rows = self.conn.execute(sql + " ORDER BY published DESC", params).fetchall()
        return [_row_to_item(r) for r in rows]

    def post_times(self, since: datetime, fetched_by: datetime | None = None) -> dict[str, list[datetime]]:
        """{source: publish times, oldest first} of every dated post at/after `since`, plus each
        source's latest post before it: all a digest needs to know about a poster's past, and the
        extra one is what shows a poster who was silent through the whole span. `fetched_by`
        ignores anything stored after that moment, like `published_between`'s."""
        seen = " AND fetched_at <= ?" if fetched_by is not None else ""
        extra = [fetched_by.isoformat()] if fetched_by is not None else []
        bound = since.isoformat()
        rows = self.conn.execute(
            f"SELECT source, published FROM items WHERE published >= ?{seen}"
            f" UNION ALL SELECT source, MAX(published) FROM items WHERE published < ?{seen} GROUP BY source",
            [bound, *extra, bound, *extra],
        )
        times: dict[str, list[datetime]] = {}
        for source, published in rows:
            times.setdefault(source, []).append(datetime.fromisoformat(published))
        for stamps in times.values():
            stamps.sort()
        return times

    def flag_after_silence(
        self, items: list[FeedItem], days: int = SILENCE_DAYS, fetched_by: datetime | None = None
    ) -> int:
        """Mark the items whose poster had been silent for more than `days` days (see
        tiers.mark_after_silence). Call it wherever a digest loads its items, before rendering:
        the rest of the pipeline reads the flag through `tier_of`."""
        dated = [i.published for i in items if i.published]
        if not dated:
            return 0
        return mark_after_silence(items, self.post_times(min(dated) - timedelta(days=days), fetched_by), days)

    def get_many(self, ids: list[str]) -> list[FeedItem]:
        """The stored items with these ids, newest first: a digest lists its items by id (`<!-- item:ID -->`),
        so this rebuilds exactly what a past digest was made from."""
        items: list[FeedItem] = []
        for k in range(0, len(ids), 500):  # SQLite caps the variables in one query
            batch = ids[k:k + 500]
            rows = self.conn.execute(
                f"SELECT {_ITEM_COLUMNS} FROM items WHERE id IN ({','.join('?' * len(batch))})", batch
            ).fetchall()
            items += [_row_to_item(r) for r in rows]
        return sorted(items, key=lambda i: i.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    def recent_by_fetch(self, since: datetime) -> list[FeedItem]:
        """All items *fetched* at/after `since`, regardless of when they
        were published. Powers the "since-run" window: a tweet is almost
        always published before the moment it's fetched, so filtering on
        `published` there would make a digest run right after a fetch come
        up empty — this filters on `fetched_at` instead, i.e. what the most
        recent fetch(es) actually added."""
        cutoff = since.isoformat()
        rows = self.conn.execute(
            f"SELECT {_ITEM_COLUMNS} FROM items"
            " WHERE fetched_at >= ?"
            " ORDER BY published DESC",
            (cutoff,),
        ).fetchall()
        return [_row_to_item(r) for r in rows]

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def _row_to_item(row: tuple) -> FeedItem:
    (id_, source, source_type, title, url, author, published, text,
     full_text, images, grp, retweet_of_author, retweet_of_text) = row
    return FeedItem(
        id=id_,
        source=source,
        source_type=source_type,
        title=title,
        url=url,
        author=author or "",
        published=datetime.fromisoformat(published) if published else None,
        text=text or "",
        full_text=full_text or "",
        images=json.loads(images) if images else [],
        group=grp or "",
        retweet_of_author=retweet_of_author or "",
        retweet_of_text=retweet_of_text or "",
    )
