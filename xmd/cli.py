from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import __version__
from .core.config import Config, Source, load_config
from .digest.export import build_export
from .ingest.fetcher import fetch_all
from .core.models import FeedItem
from .digest.render import build_markdown, build_quick
from .core.store import Store

EXPORT_DIRNAME = ".export"  # dot-dir under digest_dir: invisible to Obsidian
DEFAULT_LOOP_SECONDS = 900  # 15 min
SINCE_RUN_MAX_HOURS = 168  # 7 days -- AFK-gap cap, so a long absence can't blow up API cost
PROGRESS_BAR_WIDTH = 30


def _render_progress(done: int, total: int, started: float | None = None) -> None:
    """Plain in-place progress bar (\\r, no dependency) — fetches can now
    take minutes under tweetapi.com's rate limit, so a silent hang until
    the final `fetched: N new item(s)` line is a bad default. `started`
    (a time.perf_counter() reading from when this pass began) adds a
    rough ETA, extrapolated from the rate so far, once at least one
    source has answered."""
    if total <= 0:
        return
    filled = int(PROGRESS_BAR_WIDTH * min(done / total, 1.0))
    bar = "#" * filled + "-" * (PROGRESS_BAR_WIDTH - filled)
    eta = ""
    if started is not None and done:
        remaining = (time.perf_counter() - started) / done * (total - done)
        eta = f", ~{remaining:.0f}s left" if remaining >= 1 else ", almost done"
    end = "\n" if done >= total else ""
    sys.stdout.write(f"\rfetching sources [{bar}] {done}/{total}{eta}    {end}")
    sys.stdout.flush()


def _report_failures(failed: list[tuple[Source, str]]) -> None:
    """Name every source that could not be fetched, and why, after the pass:
    the usual cause is an account that changed its @, which only the user can
    fix. The per-source log line is easy to miss among the INFO lines and the
    progress bar."""
    if not failed:
        return
    print(f"{len(failed)} source(s) failed to fetch (if an account was renamed, update its handle in x.md):")
    for source, why in failed:
        label = f"@{source.handle}" if source.name == f"@{source.handle}" else f"@{source.handle} ({source.name})"
        print(f"  {label}: {why}")


async def _fetch_to_store(config: Config, progress: Callable[[int, int], None] | None = None) -> int:
    """Fetch every source and store new items, then report the sources that
    failed. The lookback is sized to the actual gap since the previous fetch
    rather than always the full `config.x_max_age_hours` — narrower on a quick
    re-run (so it doesn't re-page a handle's whole window for nothing), wider
    after a long gap (capped at SINCE_RUN_MAX_HOURS) so `xmd digest --window
    since-run` never claims a window the fetch didn't actually cover."""
    store = Store(config.storage)
    try:
        last_run_at = store.get_meta("last_run_at")
    finally:
        store.close()

    override = None
    if last_run_at:
        gap_hours = (datetime.now(timezone.utc) - datetime.fromisoformat(last_run_at)).total_seconds() / 3600
        override = int(min(gap_hours, SINCE_RUN_MAX_HOURS)) + 1

    result = await fetch_all(config, progress=progress, x_max_age_hours_override=override)
    store = Store(config.storage)
    try:
        new = store.add_items(result.items)
        store.set_meta("last_run_at", datetime.now(timezone.utc).isoformat())
    finally:
        store.close()
    _report_failures(result.failed)
    return new


def _digest(config: Config, window: str, full: bool = False, quick: bool = False) -> Path | None:
    """`window="24h"` is a content-time filter (published in the last 24h).
    `window="since-run"` is a fetch-time filter against its own cursor
    (`last_digest_at`, separate from fetch's `last_run_at`): everything
    stored since the *previous* `xmd digest` call, regardless of when it was
    published — filtering "since-run" on published date instead would make
    a digest right after a fetch come up empty, since a tweet is always
    published before the moment it's fetched.

    The cursor advances on every call that gets as far as writing (or finding
    nothing to write), even when nothing new is found, so "since-run" always
    means "since I last checked," not "since I last saw something.\"

    Returns the path the full digest has (or would have, without `full`): its
    stem names every file this digest writes. None when the window is empty."""
    now = datetime.now(timezone.utc)
    store = Store(config.storage)
    try:
        if window == "24h":
            items = store.recent(now - timedelta(hours=24))
        else:  # since-run
            last_digest_at = store.get_meta("last_digest_at")
            since = (
                datetime.fromisoformat(last_digest_at)
                if last_digest_at
                else now - timedelta(hours=24)
            )
            items = store.recent_by_fetch(since)
        store.flag_after_silence(items)
    finally:
        store.close()

    written = None
    if items:
        label = "past 24h" if window == "24h" else "since last run"
        out_path = config.digest_dir / f"{now.strftime('%Y-%m-%d-%H%M')}.md"
        written = write_digest(config, items, now, label, out_path, full=full, quick=quick)

    # the cursor moves once the digest is written, and to `now` (not to when the write ended), so a write that fails
    # doesn't use up the window and nothing fetched meanwhile falls between two digests
    store = Store(config.storage)
    try:
        store.set_meta("last_digest_at", now.isoformat())
    finally:
        store.close()
    return written


