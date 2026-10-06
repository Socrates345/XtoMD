"""Summarize a digest's export with a local model: Phase 2 (docs/digest-summary.md).

Chunks the newest digest export (`xmd digest` writes it) for a compression ratio, sends every chunk to a model behind an
OpenAI-compatible server (LM Studio by default) with its band's prompt, and saves the
raw replies, timings and token counts under digests/.runs/<label>/ for verifying,
assembling and scoring later. What it prints is counts and timings, no post text (a
failed chunk's line may carry a few words of the model's reply).

    python scripts\\run_system.py --model qwen --dry-run
    python scripts\\run_system.py --model qwen --only regular-01
    python scripts\\run_system.py --model qwen
    python scripts\\run_system.py --model qwen --time 10           # a brief you can read in about 10 minutes
    python scripts\\run_system.py --model qwen --compression 10%     # or set the summaries' compression yourself

Before a real run: the server is up, the model is loaded with thinking off and a context
length of at least 8192 (LM Studio: the model's load settings), and nothing else heavy
is using the GPU. --model takes part of a name, as in scripts/smoke_engine.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import replace
from datetime import datetime, timezone
from functools import lru_cache, partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run from a checkout, installed or not

import run_stats  # noqa: E402  (the script next to this one)
from xmd.summary.chunk import (  # noqa: E402
    CHARS_PER_TOKEN, DEFAULT_COMPRESSION, DEFAULT_MAX_INPUT_TOKENS, estimate_tokens, make_chunks, parse_compression,
    parse_export, tier_shares,
)
from xmd.core.config import load_config, read_group_levels, read_language  # noqa: E402
from xmd.summary.brief import LINES_PER_MINUTE, SECONDS_PER_IMAGE, build_brief, digest_items, load_item_ids  # noqa: E402
from xmd.summary.budget import (  # noqa: E402
    MAX_COMPRESSION, PRIORITY_SHARE, SUMMARY_SHARE, bare_cuts, choose_compression, parse_minutes, summary_lines,
)
from xmd.summary.engine import (  # noqa: E402
    API_KEY_ENV_VAR, BASE_URL_ENV_VAR, DEFAULT_BASE_URL, EXTRA_BODY_ENV_VAR, MODEL_ENV_VAR, Engine, EngineError,
    ModelChoiceError, choose_model, list_models, on_this_machine, parse_extra_body,
)
from xmd.summary.prompt import BRIEF_SCHEMA, DEFAULT_PROMPT, bounded_schema, load_prompt  # noqa: E402
from xmd.summary.runner import (  # noqa: E402
    DEFAULT_TEMPERATURE, IMPLAUSIBLE_CHARS_PER_TOKEN, reply_budget, run_chunks, summarize, suspect_truncation, write_run,
)

CONTEXT_TOKENS = 8192  # what the plan sizes every call for


def _sha(path: Path) -> str:
    """sha256 with LF line endings, so Windows and Linux agree (as in scripts/freeze_export.py)."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _latest_export(digests: Path, given: str) -> Path:
    """The export to brief: the one named, or the newest `xmd digest` wrote."""
    if given:
        return Path(given)
    found = sorted((digests / ".export").glob("*.txt"), key=lambda p: p.stat().st_mtime)
    if not found:
        sys.exit(f"no export in {digests / '.export'}: run `xmd digest` first, or pass --export")
    return found[-1]


def _show(result) -> None:
    """One progress line per chunk: counts and timing only."""
    if not result.ok:
        print(f"{result.chunk.id:<16} FAILED  {result.seconds:5.1f} s  after {result.attempts} attempt(s): "
              f"{(result.error or '').splitlines()[0][:110]}")
        return
    used = result.completion
    tries = f"   [{result.attempts} attempts]" if result.attempts > 1 else ""
    print(f"{result.chunk.id:<16} ok      {result.seconds:5.1f} s  {used.prompt_tokens or 0:>6,} in {used.completion_tokens or 0:>5,} out   "
          f"{len(result.items):>2} items ({result.chunk.min_items}-{result.chunk.max_items}), {result.reply_words:>4} words "
          f"(target {result.chunk.target_words}){tries}")


