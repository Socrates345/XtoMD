"""Smoke test for a model runtime: Phase 2 (docs/digest-summary.md).

Asks an OpenAI-compatible endpoint three things: does it answer, does it honour
a JSON-schema reply in the shape the brief stage will use, and how long does one
full-size chunk (~5K tokens in, ~1K out) take. Everything sent is synthetic — no
tweets, no follow list — so it is safe to point at any engine.

Defaults to LM Studio's local server (Developer tab -> start the server, port
1234). Switching models is just a different --model: part of the name is enough,
and --list shows what the server offers. Turn on Just-In-Time loading in LM
Studio so a named model is loaded on demand, and give it a context length of at
least 8192 in its load settings.

    .venv\\Scripts\\python scripts\\smoke_engine.py --list
    .venv\\Scripts\\python scripts\\smoke_engine.py --model qwen
    .venv\\Scripts\\python scripts\\smoke_engine.py --model qwen --no-think
    .venv\\Scripts\\python scripts\\smoke_engine.py --base-url http://127.0.0.1:8910/v1   # Bonsai's Prism runtime

Thinking models (Qwen3.5) reason before answering and LM Studio keeps that apart
from the reply, so the reply looks empty when the token cap runs out first. The
FAIL line says what it saw; a hint at the end says how to switch thinking off.

Eject other models first so the VRAM lines are for this one alone ("VRAM before"
is the baseline); watch Task Manager for RAM. VRAM is read from nvidia-smi.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from typing import NamedTuple

import httpx

# the reply shape the brief stage will ask every engine for (plan §5)
SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "headline": {"type": "string"},
                    "detail": {"type": "string"},
                    "ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["headline", "detail", "ids"],
            },
        }
    },
    "required": ["items"],
}
SYSTEM = "You condense numbered social-media posts into a short brief. Reply with JSON only."

# calls per export: what xmd/summary/chunk.py makes of the frozen 24h export (10; tiers are never mixed),
# and roughly three times the posts for three days (~27, the per-tier remainders don't triple)
CALLS_24H, CALLS_3D = 10, 27
PLANNED_OUT = 1200  # tokens a real chunk's reply runs to (plan §4: ~5K in, ~1.2K out)

_SUBJECTS = [
    "Nordvik Semiconductor", "the Harlow city council", "Kestrel Motors",
    "Aurelia Bank", "the Tamsin research group", "Orbital Freight Co",
    "the Lindqvist ministry", "Pelagic Energy",
]
_VERBS = ["announces", "delays", "confirms", "denies", "expands", "cuts", "reports", "signs"]
_OBJECTS = [
    "a new factory in {place}", "guidance for the coming quarter",
    "a {n}-year supply deal", "a review of {place} port fees",
    "{n} job cuts", "a recall of {n}k vehicles",
]
_PLACES = ["Rotterdam", "Osaka", "Lagos", "Tallinn", "Recife", "Perth"]


def _posts(count: int) -> str:
    """`count` fake numbered posts; the combinations repeat, like a trending story."""
    lines = []
    for i in range(1, count + 1):
        obj = _OBJECTS[(i * 5) % len(_OBJECTS)].format(place=_PLACES[i % len(_PLACES)], n=10 + i % 40)
        lines.append(
            f"[{i}] @user{i % 23} {_SUBJECTS[i % len(_SUBJECTS)]} "
            f"{_VERBS[(i * 3) % len(_VERBS)]} {obj}, according to a statement on day {i % 9 + 1}."
        )
    return "\n".join(lines)


def _prompt(count: int, most: int) -> str:
    return (
        f"Posts:\n{_posts(count)}\n\n"
        f'Return {{"items": [{{"headline", "detail", "ids"}}]}}: one item per story, at most {most}, '
        "ids = the post numbers it draws on. Use only facts stated in the posts."
    )


class Reply(NamedTuple):
    text: str  # message.content
    finish: str | None  # "stop", or "length" when the token cap cut it off
    usage: dict
    seconds: float
    thought: int  # chars of reasoning the server kept apart from `content` (LM Studio: reasoning_content)


def _chat(
    client: httpx.Client, base: str, model: str, user: str, schema: dict | None, max_tokens: int,
    no_think: bool = False,
) -> Reply:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    if schema:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "brief", "strict": True, "schema": schema},
        }
    if no_think:  # Qwen's documented switch; Qwen3.5 has no /no_think soft switch
        body["chat_template_kwargs"] = {"enable_thinking": False}
    start = time.perf_counter()
    resp = client.post(f"{base}/chat/completions", json=body)
    seconds = time.perf_counter() - start
    resp.raise_for_status()
    data = resp.json()
    choice = data["choices"][0]
    message = choice["message"]
    thought = message.get("reasoning_content") or message.get("reasoning") or ""
    return Reply(message.get("content") or "", choice.get("finish_reason"), data.get("usage") or {}, seconds, len(thought))


def _why(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}: {exc.response.text[:300]}"
    return f"{type(exc).__name__}: {exc}"


def _models(client: httpx.Client, base: str) -> list[str] | None:
    """Model ids the server offers (embedding models dropped); None if it has no listing.

    A connection failure is left to raise, so the caller can tell "server down"
    from "server without /models"."""
    resp = client.get(f"{base}/models")
    if resp.status_code != 200:
        return None
    try:
        ids = [m["id"] for m in resp.json()["data"]]
    except (ValueError, KeyError, TypeError):
        return None
    return [i for i in ids if "embed" not in i.lower()]


def _choose(wanted: str, models: list[str] | None) -> str | None:
    """The model id to send for --model `wanted`; None (after saying why) if it can't be decided."""
    if models is None:  # nothing to check against: send the name as given
        if wanted:
            return wanted
        print("the server has no model listing: name one with --model")
        return None
    if not models:
        print("the server lists no models: download one in LM Studio (and turn on Just-In-Time loading)")
        return None
    if not wanted:
        if len(models) == 1:
            return models[0]
        print("several models on offer, pick one with --model (part of the name is enough):")
        print("\n".join(f"  {m}" for m in models))
        return None
    if wanted in models:
        return wanted
    hits = [m for m in models if wanted.lower() in m.lower()]
    if len(hits) == 1:
        return hits[0]
    if hits:
        print(f"--model {wanted!r} fits {len(hits)} models, be more specific:")
        print("\n".join(f"  {m}" for m in hits))
        return None
    print(f"note: no listed model fits {wanted!r}, sending it as given. On offer:")
    print("\n".join(f"  {m}" for m in models))
    return wanted


