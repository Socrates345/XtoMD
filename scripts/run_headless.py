"""Run the brief pipeline (or a smoke test) against LM Studio without opening its window: starts the
server and loads the model with LM Studio's own `lms` CLI, runs the pipeline, then unloads the model
and stops the server again -- even if the pipeline fails or the run is interrupted.

    python scripts\\run_headless.py --smoke                      # ~1 minute sanity check, no brief written
    python scripts\\run_headless.py --24h                        # a real brief; LM Studio's window is never opened
    python scripts\\run_headless.py --since-last-run --no-fetch  # any make_brief.py flag is passed through

Needs `lms` on PATH (it ships with the LM Studio desktop app; run the app at least once
first) and Thinking already unticked once for the model in the app (README, LM Studio
setup): this script only drives LM Studio, it does not configure it. `lms load` does carry
that setting over (confirmed 2026-09-22) -- but re-check with --smoke after changing the
model or its LM Studio config: an empty reply plus a "thinking" hint in the output means
it didn't carry over this time.

`--port` controls where the server is started and what base URL the pipeline is given;
pass a `--base-url` meant for the pipeline itself and it is silently overridden by that,
since this script is the one starting the server.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run from a checkout, installed or not

import make_brief  # noqa: E402  (the scripts next to this one)
import run_system  # noqa: E402
import smoke_engine  # noqa: E402
from xmd.summary.engine import API_KEY_ENV_VAR, EngineError, list_models  # noqa: E402

DEFAULT_PORT = 1234
DEFAULT_TTL = 1800  # seconds LM Studio keeps the model loaded with no calls: a backstop if this script dies mid-run
LOAD_TIMEOUT = 300  # a cold disk read of a several-GB model can be slow the first time
LMS_MISSING = 3  # distinct from the pipeline's own 0/1/2 (run_system.py, make_brief.py): needs a one-time setup step, not a retry


def _lms(path: str) -> str:
    found = path or shutil.which("lms")
    if not found:
        print(
            "lms (LM Studio's CLI) is not on PATH.\n"
            "  -> Run the LM Studio desktop app at least once (lms ships bundled with it and is added to\n"
            "     PATH then), or pass --lms-path. Docs: https://lmstudio.ai/docs/cli"
        )
        sys.exit(LMS_MISSING)
    return found


def _call(lms: str, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    # lms writes UTF-8 (spinner glyphs included); Windows' default locale codec (cp1252) chokes on it, so
    # the encoding is pinned here rather than left to `text=True`'s platform default.
    return subprocess.run(
        [lms, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )


def _server_up(base_url: str, api_key: str) -> bool:
    try:
        list_models(base_url, api_key)
        return True
    except EngineError:
        return False


def _warn_if_other_model_loaded(lms: str, model: str) -> None:
    """Best effort: `lms ps`'s exact output shape isn't documented, so this only ever warns, never blocks."""
    try:
        ps = _call(lms, "ps", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return
    if ps.returncode == 0 and ps.stdout.strip() and model not in ps.stdout:
        print(f"[lms] note: something other than {model} may already be loaded (8 GB VRAM likely can't hold two):\n{ps.stdout}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Start LM Studio headlessly via its `lms` CLI, run the brief pipeline (or --smoke), stop it again.",
    )
    ap.add_argument("--model", default=make_brief.DEFAULT_MODEL,
                    help=f"model id for `lms load` and the pipeline (default: {make_brief.DEFAULT_MODEL})")
    ap.add_argument("--context-length", type=int, default=run_system.CONTEXT_TOKENS,
                    help=f"passed to `lms load`; the pipeline sizes every call assuming this much is loaded "
                         f"(default: {run_system.CONTEXT_TOKENS})")
    ap.add_argument("--gpu", default="", help="passed to `lms load --gpu` (0-1, off, max); omitted by default "
                    "to keep the GPU offload already tuned in LM Studio")
    ap.add_argument("--ttl", type=float, default=DEFAULT_TTL,
                    help=f"`lms load --ttl`: LM Studio unloads on its own after this many idle seconds, in case "
                         f"this script cannot (default: {DEFAULT_TTL:.0f})")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"LM Studio's server port (default: {DEFAULT_PORT})")
    ap.add_argument("--lms-path", default="", help="path to lms(.exe) if it is not on PATH")
    ap.add_argument("--api-key", default=os.environ.get(API_KEY_ENV_VAR, ""),
                    help=f"only if the runtime wants one (default: ${API_KEY_ENV_VAR})")
    ap.add_argument("--smoke", action="store_true", help="run smoke_engine.py instead of make_brief.py: "
                    "~1 minute, no brief written")
    args, rest = ap.parse_known_args(argv)  # unrecognized flags (--24h, --compression, ...) pass straight through

    lms = _lms(args.lms_path)
    base_url = f"http://127.0.0.1:{args.port}/v1"

    print(f"=== lms: server (port {args.port}) + model ({args.model}) ===")
    started_server = False
    if _server_up(base_url, args.api_key):
        print("server already up: leaving it running when done")
    else:
        result = _call(lms, "server", "start", "--port", str(args.port), "--bind", "127.0.0.1")
        if result.returncode != 0 and not _server_up(base_url, args.api_key):
            sys.exit(f"lms server start failed:\n{result.stdout}{result.stderr}")
        started_server = True

    _warn_if_other_model_loaded(lms, args.model)
    print(f"loading {args.model} (context {args.context_length}, ttl {args.ttl:.0f}s"
          f"{f', gpu {args.gpu}' if args.gpu else ''})")
    load_args = ["load", args.model, "--context-length", str(args.context_length), "--ttl", str(args.ttl)]
    if args.gpu:
        load_args += ["--gpu", args.gpu]
    try:
        result = _call(lms, *load_args, timeout=LOAD_TIMEOUT)
    except subprocess.TimeoutExpired:
        print(f"lms load did not finish within {LOAD_TIMEOUT:.0f}s (a cold disk read can be slow the first time)")
        if started_server:
            _call(lms, "server", "stop")
        return 1
    if result.returncode != 0:
        print(result.stdout + result.stderr)
        if started_server:
            _call(lms, "server", "stop")
        return 1

    forwarded = [*rest, "--model", args.model, "--base-url", base_url]
    if args.api_key:
        forwarded += ["--api-key", args.api_key]

    try:
        if args.smoke:
            print("\n=== smoke_engine.py ===\n")
            code = smoke_engine.main(forwarded)
        else:
            print("\n=== make_brief.py ===\n")
            code = make_brief.main(forwarded)
    finally:
        print(f"\n=== lms: teardown ===\nunloading {args.model}")
        try:
            _call(lms, "unload", args.model, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"unload failed, leaving it to --ttl: {exc}")
        if started_server:
            print("stopping the server")
            try:
                _call(lms, "server", "stop", timeout=60)
            except (OSError, subprocess.SubprocessError) as exc:
                print(f"server stop failed: {exc}")

    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("\nstopped (Ctrl+C)")
