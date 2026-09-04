from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__
from .config import Config, load_config
from .fetcher import fetch_all
from .render import build_markdown
from .store import Store

DEFAULT_LOOP_SECONDS = 900  # 15 min
SINCE_RUN_MAX_HOURS = 168  # 7 days -- AFK-gap cap, so a long absence can't blow up API cost


async def _fetch_to_store(config: Config) -> int:
    """Fetch every source and store new items. If the previous run was
    longer ago than `config.x_max_age_hours` covers, widen this fetch's
    lookback to close the gap (capped at SINCE_RUN_MAX_HOURS) — otherwise
    `xmd digest --window since-run` could claim a window the fetch never
    actually covered."""
    store = Store(config.storage)
    try:
        last_run_at = store.get_meta("last_run_at")
    finally:
        store.close()

    override = None
    if last_run_at:
        gap_hours = (datetime.now(timezone.utc) - datetime.fromisoformat(last_run_at)).total_seconds() / 3600
        if gap_hours > config.x_max_age_hours:
            override = int(min(gap_hours, SINCE_RUN_MAX_HOURS)) + 1

    items = await fetch_all(config, x_max_age_hours_override=override)
    store = Store(config.storage)
    try:
        new = store.add_items(items)
        store.set_meta("last_run_at", datetime.now(timezone.utc).isoformat())
    finally:
        store.close()
    return new


def _digest(config: Config, window: str) -> Path | None:
    """`window="24h"` is a content-time filter (published in the last 24h).
    `window="since-run"` is a fetch-time filter against its own cursor
    (`last_digest_at`, separate from fetch's `last_run_at`): everything
    stored since the *previous* `xmd digest` call, regardless of when it was
    published — filtering "since-run" on published date instead would make
    a digest right after a fetch come up empty, since a tweet is always
    published before the moment it's fetched.

    The cursor advances on every call, even when nothing new is found, so
    "since-run" always means "since I last checked," not "since I last saw
    something.\""""
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
        store.set_meta("last_digest_at", now.isoformat())
    finally:
        store.close()

    if not items:
        return None

    label = "past 24h" if window == "24h" else "since last run"
    priority_sources = frozenset(s.name for s in config.sources if s.priority)
    markdown = build_markdown(items, now, label=label, priority_sources=priority_sources)
    config.digest_dir.mkdir(parents=True, exist_ok=True)
    out_path = config.digest_dir / f"{now.strftime('%Y-%m-%d-%H%M')}.md"
    out_path.write_text(markdown, encoding="utf-8")
    return out_path


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
        if args.loop:
            log = logging.getLogger("xmd")
            log.info("looping fetch every %ss — Ctrl+C to stop", args.loop)
            while True:
                new = asyncio.run(_fetch_to_store(config))
                print(f"fetched: {new} new item(s)")
                time.sleep(args.loop)
        new = asyncio.run(_fetch_to_store(config))
        print(f"fetched: {new} new item(s)")
        return

    if args.command == "digest":
        out_path = _digest(config, args.window)
        if out_path:
            print(f"saved: {out_path}")
        else:
            print("nothing new in this window — no file written")
        return


if __name__ == "__main__":
    main()
