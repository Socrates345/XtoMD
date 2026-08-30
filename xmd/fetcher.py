"""Fetch orchestration: run every source's adapter, tolerate per-source
failure.

X is the only platform today, dispatched on `config.x_backend`. Adding
another platform later needs no restructuring — an adapter is just a module
with `fetch(source, max_items, **kwargs) -> list[FeedItem]` (see
adapters/twitterapi.py), and `one()` below would grow one more `elif
source.type == "...":` branch. A generic RSS/Atom adapter already existed in
this project's history (feedparser-based, ~120 lines, no external service
needed) and can be resurrected from git history (`main` branch,
`rss40/adapters/rss.py`) when RSS/YouTube/Substack support is actually
wanted — not built now.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from .adapters import tweetapi as tweetapi_adapter
from .adapters import twitterapi as twitterapi_adapter
from .config import Config
from .filters import apply_filters
from .models import FeedItem

log = logging.getLogger("xmd")

# Polite ceiling: with a large handle list, unbounded concurrency would
# hammer the backend and get the API key rate-limited.
MAX_CONCURRENCY = 10


async def fetch_all(
    config: Config,
    progress: Callable[[str], None] | None = None,
    x_max_age_hours_override: int | None = None,
) -> list[FeedItem]:
    """Fetch every source concurrently (bounded). A failing source logs a
    warning and contributes nothing; it never fails the whole run.

    `x_max_age_hours_override`, when given, replaces `config.x_max_age_hours`
    for this call only — backs the "since last run" AFK safeguard in cli.py,
    which widens the lookback to cover a gap without persisting the change."""
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    total = len(config.sources)
    done = 0
    max_age_hours = (
        config.x_max_age_hours if x_max_age_hours_override is None else x_max_age_hours_override
    )

    async def one(source) -> list[FeedItem]:
        nonlocal done
        t0 = time.perf_counter()
        try:
            async with sem:
                # fetch headroom over max_per_source; 0 = unlimited, so take
                # whatever the backend offers per page (~20/handle)
                max_items = config.max_per_source * 2 or 100
                adapter = tweetapi_adapter if config.x_backend == "tweetapi" else twitterapi_adapter
                batch = await asyncio.to_thread(
                    adapter.fetch,
                    source, config.active_x_api_key, max_items,
                    max_age_hours=max_age_hours,
                    fallback_latest=True,
                )
            log.info(
                "source %s: %d item(s) in %.1fs",
                source.name, len(batch), time.perf_counter() - t0,
            )
            return batch
        except Exception as exc:
            log.warning(
                "source %r failed after %.1fs: %s",
                source.name, time.perf_counter() - t0, exc,
            )
            return []
        finally:
            done += 1
            if progress:
                progress(f"fetching sources... {done}/{total}")

    results = await asyncio.gather(*(one(s) for s in config.sources))
    items = [item for batch in results for item in batch]
    return apply_filters(items, config)
