"""From a fresh fetch to the brief in one command: `xmd fetch`, `xmd digest`, `run_system.py`, `assemble_brief.py`.

    python scripts\\make_brief.py --24h                # the last 24 hours, with qwen/qwen3.5-9b
    python scripts\\make_brief.py --since-last-run     # only what is new since your last digest
    python scripts\\make_brief.py --24h --model qwen   # another model, or part of a name
    python scripts\\make_brief.py --24h --no-fetch     # the store is fresh already
    python scripts\\make_brief.py --24h --compression 10%    # a shorter brief

You must choose the window: `--since-last-run` covers what was fetched since your previous digest, so it is
a short brief when that was recent; `--24h` covers everything published in the last 24 hours, whatever was
digested before. Either one moves the since-run cursor, as any `xmd digest` does.

The one thing it cannot do for you is LM Studio. It checks the server first and, if it is not answering, says
what to open and waits: the moment the server is up it carries on by itself. It checks before it fetches
because a digest moves the since-run cursor: none is made for a brief that could not be written.

If it stops after the digest (LM Studio went away), the export is kept: run `run_system.py` and then
`assemble_brief.py` to finish, as the message says. Run it from the repo root, like the other scripts.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run from a checkout, installed or not

import assemble_brief  # noqa: E402  (the scripts next to this one)
import run_system  # noqa: E402
from xmd import cli  # noqa: E402
from xmd.core.config import load_config  # noqa: E402
from xmd.summary.chunk import parse_compression  # noqa: E402
from xmd.summary.engine import (  # noqa: E402
    API_KEY_ENV_VAR, DEFAULT_BASE_URL, Engine, EngineError, ModelChoiceError, choose_model, list_models,
)

DEFAULT_MODEL = "qwen/qwen3.5-9b"  # the setup the README describes and tests
POLL_SECONDS = 3  # between two looks at a server that is not up yet
WARMUP_TIMEOUT = 600  # a model that has to be loaded first is slow to answer once
WINDOW_WORDS = {"since-run": "only what is new since your last digest", "24h": "everything from the last 24 hours"}


def _compression(text: str) -> float:
    try:
        return parse_compression(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def wait_for_server(base_url: str, api_key: str, wait: float) -> list[str] | None:
    """The server's model listing (None: it answers but lists none). While it is down: say what to open, then
    look every few seconds. EngineError if it has not answered after `wait` seconds (0: do not wait, just say so)."""
    try:
        return list_models(base_url, api_key)
    except EngineError:
        pass
    print(f"LM Studio's server is not answering at {base_url}.\n"
          f"  -> Open LM Studio and start its server (Developer tab, port 1234).\n"
          f"     Model: {DEFAULT_MODEL}, context length 8192 or more, thinking off (README, Setup).")
    if wait <= 0:
        raise EngineError("not waiting (--wait 0)")
    print(f"Waiting for it: I carry on by myself once it answers (Ctrl+C to stop, I give up after {wait:.0f} s).")
    started = time.monotonic()
    while time.monotonic() - started < wait:
        time.sleep(POLL_SECONDS)
        try:
            models = list_models(base_url, api_key)
        except EngineError:
            print(".", end="", flush=True)
            continue
        print("\nthe server answers.")
        return models
    raise EngineError(f"{base_url} did not answer within {wait:.0f} s")


def _runs(runs_dir: Path) -> set[str]:
    return {p.name for p in runs_dir.glob("*") if (p / "run.json").exists()} if runs_dir.exists() else set()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch, digest, summarize with LM Studio and assemble the brief.")
    ap.add_argument("--model", default="", help=f"model id, or part of one (default: {DEFAULT_MODEL})")
    ap.add_argument("--compression", type=_compression, default=None, metavar="RATIO",
                    help="share of the regular posts' words the brief keeps, as run_system.py's --compression")
    window = ap.add_mutually_exclusive_group(required=True)  # no default: a short brief must be asked for
    window.add_argument("--since-last-run", dest="window", action="store_const", const="since-run",
                        help="only what was fetched since your last digest: a short brief when that was recent")
    window.add_argument("--24h", dest="window", action="store_const", const="24h",
                        help="everything published in the last 24 hours, whatever was digested before")
    ap.add_argument("--no-fetch", action="store_true", help="skip the fetch: the store is fresh already")
    ap.add_argument("--full", action="store_true", help="also write the full digest, as `xmd digest --full`")
    ap.add_argument("--quick", action="store_true", help="also write the quick digest, as `xmd digest --quick`")
    ap.add_argument("--wait", type=float, default=900, metavar="SECONDS",
                    help="how long to wait for LM Studio's server to come up (default 900; 0: do not wait)")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL, help="default: LM Studio")
    ap.add_argument("--api-key", default=os.environ.get(API_KEY_ENV_VAR, ""),
                    help=f"only if the runtime wants one (default: ${API_KEY_ENV_VAR}; a key typed here stays in your shell history)")
    ap.add_argument("--config", default="sources.yaml", help="path to sources.yaml")
    args = ap.parse_args(argv)

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        sys.exit(f"config error: {exc}")
    digests = config.digest_dir

    print("[1/5] LM Studio")
    wanted = args.model or DEFAULT_MODEL
    try:
        models = wait_for_server(args.base_url, args.api_key, args.wait)
        model, note = choose_model(wanted, models)
    except EngineError as exc:
        print(f"\ncannot reach the server, nothing was fetched: {exc}")
        return 1
    except ModelChoiceError as exc:
        print(f"\n{exc}")
        return 2
    if note:  # nothing on the server fits: stop now, not after the digest has used up its window
        print(f"\nno model on the server fits {wanted!r}: download it in LM Studio, or name another with --model. On offer:")
        print("\n".join(f"  {m}" for m in models or []))
        return 2
    print(f"model {model}: checking that it answers ...")
    try:  # loads the model now, and a model that thinks fails here, before anything is fetched
        with Engine(model, args.base_url, args.api_key, WARMUP_TIMEOUT) as engine:
            warm = engine.complete("Reply with one word.", "ok", max_tokens=16)
    except EngineError as exc:
        print(f"the model did not answer, nothing was fetched: {exc}\n"
              "Thinking must be off and the context length 8192 or more (README, Setup).")
        return 1
    print(f"ok in {warm.seconds:.1f} s\n")

    print("[2/5] fetch")
    if args.no_fetch:
        print("skipped (--no-fetch)")
    else:
        cli.main(["--config", args.config, "fetch"])

    print(f"\n[3/5] digest, {WINDOW_WORDS[args.window]}")
    export = cli.run_digest(config, args.window, full=args.full, quick=args.quick)
    if export is None:
        print("no brief to make.")
        return 0

    print("\n[4/5] summarize")
    before = _runs(digests / ".runs")
    code = run_system.main([
        "--export", str(export), "--digests-dir", str(digests), "--model", model, "--base-url", args.base_url,
        *(["--api-key", args.api_key] if args.api_key else []),
        *(["--compression", str(args.compression)] if args.compression is not None else []),
    ])
    new = sorted(_runs(digests / ".runs") - before)
    if not new:  # nothing was run (the server went away): run_system said why
        print(f"\nthe digest is made ({export.name}) but the model step did not finish. To retry from there:\n"
              f"  python scripts\\run_system.py --model {model}\n  python scripts\\assemble_brief.py")
        return code or 1
    if code:
        print("\nsome chunks failed: assembling anyway, their posts appear as one-liners under 'Not summarized'.")

    print("\n[5/5] assemble")
    return max(code, assemble_brief.main([
        "--run", new[-1], "--digests-dir", str(digests), "--config", args.config,
        *(["--api-key", args.api_key] if args.api_key else []),
    ]))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("\nstopped (Ctrl+C)")
