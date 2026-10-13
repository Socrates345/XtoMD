"""From a fresh fetch to the brief in one command: `xmd fetch`, `xmd digest`, `run_system.py`, `assemble_brief.py`.

    python scripts\\make_brief.py --24h                # the last 24 hours, with qwen/qwen3.5-9b
    python scripts\\make_brief.py --since-last-run     # only what is new since your last digest
    python scripts\\make_brief.py --24h --model qwen   # another model, or part of a name
    python scripts\\make_brief.py --24h --no-fetch     # the store is fresh already
    python scripts\\make_brief.py --24h --time 10       # a brief you can read in about 10 minutes

You must choose the window: `--since-last-run` covers what was fetched since your previous digest, so it is
a short brief when that was recent; `--24h` covers everything published in the last 24 hours, whatever was
digested before. Either one moves the since-run cursor, but only once its brief is written: a run that stops
before leaves the cursor where it was, so the next `--since-last-run` brief still covers its posts.

The one thing it cannot do for you is LM Studio. It checks the server first and, if it is not answering, says
what to open and waits: the moment the server is up it carries on by itself. It checks before it fetches, so
that nothing is fetched or digested for a brief that could not be written.

If it stops after the digest (LM Studio went away), the export is kept: run `run_system.py` and then
`assemble_brief.py` to finish, as the message says (the next `--since-last-run` brief then repeats those posts).
Run it from the repo root, like the other scripts.

Away from the laptop the model is a hosted one (docs/remote.md): `--base-url`, `--model` and `--extra-body`, or
`XMD_LLM_BASE_URL`, `XMD_LLM_MODEL` and `XMD_LLM_EXTRA_BODY` in the environment, name it. A server that is not on
this machine is not waited for: it answers or the run stops, before anything is fetched.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run from a checkout, installed or not

import assemble_brief  # noqa: E402  (the scripts next to this one)
import run_system  # noqa: E402
from xmd import cli  # noqa: E402
from xmd.core.config import load_config  # noqa: E402
from xmd.summary.engine import (  # noqa: E402
    API_KEY_ENV_VAR, BASE_URL_ENV_VAR, DEFAULT_BASE_URL, EXTRA_BODY_ENV_VAR, MODEL_ENV_VAR, Engine, EngineError,
    ModelChoiceError, choose_model, list_models, on_this_machine, refusal_hint,
)

DEFAULT_MODEL = "qwen/qwen3.5-9b"  # the setup the README describes and tests
POLL_SECONDS = 3  # between two looks at a server that is not up yet
WARMUP_TIMEOUT = 600  # a model that has to be loaded first is slow to answer once
WINDOW_WORDS = {"since-run": "only what is new since your last digest", "24h": "everything from the last 24 hours"}


def wait_for_server(base_url: str, api_key: str, wait: float) -> list[str] | None:
    """The server's model listing (None: it answers but lists none). While it is down: say what to open, then
    look every few seconds. EngineError if it has not answered after `wait` seconds (0: do not wait, just say so),
    or at once when the server is not on this machine: there is nothing to open, and a run nobody watches (a
    timer, a phone) should not sit there."""
    try:
        return list_models(base_url, api_key)
    except EngineError:
        if not on_this_machine(base_url):
            raise
    print(f"LM Studio's server is not answering at {base_url}.\n"
          f"  -> Open LM Studio and start its server (Developer tab, port 1234).\n"
          f"     Model: {DEFAULT_MODEL}, context length 8192 or more, thinking off (README, LM Studio setup).")
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
    ap.add_argument("--model", default=os.environ.get(MODEL_ENV_VAR, ""),
                    help=f"model id, or part of one (default: ${MODEL_ENV_VAR}, else {DEFAULT_MODEL})")
    ap.add_argument("--time", type=run_system.time_budget, default=None, metavar="MIN",
                    help="the reading time to fit the brief to, in minutes (10, 30...); without it the brief is as "
                         "long as the day was")
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
    ap.add_argument("--base-url", default=os.environ.get(BASE_URL_ENV_VAR) or DEFAULT_BASE_URL,
                    help=f"default: ${BASE_URL_ENV_VAR}, else LM Studio")
    ap.add_argument("--api-key", default=os.environ.get(API_KEY_ENV_VAR, ""),
                    help=f"only if the runtime wants one (default: ${API_KEY_ENV_VAR}; a key typed here stays in your shell history)")
    ap.add_argument("--extra-body", type=run_system.extra_body, default=os.environ.get(EXTRA_BODY_ENV_VAR, ""),
                    metavar="JSON", help=f"a JSON object of fields added to every request, for what one provider "
                                         f"wants and no other (default: ${EXTRA_BODY_ENV_VAR}; docs/remote.md has Venice's)")
    ap.add_argument("--config", default="sources.yaml", help="path to sources.yaml")
    args = ap.parse_args(argv)

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        sys.exit(f"config error: {exc}")
    digests = config.digest_dir

    local = on_this_machine(args.base_url)
    print("[1/5] LM Studio" if local else "[1/5] model server")
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
        print(f"\nno model on the server fits {wanted!r}: {'download it in LM Studio, or ' if local else ''}"
              "name another with --model. On offer:")
        print("\n".join(f"  {m}" for m in models or []))
        return 2
    print(f"model {model}: checking that it answers ...")
    try:  # loads the model now, and a model that thinks fails here, before anything is fetched
        with Engine(model, args.base_url, args.api_key, WARMUP_TIMEOUT, extra_body=args.extra_body) as engine:
            warm = run_system.warm_up(engine)
    except EngineError as exc:
        print(f"the model did not answer, nothing was fetched: {exc}\n"
              + (refusal_hint(exc.status) or
                 ("Thinking must be off and the context length 8192 or more (README, LM Studio setup)." if local else
                  "Thinking must be off: a hosted model takes its own switch for that in --extra-body (docs/remote.md).")))
        return 1
    print(f"ok in {warm.seconds:.1f} s\n")

    print("[2/5] fetch")
    if args.no_fetch:
        print("skipped (--no-fetch)")
    else:
        cli.main(["--config", args.config, "fetch"])

    print(f"\n[3/5] digest, {WINDOW_WORDS[args.window]}")
    since = cli.digest_cursor(config)
    export = cli.run_digest(config, args.window, full=args.full, quick=args.quick)
    if export is None:
        print("no brief to make.")
        return 0
    # The digest moved the since-run cursor. It goes back where it was until this digest's brief exists: a run that
    # stops before (the model server went away, the machine was restarted) leaves its posts to the next "since" brief.
    digested = cli.digest_cursor(config)
    if since:
        cli.set_digest_cursor(config, since)

    print("\n[4/5] summarize")
    before = _runs(digests / ".runs")
    code = run_system.main([
        "--export", str(export), "--digests-dir", str(digests), "--model", model, "--base-url", args.base_url,
        "--config", args.config,
        *(["--api-key", args.api_key] if args.api_key else []),
        *(["--extra-body", json.dumps(args.extra_body)] if args.extra_body else []),
        *(["--time", f"{args.time:g}"] if args.time is not None else []),
    ])
    new = sorted(_runs(digests / ".runs") - before)
    if not new:  # nothing was run (the server went away): run_system said why
        print(f"\nthe digest is made ({export.name}) but the model step did not finish. To retry from there:\n"
              f"  python scripts\\run_system.py --model {model}\n  python scripts\\assemble_brief.py")
        return code or 1
    if code:
        print("\nsome chunks failed: assembling anyway, their posts appear as one-liners under 'Not summarized'.")

    print("\n[5/5] assemble")
    assembled = assemble_brief.main([
        "--run", new[-1], "--digests-dir", str(digests), "--config", args.config,
        *(["--api-key", args.api_key] if args.api_key else []),
    ])
    if not assembled and digested:  # the brief is written: "since" now starts after its digest
        cli.set_digest_cursor(config, digested)
    return max(code, assembled)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("\nstopped (Ctrl+C)")