def _brief_problem(text: str, posts: int) -> str | None:
    """None if `text` is a brief in the agreed shape, else what is wrong with it."""
    try:
        items = json.loads(text)["items"]
        bad = 0
        for item in items:
            if not isinstance(item["headline"], str) or not isinstance(item["detail"], str):
                raise TypeError("headline/detail must be strings")
            bad += sum(1 for n in item["ids"] if not isinstance(n, int) or not 1 <= n <= posts)
    except (ValueError, KeyError, TypeError) as exc:
        return f"not the requested JSON ({type(exc).__name__}); reply starts {text[:60]!r}"
    if not items:
        return "valid JSON but no items"
    if bad:
        return f"{bad} cited id(s) outside 1..{posts}"
    return None


def _empty_note(reply: Reply) -> str:
    """What an empty reply looked like: a thinking model can spend its whole token budget reasoning."""
    return f"empty reply (finish_reason={reply.finish}, {reply.thought} chars of reasoning)"


# printed once, when a reply came back empty because the token cap ran out
THINKING_HINT = """\
thinking is probably on: the model spends its tokens reasoning before it answers.
  LM Studio: untick Thinking in the model's settings, then reload the model.
  --no-think sends chat_template_kwargs {"enable_thinking": false}, Qwen's own documented
  switch, if your server honours it."""


def _problem(reply: Reply, posts: int) -> str | None:
    """What is wrong with a structured reply, or None."""
    return _empty_note(reply) if not reply.text.strip() else _brief_problem(reply.text, posts)