def write_digest(
    config: Config, items: list[FeedItem], now: datetime, label: str, out_path: Path,
    full: bool = False, quick: bool = False,
) -> Path:
    """Write what the brief is made from: the LLM export with its post-number map,
    and the ids of every item in `items` (the brief also shows the priority and
    image tweets the export leaves out, and finds them again by id). The full
    digest is written at `out_path` only with `full`, the quick digest next to it
    only with `quick`. Which items go in is the caller's business (see `_digest`,
    or scripts/freeze_export.py)."""
    priority_sources = frozenset(s.name for s in config.sources if s.priority)
    quick_path, export_path, map_path, items_path = _companions(out_path)
    export_path.parent.mkdir(parents=True, exist_ok=True)

    if full:
        out_path.write_text(
            build_markdown(
                items, now, label=label, priority_sources=priority_sources,
                similarity=config.trending_similarity, snippet_chars=config.snippet_chars,
            ),
            encoding="utf-8",
        )
    if quick:
        quick_path.write_text(
            build_quick(
                items, now, label=label, priority_sources=priority_sources,
                recap_groups=config.recap_groups, full_name=out_path.name if full else "",  # no link to a file not written
                similarity=config.trending_similarity, snippet_chars=config.snippet_chars,
                retweet_chars=config.retweet_chars, media_chars=config.media_chars,
            ),
            encoding="utf-8",
        )
    items_path.write_text(json.dumps(list(dict.fromkeys(i.id for i in items))), encoding="utf-8")
    export_text, export_map = build_export(
        items, now, label=label, priority_sources=priority_sources,
        recap_groups=config.recap_groups, similarity=config.trending_similarity,
    )
    export_path.write_text(export_text, encoding="utf-8")
    map_path.write_text(json.dumps(export_map, ensure_ascii=False, indent=1), encoding="utf-8")
    return out_path


def run_digest(config: Config, window: str, full: bool = False, quick: bool = False) -> Path | None:
    """`xmd digest`: make the digest, say where each file went, and return the export the brief is made
    from (None when the window was empty and nothing was written)."""
    out_path = _digest(config, window, full=full, quick=quick)
    if not out_path:
        print("nothing new in this window — no file written")
        return None
    quick_path, export_path, _map_path, _items_path = _companions(out_path)
    if full:
        print(f"saved: {out_path}")
    if quick:
        print(f"quick: {quick_path}")
    print(f"export: {export_path} (+ .map.json, .items.json)")
    return export_path


def _companions(full: Path) -> tuple[Path, Path, Path, Path]:
    """The other files named after a full digest: the quick digest, and the LLM
    export with its post-number map and its list of item ids (in a dot-dir, so
    Obsidian ignores them). Whether the full and quick digests exist is up to
    `write_digest`; the other three are always written."""
    export_dir = full.parent / EXPORT_DIRNAME
    return (
        full.with_name(f"{full.stem}-quick.md"),
        export_dir / f"{full.stem}.txt",
        export_dir / f"{full.stem}.map.json",
        export_dir / f"{full.stem}.items.json",
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="xmd", description="X (Twitter) -> markdown, no AI, no bot.")
    parser.add_argument("--config", default="sources.yaml", help="path to sources.yaml")
    parser.add_argument("--version", action="version", version=f"xmd {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="fetch configured X sources into the local store")
    p_fetch.add_argument(
        "--loop",
        nargs="?",
        const=DEFAULT_LOOP_SECONDS,
        type=int,
        metavar="SECONDS",
        help=f"keep fetching forever, pausing SECONDS between passes (default {DEFAULT_LOOP_SECONDS})",
    )

    p_digest = sub.add_parser("digest", help="render stored items to a markdown file")
    p_digest.add_argument(
        "--window",
        choices=["since-run", "24h"],
        default="since-run",
        help="since-run: everything stored since the last `xmd digest` (default); "
        "24h: a fixed rolling 24-hour window",
    )
    p_digest.add_argument(
        "--full",
        action="store_true",
        help="also write the full digest, every tweet in full (off by default: the brief is the reading copy)",
    )
    p_digest.add_argument(
        "--quick",
        action="store_true",
        help="also write the quick digest, one line per post (off by default)",
    )

    sub.add_parser("sources", help="list configured sources")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        sys.exit(f"config error: {exc}")

    if args.command == "sources":
        for s in config.sources:
            group = f" [{s.group}]" if s.group else ""
            priority = " (priority)" if s.priority else ""
            print(f"{s.name} -> @{s.handle}{group}{priority}")
        return

    if args.command == "fetch":
        def progress_for_this_pass():  # a fresh start time each pass, so --loop's ETA isn't cumulative
            if not sys.stdout.isatty():
                return None
            started = time.perf_counter()
            return lambda done, total: _render_progress(done, total, started)

        if args.loop:
            log = logging.getLogger("xmd")
            log.info("looping fetch every %ss — Ctrl+C to stop", args.loop)
            while True:
                new = asyncio.run(_fetch_to_store(config, progress=progress_for_this_pass()))
                print(f"fetched: {new} new item(s)")
                time.sleep(args.loop)
        new = asyncio.run(_fetch_to_store(config, progress=progress_for_this_pass()))
        print(f"fetched: {new} new item(s)")
        return

    if args.command == "digest":
        run_digest(config, args.window, full=args.full, quick=args.quick)
        return


if __name__ == "__main__":
    main()
