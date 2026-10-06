"""Assemble a saved run into the readable brief: Phase 2 (docs/digest-summary.md).

Reads a run (digests/.runs/<label>), the digest export it was made from, and the digest's
own posts (rebuilt from the item ids in `.export/<stamp>.items.json`, or in the full digest
of an older run, out of a copy of the database), and writes `digests/<date>-<hour>-<window>-brief.md` (e.g.
`2026-09-22-9pm-24h-brief.md`): priority tweets first and in full, then trending
stories, then each group's section in the order of its importance, with the image tweets
whole and the model's summaries linking to the tweets they cite. Each section opens with a
short summary of what it was about (one model call per section, the same model as the run,
cached in the run's folder) and the names that were louder than usual against the last 14
days. It checks every summary against what it was written from and prints only counts. With `language:` set in
sources.yaml, the tweets it shows word for word (priority, images, trending, the one-liners of a failed chunk) that
are in a language you don't read are translated, the original quoted under each (cached in the run's folder too).
A run made with --time (or --time given here) is fitted to that reading time (xmd/summary/budget.py): priority tweets
become one-liners when they would take more than half of it, or when the brief can't fit otherwise, and image tweets
keep their pictures only as far as the time allows. A brief
never replaces an earlier one: when its name is taken, it gets a number (`...-brief-2.md`).

    python scripts\\assemble_brief.py                        # the newest run in digests/.runs
    python scripts\\assemble_brief.py --run qwen-qwen3.5-9b__A__20260920-152052
    python scripts\\assemble_brief.py --no-summaries          # counting only, no model call

Run it from the repo root so sources.yaml (priority sources, recap groups) is your real one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run from a checkout, installed or not

from xmd.summary.brief import (  # noqa: E402
    brief_stamp, build_brief, digest_items, load_item_ids, load_run, summary_items, window_kind,
)
from xmd.summary.chunk import parse_export  # noqa: E402
from xmd.core.config import load_config  # noqa: E402
from xmd.summary.engine import API_KEY_ENV_VAR, Engine  # noqa: E402
from xmd.summary.prompt import load_section_prompt  # noqa: E402
from xmd.summary.runner import compression_label  # noqa: E402
from xmd.summary.sections import load_cache, save_cache, section_inputs, summarize_sections  # noqa: E402
from xmd.summary.topics import topics_by_section, usage_baseline  # noqa: E402
from xmd.summary import translate  # noqa: E402
from xmd.summary.budget import plan_cuts  # noqa: E402
from xmd.core.tiers import MEDIA, tier_of  # noqa: E402
from run_system import time_budget  # noqa: E402  (the script next to this one)

HISTORY_DAYS = 14  # what "louder than usual" is measured against


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _unused(path: Path) -> Path:
    """`path`, or when a file is there already, the first of `...-2.md`, `...-3.md` that isn't: a brief never
    replaces an earlier one."""
    k = 2
    candidate = path
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}-{k}{path.suffix}")
        k += 1
    return candidate


def _engine(args: argparse.Namespace, run_engine: dict, timeout: float = 120) -> Engine:
    """The model for the calls made here: the run's own, unless --model / --base-url say otherwise."""
    return Engine(args.model or run_engine["model"], args.base_url or run_engine["base_url"], args.api_key, timeout,
                  run_engine.get("no_think", False), run_engine.get("extra_body"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Assemble a saved run into the readable brief.")
    ap.add_argument("--run", default="", help="run label (default: the newest in DIGESTS/.runs)")
    ap.add_argument("--config", default="sources.yaml", help="path to sources.yaml")
    ap.add_argument("--digests-dir", default="digests")
    ap.add_argument("--out", default="",
                    help="where to write the brief (default: DIGESTS/<date>-<hour>-<window>[-<N>min]-brief.md, numbered "
                         "when that name is taken)")
    ap.add_argument("--time", type=time_budget, default=None, metavar="MIN",
                    help="fit the brief to this reading time in minutes (default: the run's own --time, if it had one); "
                         "so any run can be re-cut without summarizing again")
    ap.add_argument("--no-summaries", action="store_true", help="do not call the model for section summaries")
    ap.add_argument("--no-translate", action="store_true",
                    help="show every tweet as posted, whatever `language:` in sources.yaml says (no model call for it)")
    ap.add_argument("--refresh", action="store_true", help="ask the model again even where an answer is cached")
    ap.add_argument("--model", default="", help="model for the section summaries (default: the run's)")
    ap.add_argument("--base-url", default="", help="server for the section summaries (default: the run's)")
    ap.add_argument("--api-key", default=os.environ.get(API_KEY_ENV_VAR, ""),
                    help=f"only if the runtime wants one (default: ${API_KEY_ENV_VAR}; a key typed here stays in your shell history)")
    args = ap.parse_args(argv)

    try:  # first, as make_brief.py does: a broken config should not cost a look through the run first
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        sys.exit(f"config error: {exc}")

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
    export_text = export.read_text(encoding="utf-8")
    posts, repeated = parse_export(export_text, mapping)
    full = digests / f"{stem}.md"
    ids = load_item_ids(export, full)
    if ids is None:
        print(f"{export.with_suffix('.items.json')} is missing: it lists the items this brief is made from "
              f"(the full digest {full.name} would do, but it is missing too)")
        return 1

    items, history = digest_items(ids, config.storage, HISTORY_DAYS)
    if len(items) != len(ids):
        print(f"WARNING: {len(ids) - len(items)} of the digest's {len(ids)} items are no longer in the database")
    # the brief is dated when it is written, not when its digest was made: a run re-assembled hours later must not
    # carry the digest's hour. Shown on the configured clock.
    now = datetime.now(timezone.utc).astimezone(config.tz)

    engine, compression = manifest["engine"], compression_label(manifest)
    levels = manifest.get("levels") or {}
    post_text = {p.n: p.text for p in posts}
    topics = topics_by_section(items, usage_baseline(history))

    summaries: dict[str, str] = {}
    kept, _dropped = summary_items(records, mapping, post_text, repeated, levels, config.recap_groups)
    inputs = section_inputs(kept, mapping, topics)
    if inputs and not args.no_summaries:
        system = load_section_prompt(language=config.language)
        cache_path = run / "sections.json"
        cache = {} if args.refresh else load_cache(cache_path)

        def show(label: str, result, cached: bool) -> None:
            status = "cached" if cached else ("ok" if result.text else f"skipped: {(result.error or '').splitlines()[0][:100]}")
            print(f"  section summary {label}: {status}" + (f" ({len(result.text.split())} words)" if result.text else ""))

        print("section summaries:")
        with _engine(args, engine) as model:
            results = summarize_sections(model, inputs, system, cache, on_result=show)
        save_cache(cache_path, cache)
        summaries = {label: r.text for label, r in results.items() if r.text}
        print()

    def build(translated=None, cuts=None):
        return build_brief(
            items, now, records, mapping, post_text, repeated,
            priority_sources=frozenset(s.name for s in config.sources if s.priority),
            recap_groups=config.recap_groups,
            levels=levels,
            similarity=config.trending_similarity,
            snippet_chars=config.snippet_chars, retweet_chars=config.retweet_chars, media_chars=config.media_chars,
            full_name=full.name if full.exists() else "",  # the full digest is optional: no link to a file that isn't there
            topics=topics, summaries=summaries,
            model_line=f"Brief by {engine['model']}, {compression}, prompt {manifest['prompt']['sha256'][:8]}, run {run.name}",
            translated=translated, cuts=cuts,
        )

    minutes = args.time or manifest.get("time_budget")
    priority_sources = frozenset(s.name for s in config.sources if s.priority)
    media = [i for i in items if tier_of(i, priority_sources, config.recap_groups) == MEDIA]

    def fit(translated=None):
        """The brief, cut to the reading time when there is one, and the cuts made."""
        if not minutes:
            return None, build(translated)
        return plan_cuts(minutes, lambda cuts: build(translated, cuts), media, levels)

    cuts, _brief = fit()  # what to show decides what to translate; the fit is made again with the translations
    translations: dict = {}
    if config.language and not args.no_translate:
        shown: list[str] = []  # a first pass notes every text the brief shows word for word
        build(lambda text: shown.append(text), cuts)
        cache_path = run / "translations.json"
        cache = {} if args.refresh else translate.load_cache(cache_path)
        print(f"languages: {len(set(shown))} tweets shown as posted, checking which are not in "
              f"{', '.join(config.language.accepted)}")
        with _engine(args, engine, timeout=300) as model:  # a long post's translation takes a while
            done = translate.translate_texts(model, shown, config.language, cache,
                                             on_result=lambda note: print(f"  {note}"))
        translate.save_cache(cache_path, cache)
        translations = done.translations
        print(f"  {done.foreign} in another language, {len(translations)} translated into {config.language.translate_to}"
              + (f", {done.too_long} too long to translate" if done.too_long else ""))
        for failure in done.failed:
            print(f"  not translated: {failure}")
        print()

    cuts, brief = fit(translations.get if translations else None)
    if args.out:
        out = Path(args.out)
    else:
        stamp = brief_stamp(now, window_kind(export_text), config.tz) + (f"-{minutes:g}min" if minutes else "")
        out = _unused(digests / f"{stamp}-brief.md")
    out.write_text(brief.markdown, encoding="utf-8")

    s = brief.stats
    print(f"{run.name}: {len(items)} items in the digest, {s['summary_items']} summary items kept, "
          f"{s['dropped_items']} dropped, {s['flagged_items']} marked with a warning, {s['trending_items']} trending; "
          f"{s['section_summaries']} section summaries, {s['louder_topics']} louder-than-usual names")
    if s["regular_posts"]:
        print(f"regular posts cited by a summary: {s['regular_posts_cited']} of {s['regular_posts']}; "
              f"posts without any summary (failed or not run): {s['posts_without_summary']}")
    print(f"\nreading time: ~{s['minutes_at_your_pace']:.0f} min at your pace (40 lines a minute, 4 s per image); "
          f"by words: ~{s['minutes_normal']:.0f} min at 230 wpm, ~{s['minutes_diagonal']:.0f} min at 400 wpm")
    if minutes:
        if cuts is None:
            print(f"time {minutes:g} min: the whole brief fits, nothing cut")
        else:
            kept = sum(1 for i in media if cuts.keeps_pictures(i))
            print(f"time {minutes:g} min: {kept} of {len(media)} image tweets keep their pictures, "
                  f"{sum(1 for i in media if not cuts.keeps_pictures(i) and cuts.keeps_caption(i))} get a line, "
                  f"the rest a link" + ("; priority tweets as one-liners" if cuts.priority_one_liners else ""))
            if s["minutes_at_your_pace"] > minutes:
                print(f"  the shortest this digest makes is ~{s['minutes_at_your_pace']:.0f} min (summaries, trending, "
                      "a line per priority tweet, links for the images)")
    print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
