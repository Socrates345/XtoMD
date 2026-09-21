"""Run chunks through an engine and keep what came back.

This is the "map" half of the brief: every chunk of the frozen export goes to the
engine with its band's prompt, and the replies are saved raw, with timing and token
counts, so a run can be verified, assembled and scored later without calling the
model again. A chunk that fails is recorded and the run goes on.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .chunk import Chunk, estimate_tokens
from .engine import Completion, EngineError
from .prompt import BRIEF_SCHEMA
from .tiers import RECAP, REGULAR, RETWEET

DEFAULT_TEMPERATURE = 0.2  # low: the same posts should give nearly the same brief
# tokens one reply item costs, measured on the first real runs: 70-115 for an item with a sentence of detail.
# Plain retweets are themes now, like recap, so they cost the same.
TOKENS_PER_ITEM = {REGULAR: 100, RETWEET: 100, RECAP: 100}
MAX_REPLY_TOKENS = 3200  # with ~4.2K tokens in, that stays inside an 8K context
TRUNCATION_RATIO = 1.3  # a chunk with this many times its run's median characters per token looks cut short
BIG_CHUNK = 0.6  # only chunks this big, against the run's biggest, can be cut by a context window
IMPLAUSIBLE_CHARS_PER_TOKEN = 5.5  # English runs ~4 and posts fewer, so more than this means tokens went missing


class Model(Protocol):
    """What a run needs of an engine: xmd.engine.Engine, or a fake in tests."""

    def complete_json(
        self, system: str, user: str, schema: dict, max_tokens: int, temperature: float,
    ) -> tuple[dict, Completion]: ...


@dataclass
class ChunkResult:
    chunk: Chunk
    max_tokens: int
    prompt_chars: int  # system prompt + user message
    attempts: int = 0
    seconds: float = 0.0  # wall time over all attempts
    reply: dict | None = None  # parsed and shape-checked
    completion: Completion | None = None  # the last attempt's, when it got as far as an answer
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.reply is not None

    @property
    def items(self) -> list[dict]:
        return (self.reply or {}).get("items", [])

    @property
    def reply_words(self) -> int:
        return sum(len(i["headline"].split()) + len(i["detail"].split()) for i in self.items)


def reply_budget(chunk: Chunk) -> int:
    """max_tokens for a chunk's reply: room for its most items plus the JSON around them, never unbounded.
    Counted in items, not words: JSON structure and id lists make a reply cost ~2.5 tokens per word (~4 for
    headline-only items), and a per-word budget cut four of ten chunks off in the first real run."""
    return min(MAX_REPLY_TOKENS, max(500, TOKENS_PER_ITEM[chunk.tier] * chunk.max_items + 300))


def check_reply(reply: dict, chunk: Chunk | None = None) -> str | None:
    """What is wrong with a reply, or None: its shape (a server that doesn't enforce the schema can still
    get it wrong), and with a `chunk`, whether it has enough items to be a summary of it at all."""
    items = reply.get("items")
    if not isinstance(items, list):
        return "no `items` list"
    for k, item in enumerate(items):
        if not isinstance(item, dict):
            return f"item {k} is not an object"
        if not isinstance(item.get("headline"), str) or not isinstance(item.get("detail"), str):
            return f"item {k}: headline and detail must be strings"
        ids = item.get("ids")
        if not isinstance(ids, list) or not all(isinstance(n, int) and not isinstance(n, bool) for n in ids):
            return f"item {k}: ids must be a list of integers"
    if chunk is not None and len(items) < chunk.min_items:
        return f"too thin: {len(items)} item(s) for {len(chunk.posts)} posts, at least {chunk.min_items} expected"
    return None


def run_chunks(
    engine: Model,
    chunks: list[Chunk],
    system_for: Callable[[str], str],
    schema: dict | Callable[[Chunk], dict] = BRIEF_SCHEMA,
    retries: int = 1,
    temperature: float = DEFAULT_TEMPERATURE,
    on_result: Callable[[ChunkResult], None] | None = None,
) -> list[ChunkResult]:
    """One call per chunk, in order. `system_for(band)` is the band's system prompt; `schema` is the
    reply schema, or a function of the chunk (see prompt.bounded_schema). A failed attempt (engine
    error, cut-off, wrong shape, too few items) is retried up to `retries` times."""
    results = []
    for chunk in chunks:
        system, user = system_for(chunk.band), chunk.render()
        chunk_schema = schema(chunk) if callable(schema) else schema
        result = ChunkResult(chunk, reply_budget(chunk), len(system) + len(user))
        start = time.perf_counter()
        for _ in range(retries + 1):
            result.attempts += 1
            try:
                reply, completion = engine.complete_json(system, user, chunk_schema, result.max_tokens, temperature)
            except EngineError as exc:
                result.error, result.completion = str(exc), exc.completion or result.completion
                continue
            result.completion = completion
            problem = check_reply(reply, chunk)
            if problem:
                result.error = f"unusable reply: {problem}"
                continue
            result.reply, result.error = reply, None
            break
        result.seconds = time.perf_counter() - start
        results.append(result)
        if on_result:
            on_result(result)
    return results


def summarize(results: list[ChunkResult]) -> dict:
    """Totals for a run: counts, model time, tokens, reply length against target, characters per token."""
    counted = [r for r in results if r.completion and r.completion.prompt_tokens]
    tokens_in = sum(r.completion.prompt_tokens for r in counted)
    return {
        "chunks": len(results),
        "ok": sum(r.ok for r in results),
        "failed": sum(not r.ok for r in results),
        "seconds": round(sum(r.seconds for r in results), 1),
        "prompt_tokens": tokens_in,
        "completion_tokens": sum(r.completion.completion_tokens or 0 for r in results if r.completion),
        "reply_words": sum(r.reply_words for r in results if r.ok),
        "target_words": sum(r.chunk.target_words for r in results),
        "chars_per_token": round(sum(r.prompt_chars for r in counted) / tokens_in, 2) if tokens_in else None,
    }


def suspect_truncation(results: list[ChunkResult]) -> list[str]:
    """Ids of large chunks that reached the model with far fewer tokens per character than the
    run's other large chunks: a context window that is too small cuts the prompt without an
    error. Small chunks are left out (mostly system prompt, so their ratio differs, and they
    can't overflow). A window that cuts every large chunk alike shows only in the run-wide
    characters per token (see IMPLAUSIBLE_CHARS_PER_TOKEN)."""
    counted = [r for r in results if r.completion and r.completion.prompt_tokens]
    if not counted:
        return []
    biggest = max(r.prompt_chars for r in counted)
    ratios = {
        r.chunk.id: r.prompt_chars / r.completion.prompt_tokens
        for r in counted if r.prompt_chars >= BIG_CHUNK * biggest
    }
    if len(ratios) < 3:
        return []
    typical = statistics.median(ratios.values())
    return [cid for cid, ratio in ratios.items() if ratio > TRUNCATION_RATIO * typical]


def write_run(run_dir: Path, manifest: dict, results: list[ChunkResult]) -> None:
    """One JSON file per chunk (the raw reply included) and run.json: the manifest, totals and a table."""
    run_dir.mkdir(parents=True, exist_ok=True)
    for result in results:
        (run_dir / f"{result.chunk.id}.json").write_text(
            json.dumps(_record(result), ensure_ascii=False, indent=1), encoding="utf-8"
        )
    table = [
        {
            "chunk": r.chunk.id, "ok": r.ok, "seconds": round(r.seconds, 1),
            "prompt_tokens": r.completion.prompt_tokens if r.completion else None,
            "completion_tokens": r.completion.completion_tokens if r.completion else None,
            "items": len(r.items), "reply_words": r.reply_words if r.ok else None,
            "target_words": r.chunk.target_words,
        }
        for r in results
    ]
    (run_dir / "run.json").write_text(
        json.dumps({**manifest, "summary": summarize(results), "chunks": table}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def compression_label(manifest: dict) -> str:
    """How a saved run was sized, for a header line: "compression 0.30". A run saved while this was still
    `--scenario A|B` has no ratio in its manifest and says "scenario A"."""
    if "compression_ratio" in manifest:
        return f"compression {manifest['compression_ratio']:.2f}"
    return f"scenario {manifest['scenario']['name']}"


def _record(result: ChunkResult) -> dict:
    chunk, completion = result.chunk, result.completion
    return {
        "chunk": chunk.id,
        "tier": chunk.tier,
        "level": chunk.level,
        "ids": [p.n for p in chunk.posts],
        "input_words": sum(p.words for p in chunk.posts),
        "target_words": chunk.target_words,
        "min_items": chunk.min_items,
        "max_items": chunk.max_items,
        "estimated_tokens": estimate_tokens(chunk.render()),
        "max_tokens": result.max_tokens,
        "attempts": result.attempts,
        "seconds": round(result.seconds, 2),
        "ok": result.ok,
        "error": result.error,
        "completion": None if completion is None else {
            "finish": completion.finish,
            "prompt_tokens": completion.prompt_tokens,
            "completion_tokens": completion.completion_tokens,
            "reasoning_chars": completion.reasoning_chars,
            "seconds": round(completion.seconds, 2),
        },
        "reply": result.reply,
        "raw_text": completion.text if completion else None,
    }
