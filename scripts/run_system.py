"""Summarize a digest's export with a local model: Phase 2 (docs/digest-summary.md).

Chunks the newest digest export (`xmd digest` writes it) for a compression ratio, sends every chunk to a model behind an
OpenAI-compatible server (LM Studio by default) with its band's prompt, and saves the
raw replies, timings and token counts under digests/.runs/<label>/ for verifying,
assembling and scoring later. What it prints is counts and timings, no post text (a
failed chunk's line may carry a few words of the model's reply).

    python scripts\\run_system.py --model qwen --dry-run
    python scripts\\run_system.py --model qwen --only regular-01
    python scripts\\run_system.py --model qwen
    python scripts\\run_system.py --model qwen --compression 10%     # a shorter brief than the default 30%

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
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run from a checkout, installed or not

import run_stats  # noqa: E402  (the script next to this one)
from xmd.summary.chunk import (  # noqa: E402
    CHARS_PER_TOKEN, DEFAULT_COMPRESSION, DEFAULT_MAX_INPUT_TOKENS, estimate_tokens, make_chunks, parse_compression,
    parse_export, tier_shares,
)
from xmd.core.config import read_group_levels  # noqa: E402
from xmd.summary.engine import (  # noqa: E402
    API_KEY_ENV_VAR, DEFAULT_BASE_URL, Engine, EngineError, ModelChoiceError, choose_model, list_models,
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Summarize a digest export with a local model and save the raw replies.")
    ap.add_argument("--compression", type=_compression, default=DEFAULT_COMPRESSION, metavar="RATIO",
                    help="summary words / source words for the regular posts: 0.30 or 30%% (the default) keeps about 30%% "
                         "of their words, 0.10 about 10%%. Retweets and the recap group keep their own fixed shares")
    ap.add_argument("--model", default="", help="model id, or part of one; omit it when the server offers just one")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL, help="default: LM Studio")
    ap.add_argument("--api-key", default=os.environ.get(API_KEY_ENV_VAR, ""),
                    help=f"only if the runtime wants one (default: ${API_KEY_ENV_VAR}; a key typed here stays in your shell history)")
    ap.add_argument("--export", default="", help="export .txt to brief (default: the newest in DIGESTS/.export)")
    ap.add_argument("--digests-dir", default="digests")
    ap.add_argument("--sources-dir", default="sources", help="where x.md is: its `## group | high` / `| low` levels set how much each group keeps")
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
    args = ap.parse_args(argv)

    digests = Path(args.digests_dir)
    export = _latest_export(digests, args.export)
    mapping = json.loads(export.with_suffix(".map.json").read_text(encoding="utf-8"))
    posts, repeated = parse_export(export.read_text(encoding="utf-8"), mapping)
    x_md = Path(args.sources_dir) / "x.md"
    levels = read_group_levels(x_md) if x_md.exists() else {}
    chunks = make_chunks(posts, repeated, tier_shares(args.compression), args.max_input_tokens, levels=levels)
    wanted = {c.strip() for c in args.only.split(",") if c.strip()}
    if wanted:
        unknown = wanted - {c.id for c in chunks}
        if unknown:
            print(f"no such chunk: {', '.join(sorted(unknown))}. There are: {', '.join(c.id for c in chunks)}")
            return 2
        chunks = [c for c in chunks if c.id in wanted]

    system_for = lru_cache(maxsize=None)(load_prompt)
    print(f"export {export.name}  sha256 {_sha(export)[:16]}...  compression {args.compression:.2f}")
    print("group levels: " + (", ".join(f"{g}={lv}" for g, lv in sorted(levels.items())) if levels
                              else f"none set in {x_md} (every group is normal)"))
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
        print(f"\ncannot reach the server: {exc}\nLM Studio: Developer tab -> start the server (port 1234).")
        return 1
    except ModelChoiceError as exc:
        print(f"\n{exc}")
        return 2
    if note:
        print(f"\n{note}")

    started = datetime.now(timezone.utc)
    with Engine(model, args.base_url, args.api_key, args.timeout, args.no_think) as engine:
        print(f"\nmodel {model}" + ("  (thinking off requested)" if args.no_think else ""))
        try:  # loads the model outside the timed calls, and a thinking model fails here, not after ten calls
            warm = engine.complete("Reply with one word.", "ok", max_tokens=16)
        except EngineError as exc:
            print(f"warm-up failed, nothing was run: {exc}")
            return 1
        print(f"warm-up ok in {warm.seconds:.1f} s (not counted)\n")
        schema = (lambda chunk: bounded_schema(chunk.min_items, chunk.max_items)) if args.bound_items else BRIEF_SCHEMA
        results = run_chunks(engine, chunks, system_for, schema, args.retries, args.temperature, on_result=_show)

    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", model).strip("-")
    label = args.label or f"{slug}__c{args.compression:.2f}__{started:%Y%m%d-%H%M%S}" + ("__partial" if wanted else "")
    out = Path(args.out_dir) / label if args.out_dir else digests / ".runs" / label
    write_run(out, {
        "label": label,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "engine": {"base_url": args.base_url, "model": model, "no_think": args.no_think,
                   "temperature": args.temperature, "retries": args.retries, "bound_items": args.bound_items,
                   "warmup_seconds": round(warm.seconds, 1)},
        "compression_ratio": args.compression,
        "ratios": tier_shares(args.compression),
        "levels": levels,
        "chunking": {"max_input_tokens": args.max_input_tokens, "chars_per_token": CHARS_PER_TOKEN,
                     "only": sorted(wanted) or None},
        "export": {"file": export.name, "sha256": _sha(export)},
        "prompt": {"file": DEFAULT_PROMPT.name, "sha256": _sha(DEFAULT_PROMPT)},
    }, results)

    total = summarize(results)
    failed = f", {total['failed']} FAILED" if total["failed"] else ""
    print(f"\n{total['ok']}/{total['chunks']} chunks ok{failed}; {total['seconds']:.0f} s of model time "
          f"({total['seconds'] / 60:.1f} min)")
    print(f"tokens: {total['prompt_tokens']:,} in, {total['completion_tokens']:,} out; "
          f"reply {total['reply_words']:,} words against a target of {total['target_words']:,}")
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
    run_stats.report(out, with_summary=False)
    return 1 if total["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
