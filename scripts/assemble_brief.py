"""Assemble a saved run into the readable brief: Phase 2 (docs/digest-summary.md).

Reads a run (digests/.runs/<label>), the digest export it was made from, and the digest's
own posts (rebuilt from the item ids in the full digest, out of a copy of the database), and
writes `digests/<date>-brief.md`: priority tweets first and in full, then trending
stories, then each group's section in the order of its importance, with the image tweets
whole and the model's summaries linking to the tweets they cite. Each section opens with a
short summary of what it was about (one model call per section, the same model as the run,
cached in the run's folder) and the names that were louder than usual against the last 14
days. It checks every summary against what it was written from and prints only counts.

    python scripts\\assemble_brief.py                        # the newest run in digests/.runs
    python scripts\\assemble_brief.py --run qwen-qwen3.5-9b__A__20260920-152052
    python scripts\\assemble_brief.py --no-summaries          # counting only, no model call

Run it from the repo root so sources.yaml (priority sources, recap groups) is your real one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run from a checkout, installed or not

from xmd.brief import build_brief, digest_item_ids, digest_time, load_run, summary_items  # noqa: E402
from xmd.chunk import parse_export  # noqa: E402
from xmd.config import load_config  # noqa: E402
from xmd.engine import Engine  # noqa: E402
from xmd.prompt import load_section_prompt  # noqa: E402
from xmd.runner import compression_label  # noqa: E402
from xmd.sections import load_cache, save_cache, section_inputs, summarize_sections  # noqa: E402
from xmd.store import Store  # noqa: E402
from xmd.topics import topics_by_section, usage_baseline  # noqa: E402

HISTORY_DAYS = 14  # what "louder than usual" is measured against


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Assemble a saved run into the readable brief.")
    ap.add_argument("--run", default="", help="run label (default: the newest in DIGESTS/.runs)")
    ap.add_argument("--config", default="sources.yaml", help="path to sources.yaml")
    ap.add_argument("--digests-dir", default="digests")
    ap.add_argument("--out", default="", help="where to write the brief (default: DIGESTS/<date>-brief.md)")
    ap.add_argument("--no-summaries", action="store_true", help="do not call the model for section summaries")
    ap.add_argument("--refresh", action="store_true", help="ask the model again even where an answer is cached")
    ap.add_argument("--model", default="", help="model for the section summaries (default: the run's)")
    ap.add_argument("--base-url", default="", help="server for the section summaries (default: the run's)")
    ap.add_argument("--api-key", default="")
    args = ap.parse_args(argv)

    digests = Path(args.digests_dir)
    runs = digests / ".runs"
    if args.run:
        run = runs / args.run
    else:
        found = sorted((p for p in runs.glob("*") if (p / "run.json").exists()), key=lambda p: p.stat().st_mtime)
        if not found:
            print(f"no runs in {runs}")
            return 1
        run = found[-1]
    manifest, records = load_run(run)

    export = digests / ".export" / manifest["export"]["file"]
    stem = export.stem
    if _sha(export) != manifest["export"]["sha256"]:
        print(f"WARNING: {export.name} is not the export this run read (hash differs)")
    mapping = json.loads(export.with_suffix(".map.json").read_text(encoding="utf-8"))
    posts, repeated = parse_export(export.read_text(encoding="utf-8"), mapping)
    full = digests / f"{stem}.md"
    if not full.exists():
        print(f"{full} is missing: the full digest lists the items this brief is made from")
        return 1
    ids = digest_item_ids(full.read_text(encoding="utf-8"))

    config = load_config(args.config)
    with tempfile.TemporaryDirectory() as tmp:  # a copy, so the real database is never touched
        copy = Path(tmp) / "xmd.db"
        shutil.copy2(config.storage, copy)
        store = Store(copy)
        try:
            items = store.get_many(ids)
            store.flag_after_silence(items)  # as the digest did, so the same tweets are priority
            dated = [i.published for i in items if i.published]
            since = min(dated) if dated else datetime.now(timezone.utc)
            history = store.published_between(since - timedelta(days=HISTORY_DAYS), since)
        finally:
            store.close()
    if len(items) != len(ids):
        print(f"WARNING: {len(ids) - len(items)} of the digest's {len(ids)} items are no longer in the database")
    now = digest_time(stem) or (max(dated) if dated else datetime.now(timezone.utc))

    engine, compression = manifest["engine"], compression_label(manifest)
    levels = manifest.get("levels") or {}
    post_text = {p.n: p.text for p in posts}
    topics = topics_by_section(items, usage_baseline(history))

    summaries: dict[str, str] = {}
    kept, _dropped = summary_items(records, mapping, post_text, repeated, levels, config.recap_groups)
    inputs = section_inputs(kept, mapping, topics)
    if inputs and not args.no_summaries:
        system = load_section_prompt()
        cache_path = run / "sections.json"
        cache = {} if args.refresh else load_cache(cache_path)

        def show(label: str, result, cached: bool) -> None:
            status = "cached" if cached else ("ok" if result.text else f"skipped: {(result.error or '').splitlines()[0][:100]}")
            print(f"  section summary {label}: {status}" + (f" ({len(result.text.split())} words)" if result.text else ""))

        print("section summaries:")
        with Engine(args.model or engine["model"], args.base_url or engine["base_url"], args.api_key, 120,
                    engine.get("no_think", False)) as model:
            results = summarize_sections(model, inputs, system, cache, on_result=show)
        save_cache(cache_path, cache)
        summaries = {label: r.text for label, r in results.items() if r.text}
        print()

    brief = build_brief(
        items, now, records, mapping, post_text, repeated,
        priority_sources=frozenset(s.name for s in config.sources if s.priority),
        recap_groups=config.recap_groups,
        levels=levels,
        similarity=config.trending_similarity,
        snippet_chars=config.snippet_chars, retweet_chars=config.retweet_chars, media_chars=config.media_chars,
        full_name=full.name, topics=topics, summaries=summaries,
        model_line=f"Brief by {engine['model']}, {compression}, prompt {manifest['prompt']['sha256'][:8]}, run {run.name}",
    )
    out = Path(args.out) if args.out else digests / f"{now.strftime('%Y-%m-%d')}-brief.md"
    out.write_text(brief.markdown, encoding="utf-8")

    s = brief.stats
    print(f"{run.name}: {len(items)} items in the digest, {s['summary_items']} summary items kept, "
          f"{s['dropped_items']} dropped, {s['flagged_items']} marked with a warning, {s['trending_items']} trending; "
          f"{s['section_summaries']} section summaries, {s['louder_topics']} louder-than-usual names")
    if s["regular_posts"]:
        print(f"regular posts cited by a summary: {s['regular_posts_cited']} of {s['regular_posts']}; "
              f"posts without any summary (failed or not run): {s['posts_without_summary']}")
    print("\nwhat you would read:")
    for name, words in sorted(s["bands"].items(), key=lambda kv: -kv[1]):
        print(f"  {name:<16}{words:>7,} words")
    print(f"  {'total':<16}{s['words']:>7,} words, {s['lines']} lines, {s['images']} images")
    print(f"reading time: ~{s['minutes_at_your_pace']:.0f} min at your pace (40 lines a minute, 4 s per image); "
          f"by words: ~{s['minutes_normal']:.0f} min at 230 wpm, ~{s['minutes_diagonal']:.0f} min at 400 wpm")
    print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
