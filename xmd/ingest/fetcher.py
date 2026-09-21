"""Fetch orchestration: run every source's adapter, tolerate per-source
failure.

X is the only platform today, dispatched on `config.x_backend`. Adding
another platform later needs no restructuring — an adapter is just a module
with `fetch(source, api_key, max_items, **kwargs) -> list[FeedItem]` (see
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
from dataclasses import dataclass, field
from typing import Callable

from .adapters import UNLIMITED
from .adapters import tweetapi as tweetapi_adapter
from .adapters import twitterapi as twitterapi_adapter
from ..core.config import Config, Source
from .filters import apply_filters
from ..core.models import FeedItem

log = logging.getLogger("xmd")

# Polite ceiling: with a large handle list, unbounded concurrency would
# hammer the backend and get the API key rate-limited.
MAX_CONCURRENCY = 10

# Items fetched per handle when neither the user's own number nor the billing model sets a limit: a handle that
# posts more than this between two fetches loses the rest for good, so reaching it is warned about.
SAFETY_CEILING = 100


def _fetch_ceiling(config: Config, max_age_hours: int) -> tuple[int, bool]:
    """(the most items to fetch for one handle, whether that is the safety ceiling rather than the user's number).

    `fetch.max_per_source: N` asks for up to 2N: the spare half is room for the replies and retweets the filters
    drop. `0` means no limit where that is safe: tweetapi.com bills per call, not per tweet, and the age cutoff
    ends the paging. On twitterapi.io, which bills per tweet returned, or with no age cutoff, `0` keeps the
    safety ceiling."""
    if config.max_per_source > 0:
        return config.max_per_source * 2, False
    if config.x_backend == "tweetapi" and max_age_hours > 0:
        return UNLIMITED, False
    return SAFETY_CEILING, True


@dataclass
class FetchResult:
    items: list[FeedItem]
    # (source, why), in config order: the sources whose fetch raised. A source
    # that answered with no tweets is not in here.
    failed: list[tuple[Source, str]] = field(default_factory=list)


def _reason(exc: Exception) -> str:
    """One line saying what went wrong: httpx's status errors run to several
    lines, and some exceptions (a timeout) carry no message at all."""
    lines = str(exc).splitlines()
    return lines[0] if lines and lines[0] else type(exc).__name__


async def fetch_all(
    config: Config,
    progress: Callable[[int, int], None] | None = None,
    x_max_age_hours_override: int | None = None,
) -> FetchResult:
    """Fetch every source concurrently (bounded). A failing source logs a
    warning, contributes nothing and is named in the result's `failed` (a
    handle that was renamed shows up this way); it never fails the whole run.

    `x_max_age_hours_override`, when given, replaces `config.x_max_age_hours`
    for this call only — backs the "since last run" AFK safeguard in cli.py,
    which widens the lookback to cover a gap without persisting the change."""
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    total = len(config.sources)
    done = 0
    max_age_hours = (
        config.x_max_age_hours if x_max_age_hours_override is None else x_max_age_hours_override
    )
    max_items, ceiling_is_default = _fetch_ceiling(config, max_age_hours)

    async def one(source) -> tuple[list[FeedItem], str]:
        """(items, "") on success, ([], why) when the source's fetch raised."""
        nonlocal done
        t0 = time.perf_counter()
        try:
            async with sem:
                adapter = tweetapi_adapter if config.x_backend == "tweetapi" else twitterapi_adapter
                fetch_kwargs = dict(max_age_hours=max_age_hours, fallback_latest=True)
                if config.x_backend == "tweetapi":
                    # only tweetapi.com is known to need pacing (see
                    # adapters/tweetapi.py's _RateLimiter) — twitterapi.io
                    # relies on _get()'s retry-on-429 backoff instead
                    fetch_kwargs["rate_limit_per_minute"] = config.tweetapi_rate_limit_per_minute
                batch = await asyncio.to_thread(
                    adapter.fetch,
                    source, config.active_x_api_key, max_items,
                    **fetch_kwargs,
                )
            log.info(
                "source %s: %d item(s) in %.1fs",
                source.name, len(batch), time.perf_counter() - t0,
            )
            if ceiling_is_default and len(batch) >= max_items:
                log.warning(
                    "source %s: fetch stopped at the safety ceiling of %d items, so its older tweets in the window "
                    "were not fetched (fetch.max_per_source: set a number to choose the ceiling yourself)",
                    source.name, max_items,
                )
            return batch, ""
        except Exception as exc:
            why = _reason(exc)
            log.warning(
                "source %r failed after %.1fs: %s",
                source.name, time.perf_counter() - t0, why,
            )
            return [], why
        finally:
            done += 1
            if progress:
                progress(done, total)

    results = await asyncio.gather(*(one(s) for s in config.sources))
    items = [item for batch, _ in results for item in batch]
    failed = [(s, why) for s, (_, why) in zip(config.sources, results) if why]
    return FetchResult(apply_filters(items, config), failed)