def _vram() -> str:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return "nvidia-smi not found"
    try:
        out = subprocess.run(
            [exe, "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        used, total = (int(x) for x in out.strip().splitlines()[0].split(","))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return "unavailable"
    return f"{used} / {total} MiB"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Smoke-test an OpenAI-compatible model endpoint.")
    ap.add_argument("--model", default="", help="model id, or part of one (case-insensitive); "
                    "omit it when the server offers just one")
    ap.add_argument("--list", action="store_true", help="print the models the server offers and exit")
    ap.add_argument("--base-url", default="http://127.0.0.1:1234/v1", help="default: LM Studio")
    # (this script stands alone, so it names the variable itself: xmd.summary.engine.API_KEY_ENV_VAR)
    ap.add_argument("--api-key", default=os.environ.get("XMD_LLM_API_KEY", ""),
                    help="only if the runtime wants one (default: $XMD_LLM_API_KEY; a key typed here stays in your shell history)")
    ap.add_argument("--chunk-posts", type=int, default=150, help="posts in the full-chunk test (~5K tokens)")
    ap.add_argument("--timeout", type=float, default=600, help="seconds per call")
    ap.add_argument("--no-think", action="store_true",
                    help="send chat_template_kwargs enable_thinking=false (Qwen's switch; not every server honours it)")
    args = ap.parse_args(argv)

    base = args.base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
    failed = False

    with httpx.Client(headers=headers, timeout=args.timeout) as client:
        try:
            models = _models(client, base)
        except httpx.TransportError as exc:
            print(f"cannot reach {base}: {type(exc).__name__}: {exc}")
            print("LM Studio: Developer tab -> start the server (port 1234). Another runtime: pass --base-url.")
            return 1
        if args.list:
            print("\n".join(models) if models else "no models listed")
            return 0
        model = _choose(args.model, models)
        if model is None:
            return 2
        print(f"engine  {base}   model {model}")
        if args.no_think:
            print("thinking: off requested (chat_template_kwargs)")
        print(f"VRAM before: {_vram()}\n")
        ask = {"no_think": args.no_think}
        replies: list[Reply] = []
        gen_rate = 0.0  # tokens/s, from check 2 (short prompt, so close to pure generation)

        # 1. it answers (the first call also loads the model, so its time isn't speed)
        try:
            reply = _chat(client, base, model, "Reply with the single word: ok", None, 16, **ask)
        except httpx.HTTPError as exc:
            print(f"1. endpoint     FAIL  {_why(exc)}")
            if isinstance(exc, httpx.HTTPStatusError):
                print("                model not loaded? Load it in LM Studio, or turn on Just-In-Time loading.")
            return 1
        replies.append(reply)
        problem = None if reply.text.strip() else _empty_note(reply)
        print(f"1. endpoint     {'OK  ' if not problem else 'FAIL'}  "
              f"reply {reply.text.strip()[:40]!r} in {reply.seconds:.1f} s (first call: includes loading the model)"
              + (f"   {problem}" if problem else ""))
        failed |= bool(problem)

        # 2. a short structured reply: schema honoured, plus generation speed
        try:
            reply = _chat(client, base, model, _prompt(12, 5), SCHEMA, 700, **ask)
        except httpx.HTTPError as exc:
            print(f"2. json schema  FAIL  {_why(exc)}")
            failed = True
        else:
            replies.append(reply)
            out = reply.usage.get("completion_tokens") or len(reply.text) // 4
            gen_rate = out / reply.seconds
            problem = _problem(reply, 12)
            print(f"2. json schema  {'OK  ' if not problem else 'FAIL'}  "
                  f"{out} tokens out in {reply.seconds:.1f} s = {out / reply.seconds:.1f} tok/s"
                  + (f"   {problem}" if problem else ""))
            failed |= bool(problem)

        # 3. a full-size chunk: what one real call will cost, and whether the context holds it
        # a nonce first, so a prompt cached by an earlier run can't make this look faster than a fresh chunk
        user = f"Batch {uuid.uuid4().hex[:8]}\n\n" + _prompt(args.chunk_posts, 20)
        try:
            reply = _chat(client, base, model, user, SCHEMA, 1500, **ask)
        except httpx.HTTPError as exc:
            print(f"3. full chunk   FAIL  {_why(exc)}")
            failed = True
        else:
            replies.append(reply)
            tokens_in, tokens_out = reply.usage.get("prompt_tokens"), reply.usage.get("completion_tokens")
            problem = _problem(reply, args.chunk_posts)
            if tokens_in and tokens_in < 0.7 * len(user) / 4:
                # servers with a small default context cut the prompt without an error
                problem = (f"context too small: sent ~{len(user) // 4} tokens, server counted {tokens_in}, "
                           "so the prompt was silently cut (raise the model's context length in LM Studio's load settings)")
            print(f"3. full chunk   {'OK  ' if not problem else 'FAIL'}  "
                  f"{tokens_in or 'n/a'} tokens in, {tokens_out or 'n/a'} out, {reply.seconds:.0f} s measured"
                  + (f"   {problem}" if problem else ""))
            per_call = reply.seconds
            if tokens_out and tokens_out < PLANNED_OUT:  # a real chunk's reply is longer
                # check 2's rate can be a slow first sample; the reply can't have run slower than tokens / total time
                per_call += (PLANNED_OUT - tokens_out) / max(gen_rate, tokens_out / reply.seconds)
            print(f"                ~{per_call:.0f} s per call (a real reply runs ~{PLANNED_OUT} tokens): "
                  f"24h export (~{CALLS_24H} calls) ~ {per_call * CALLS_24H / 60:.1f} min, "
                  f"3-day (~{CALLS_3D} calls) ~ {per_call * CALLS_3D / 60:.1f} min")
            if reply.finish == "length":
                print("                note: the reply hit the 1500-token cap")
            failed |= bool(problem)
        if any(not r.text.strip() and r.finish == "length" for r in replies):
            print(f"\n{THINKING_HINT}")
        print(f"\nVRAM after (model still loaded): {_vram()}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
