"""Freeze one fixed window of stored posts as a reproducible reference input (Phase 2, docs/digest-summary.md).

`xmd digest` can only cut "since the last digest" or "the last 24 h from now", so it
cannot rebuild a past day. This cuts an explicit window from the database and writes
the files `xmd digest` would (full digest, quick digest, LLM export + map), named
`frozen-<from>--<to>`, plus a manifest: counts per tier, full-text words, and a hash of
the export (line endings normalized to LF, so Windows and Linux agree), so every
system can be shown to have read the same file.

The database is copied first and only the copy is opened: xmd.db and `last_digest_at`
are left alone. It prints counts only, never post text.

    python scripts\\freeze_export.py --from 2026-09-18T16:00 --to 2026-09-19T16:00 --fetched-by 2026-09-19T11:46

Without --fetched-by the window is whatever the database holds now, and a later
fetch that back-fills it changes the result. Times are UTC. Run it from the repo root so sources.yaml (and the priority and recap
settings in it) are the ones your real digests use.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run from a checkout, installed or not

from xmd.cli import _companions, write_digest  # noqa: E402
from xmd.config import load_config  # noqa: E402
from xmd.store import Store  # noqa: E402
from xmd.tiers import MEDIA, PRIORITY, RECAP, REGULAR, RETWEET, tier_of  # noqa: E402

TIERS = (PRIORITY, MEDIA, REGULAR, RETWEET, RECAP)  # the tier order of the plan's "Size and reading time"


def _utc(text: str) -> datetime:
    when = datetime.fromisoformat(text)
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Freeze a fixed window of stored posts as the reference export.")
    ap.add_argument("--from", dest="since", required=True, type=_utc, help="window start, UTC, inclusive (2026-09-18T16:00)")
    ap.add_argument("--to", dest="until", required=True, type=_utc, help="window end, UTC, exclusive")
    ap.add_argument("--fetched-by", type=_utc, default=None,
                    help="only posts stored by then, UTC (2026-09-19T11:46), so a later fetch can't change the window")
    ap.add_argument("--config", default="sources.yaml", help="path to sources.yaml")
    args = ap.parse_args(argv)
    if args.until <= args.since:
        ap.error("--to must be after --from")

    config = load_config(args.config)
    with tempfile.TemporaryDirectory() as tmp:  # read a copy, so the real database is never written
        copy = Path(tmp) / "xmd.db"
        shutil.copy2(config.storage, copy)
        store = Store(copy)
        try:
            items = store.published_between(args.since, args.until, args.fetched_by)
            store.flag_after_silence(items, fetched_by=args.fetched_by)
        finally:
            store.close()
    if not items:
        print("no items in that window")
        return 1

    priority_sources = frozenset(s.name for s in config.sources if s.priority)
    tiers = {i.id: tier_of(i, priority_sources, config.recap_groups) for i in items}
    per_tier = {
        t: {"items": sum(1 for i in items if tiers[i.id] == t),
            "words": sum(i.word_count for i in items if tiers[i.id] == t)}
        for t in TIERS
    }
    media = [i for i in items if tiers[i.id] == MEDIA]

    stem = f"frozen-{args.since:%Y-%m-%d-%H%M}--{args.until:%Y-%m-%d-%H%M}"
    label = f"frozen {args.since:%Y-%m-%d %H:%M} to {args.until:%Y-%m-%d %H:%M} UTC"
    if args.fetched_by:
        label += f", as fetched by {args.fetched_by:%Y-%m-%d %H:%M}"
    full = config.digest_dir / f"{stem}.md"
    write_digest(config, items, args.until, label, full)  # `now` is the window end, so a re-freeze is identical
    quick, export, export_map = _companions(full)

    manifest = {
        "window": {
            "from": args.since.isoformat(), "to": args.until.isoformat(),
            "fetched_by": args.fetched_by.isoformat() if args.fetched_by else None,
        },
        "items": len(items),
        "words": sum(i.word_count for i in items),
        "image_tweets": len(media),
        "images": sum(len(i.images) for i in media),
        "tiers": per_tier,
        "export": {
            "posts": len(json.loads(export_map.read_text(encoding="utf-8"))),
            # LF-normalized: Windows writes CRLF, and the hash must not depend on the OS that froze it
            "sha256": hashlib.sha256(export.read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
        },
        "files": [p.name for p in (full, quick, export, export_map)],
    }
    manifest_path = export.with_name(f"{stem}.manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")

    print(label)
    print(f"{manifest['items']} items, {manifest['words']:,} words, "
          f"{manifest['images']} images in {manifest['image_tweets']} image tweets\n")
    print(f"{'tier':<10}{'items':>7}{'words':>9}")
    for tier in TIERS:
        print(f"{tier:<10}{per_tier[tier]['items']:>7}{per_tier[tier]['words']:>9,}")
    print(f"\nexport: {manifest['export']['posts']} posts, sha256 {manifest['export']['sha256'][:16]}...")
    print(f"written to {full.parent}: {', '.join(manifest['files'])}, {manifest_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