def _compression(text: str) -> float:
    try:
        return parse_compression(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def time_budget(text: str) -> float:
    """--time's argument (make_brief.py and assemble_brief.py use it too)."""
    try:
        return parse_minutes(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def extra_body(text: str) -> dict:
    """--extra-body's argument (make_brief.py uses it too)."""
    try:
        return parse_extra_body(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _fit_compression(minutes: float, export: Path, digests: Path, config_path: str, mapping: dict, posts, repeated,
                     levels: dict, max_input_tokens: int) -> float | None:
    """The compression for a brief of `minutes`, having said how the time is shared: the brief is laid out without
    summaries (priority and image tweets, trending...) and measured, whole and at its barest, and the summaries'
    lines are added at each compression (see budget.py). None, having said why, when the digest's tweets can't be
    read."""
    try:
        config = load_config(config_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"config error: {exc}")
        return None
    ids = load_item_ids(export, digests / f"{export.stem}.md")
    if ids is None:
        print(f"{export.with_suffix('.items.json')} is missing: --time needs it to measure the brief; use --compression")
        return None
    items, _history = digest_items(ids, config.storage)
    no_summaries = [{"chunk": "plan", "tier": "regular", "ids": [p.n for p in posts], "ok": True, "reply": {"items": []}}]
    now, post_text = datetime.now(timezone.utc), {p.n: p.text for p in posts}

    def build(cuts=None):
        return build_brief(
            items, now, no_summaries, mapping, post_text, repeated,
            priority_sources=frozenset(s.name for s in config.sources if s.priority), recap_groups=config.recap_groups,
            levels=levels, similarity=config.trending_similarity, snippet_chars=config.snippet_chars,
            retweet_chars=config.retweet_chars, media_chars=config.media_chars, cuts=cuts,
        )

    def sized(cuts):
        barest = build(cuts)
        return barest, choose_compression(
            minutes, base.stats["minutes_at_your_pace"], barest.stats["minutes_at_your_pace"],
            lambda c: summary_lines(make_chunks(posts, repeated, tier_shares(c), max_input_tokens, levels=levels)))

    base = build()
    bare = bare_cuts(minutes, base)
    barest, sizing = sized(bare)
    if (sizing.summary_minutes > minutes - barest.stats["minutes_at_your_pace"] and not bare.priority_one_liners
            and base.stats["band_minutes"].get("priority")):  # as plan_cuts will: in full, the brief can't fit
        bare = replace(bare, priority_one_liners=True)
        barest, sizing = sized(bare)
    for line in _sizing_lines(minutes, sizing, bare.priority_one_liners, base.stats, barest.stats):
        print(line)
    return sizing.compression


def _sizing_lines(minutes: float, sizing, one_liners: bool, whole: dict, barest: dict) -> list[str]:
    """What --time decided, for the console: how long the brief is uncut, and how the time is shared. `whole` and
    `barest` are the stats of the brief without its summaries, uncut and at its barest."""
    pace = f"{LINES_PER_MINUTE} lines a minute, {SECONDS_PER_IMAGE} s a picture"
    if sizing.whole_minutes <= minutes:
        return [f"time {minutes:g} min ({pace}): the whole brief takes ~{sizing.whole_minutes:.0f} min at most, "
                f"so nothing is cut (compression {sizing.compression:.2f})"]
    leaves = minutes - barest["minutes_at_your_pace"]  # what the summaries and the image tweets have between them
    half = SUMMARY_SHARE * minutes <= leaves
    limit = "half the time" if half else f"the ~{max(leaves, 0):.1f} min the rest leaves"
    if sizing.compression == MAX_COMPRESSION:
        why = "the usual one: they have the room"
    elif sizing.fits:
        why = f"lowered from {MAX_COMPRESSION:.2f} to fit {limit}"
    else:
        why = f"the lowest there is, though more than {limit}"
    priority = whole["band_minutes"].get("priority", 0)
    lines = [f"time {minutes:g} min ({pace}): uncut, this brief would take ~{sizing.whole_minutes:.0f} min. So:",
             f"  summaries        ~{sizing.summary_minutes:.1f} min at most, compression {sizing.compression:.2f} ({why})"]
    if priority:
        cause = "more than half the time" if priority > PRIORITY_SHARE * minutes else "too long for the brief to fit"
        lines.append(f"  priority tweets  ~{barest['band_minutes'].get('priority', 0):.1f} min, "
                     + (f"one line each (in full ~{priority:.1f} min: {cause})" if one_liners else "in full"))
    lines.append(f"  image tweets     ~{max(leaves - sizing.summary_minutes, 0):.1f} min of the "
                 f"~{whole['band_minutes'].get('images', 0):.1f} they take whole, plus what the summaries leave unused: "
                 "pictures first, then a line each, then links")
    if sizing.summary_minutes > leaves:
        lines.append(f"  that is more than {minutes:g} min: the brief may come out longer than asked")
    lines.append("  the summaries are sized now; the priority and image tweets are cut when the brief is assembled")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Summarize a digest export with a local model and save the raw replies.")
    size = ap.add_mutually_exclusive_group()
    size.add_argument("--time", type=time_budget, default=None, metavar="MIN",
                      help="the reading time to aim at, in minutes (10, 30...): the summaries keep the usual compression "
                           "unless they alone would take more than half of it, and assemble_brief.py cuts image and "
                           "priority tweets to fit it")
    size.add_argument("--compression", type=_compression, default=DEFAULT_COMPRESSION, metavar="RATIO",
                    help="summary words / source words for the regular posts: 0.30 or 30%% (the default) keeps about 30%% "
                         "of their words, 0.10 about 10%%. Retweets and the recap group keep their own fixed shares")
    ap.add_argument("--model", default=os.environ.get(MODEL_ENV_VAR, ""),
                    help=f"model id, or part of one; omit it when the server offers just one (default: ${MODEL_ENV_VAR})")
    ap.add_argument("--base-url", default=os.environ.get(BASE_URL_ENV_VAR) or DEFAULT_BASE_URL,
                    help=f"default: ${BASE_URL_ENV_VAR}, else LM Studio")
    ap.add_argument("--api-key", default=os.environ.get(API_KEY_ENV_VAR, ""),
                    help=f"only if the runtime wants one (default: ${API_KEY_ENV_VAR}; a key typed here stays in your shell history)")
    ap.add_argument("--extra-body", type=extra_body, default=os.environ.get(EXTRA_BODY_ENV_VAR, ""), metavar="JSON",
                    help=f"a JSON object of fields added to every request, for what one provider wants and no other "
                         f"(default: ${EXTRA_BODY_ENV_VAR}; docs/remote.md has Venice's)")
    ap.add_argument("--export", default="", help="export .txt to brief (default: the newest in DIGESTS/.export)")
    ap.add_argument("--digests-dir", default="digests")
    ap.add_argument("--sources-dir", default="sources", help="where x.md is: its `## group | high` / `| low` levels set how much each group keeps")
    ap.add_argument("--config", default="sources.yaml",
                    help="sources.yaml: its `language:` says which languages the summaries may keep and which one the rest is written in")
    ap.add_argument("--out-dir", default="", help="where runs are saved (default: DIGESTS/.runs)")
    ap.add_argument("--label", default="", help="name of this run (default: model, compression and time)")
    ap.add_argument("--only", default="", help="comma-separated chunk ids to run, e.g. regular-01,recap-03")
    ap.add_argument("--dry-run", action="store_true", help="show the chunk plan and stop; no model is called")
    ap.add_argument("--no-think", action="store_true", help="send chat_template_kwargs enable_thinking=false")
    ap.add_argument("--bound-items", action=argparse.BooleanOptionalAction, default=True,
                    help="put each chunk's min and max item counts in the JSON schema (LM Studio enforces them); "
                         "--no-bound-items sends the plain schema")
    ap.add_argument("--max-input-tokens", type=int, default=DEFAULT_MAX_INPUT_TOKENS, help="posts per chunk, in estimated tokens")
    ap.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    ap.add_argument("--retries", type=int, default=1, help="extra attempts for a chunk that fails")
    ap.add_argument("--timeout", type=float, default=600, help="seconds per call")
    ap.add_argument("--stats", action="store_true",
                    help="print the full chunk plan, per-chunk progress, token counts, characters-per-token, "
                         "and the run_stats.py table (off by default: run_stats.py itself is always there on demand)")
    args = ap.parse_args(argv)

    digests = Path(args.digests_dir)
    export = _latest_export(digests, args.export)
    mapping = json.loads(export.with_suffix(".map.json").read_text(encoding="utf-8"))
    posts, repeated = parse_export(export.read_text(encoding="utf-8"), mapping)
    x_md = Path(args.sources_dir) / "x.md"
    levels = read_group_levels(x_md) if x_md.exists() else {}
    if args.time:
        compression = _fit_compression(args.time, export, digests, args.config, mapping, posts, repeated, levels,
                                       args.max_input_tokens)
        if compression is None:
            return 2
        args.compression = compression
    chunks = make_chunks(posts, repeated, tier_shares(args.compression), args.max_input_tokens, levels=levels)
    try:
        language = read_language(args.config)
    except ValueError as exc:
        print(f"config error: {exc}")
        return 2
    wanted = {c.strip() for c in args.only.split(",") if c.strip()}
    if wanted:
        unknown = wanted - {c.id for c in chunks}
        if unknown:
            print(f"no such chunk: {', '.join(sorted(unknown))}. There are: {', '.join(c.id for c in chunks)}")
            return 2
        chunks = [c for c in chunks if c.id in wanted]

    system_for = lru_cache(maxsize=None)(partial(load_prompt, language=language))
    print(f"export {export.name}  sha256 {_sha(export)[:16]}...  compression {args.compression:.2f}")
    if language:
        print(f"languages: {', '.join(language.accepted)} kept as posted, the rest written in {language.translate_to}")
    print("group levels: " + (", ".join(f"{g}={lv}" for g, lv in sorted(levels.items())) if levels
                              else f"none set in {x_md} (every group is normal)"))
    if args.dry_run or args.stats:
        print(f"\n{'chunk':<16} {'level':<7} {'posts':>5} {'est. in':>8} {'items':>7} {'target':>7} {'max out':>8}")
        needs = []
        for chunk in chunks:
            est_in = estimate_tokens(system_for(chunk.band) + chunk.render())
            needs.append(est_in + reply_budget(chunk))
            print(f"{chunk.id:<16} {chunk.level:<7} {len(chunk.posts):>5} {est_in:>8,} {f'{chunk.min_items}-{chunk.max_items}':>7} "
                  f"{chunk.target_words:>7} {reply_budget(chunk):>8,}")
        print(f"{len(chunks)} calls; the largest needs ~{max(needs):,} tokens of context (at most; the estimate is cautious), "
              f"the plan allows {CONTEXT_TOKENS:,}")
    if args.dry_run:
        return 0

    try:
        model, note = choose_model(args.model, list_models(args.base_url, args.api_key))
    except EngineError as exc:
        print(f"\ncannot reach the server: {exc}")
        if on_this_machine(args.base_url):
            print("LM Studio: Developer tab -> start the server (port 1234).")
        return 1
    except ModelChoiceError as exc:
        print(f"\n{exc}")
        return 2
    if note:
        print(f"\n{note}")

    started = datetime.now(timezone.utc)
    with Engine(model, args.base_url, args.api_key, args.timeout, args.no_think, args.extra_body) as engine:
        if args.stats:
            print(f"\nmodel {model}" + ("  (thinking off requested)" if args.no_think else ""))
        try:  # loads the model outside the timed calls, and a thinking model fails here, not after ten calls
            warm = engine.complete("Reply with one word.", "ok", max_tokens=16)
        except EngineError as exc:
            print(f"warm-up failed, nothing was run: {exc}")
            return 1
        if args.stats:
            print(f"warm-up ok in {warm.seconds:.1f} s (not counted)\n")
        schema = (lambda chunk: bounded_schema(chunk.min_items, chunk.max_items)) if args.bound_items else BRIEF_SCHEMA
        print(f"summarizing {len(chunks)} chunk{'s' if len(chunks) != 1 else ''}...")
        results = run_chunks(engine, chunks, system_for, schema, args.retries, args.temperature,
                              on_result=_show if args.stats else None)

    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", model).strip("-")
    budget = f"__t{args.time:g}" if args.time else ""
    label = args.label or f"{slug}__c{args.compression:.2f}{budget}__{started:%Y%m%d-%H%M%S}" + ("__partial" if wanted else "")
    out = Path(args.out_dir) / label if args.out_dir else digests / ".runs" / label
    write_run(out, {
        "label": label,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "engine": {"base_url": args.base_url, "model": model, "no_think": args.no_think,
                   "extra_body": args.extra_body,  # assemble_brief.py sends the same with its own calls
                   "temperature": args.temperature, "retries": args.retries, "bound_items": args.bound_items,
                   "warmup_seconds": round(warm.seconds, 1)},
        "compression_ratio": args.compression,
        "time_budget": args.time,  # minutes; assemble_brief.py fits the brief to it
        "ratios": tier_shares(args.compression),
        "levels": levels,
        "language": {"accepted": list(language.accepted), "translate_to": language.translate_to} if language else None,
        "chunking": {"max_input_tokens": args.max_input_tokens, "chars_per_token": CHARS_PER_TOKEN,
                     "only": sorted(wanted) or None},
        "export": {"file": export.name, "sha256": _sha(export)},
        "prompt": {"file": DEFAULT_PROMPT.name, "sha256": _sha(DEFAULT_PROMPT)},
    }, results)

    total = summarize(results)
    failed = f", {total['failed']} FAILED" if total["failed"] else ""
    print(f"\n{total['ok']}/{total['chunks']} chunks ok{failed}; {total['seconds']:.0f} s of model time "
          f"({total['seconds'] / 60:.1f} min); reply {total['reply_words']:,} words against a target of "
          f"{total['target_words']:,}")
    if args.stats:
        print(f"tokens: {total['prompt_tokens']:,} in, {total['completion_tokens']:,} out")
        if total["chars_per_token"]:
            print(f"measured ~{total['chars_per_token']} characters per token (the chunker assumes {CHARS_PER_TOKEN})")
    suspects = suspect_truncation(results)
    if suspects:
        print(f"WARNING: {', '.join(suspects)} reached the model with far fewer tokens than their text should "
              "make: the context window is probably too small and cut the prompt. Raise the model's context length.")
    elif (total["chars_per_token"] or 0) > IMPLAUSIBLE_CHARS_PER_TOKEN:
        print(f"WARNING: {total['chars_per_token']} characters per token is more than text can give: the context "
              "window is probably too small and cut every large prompt alike. Raise the model's context length.")
    print(f"saved to {out}\n")
    if args.stats:
        run_stats.report(out, with_summary=False)
    return 1 if total["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
