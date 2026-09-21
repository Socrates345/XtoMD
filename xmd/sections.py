"""The summary that opens each section of the brief: what the section was about today.

The chunk summaries (runner.py) say what individual posts said. This reads one section's
bullets together, with how many accounts posted about each and the names that were louder
than usual (topics.py), and says what stood out. What a counter cannot tell, that a quiet
name was big news, only a reader can, so a model writes it, and it is checked like every
other reply: a number, handle or ticker in the summary that its input never had makes it
unusable, and the section simply has no summary.

Answers are cached by their input, so assembling a brief again does not call the model again.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .brief import BriefItem
from .engine import Completion, EngineError
from .prompt import SECTION_SCHEMA
from .topics import Topic
from .verify import support

MIN_BULLETS = 3  # a section with fewer bullets has nothing to sum up
MAX_BULLETS = 60  # bullets sent; a section past this is cut at the least important end
SECTION_TOKENS = 300  # a summary is one or two sentences


class Model(Protocol):
    def complete_json(
        self, system: str, user: str, schema: dict, max_tokens: int, temperature: float,
    ) -> tuple[dict, Completion]: ...


@dataclass(frozen=True)
class SectionSummary:
    text: str | None  # None when there is none to show
    error: str | None = None  # why not


def _accounts(item: BriefItem, mapping: dict) -> int:
    return len({mapping[str(n)]["source"] for n in item.ids})


def section_inputs(kept: list[BriefItem], mapping: dict, topics: dict[str, list[Topic]]) -> dict[str, str]:
    """{section label: the message for its summary}, for the sections with enough bullets."""
    by_section: dict[str, list[BriefItem]] = defaultdict(list)
    for item in kept:
        by_section[item.section].append(item)
    inputs = {}
    for label, items in by_section.items():
        if len(items) < MIN_BULLETS:
            continue
        lines = [f"Section: {label}"]
        if topics.get(label):
            lines.append("Louder than usual: " + ", ".join(
                f"{t.term} ({t.posts} posts from {t.sources} accounts)" for t in topics[label]))
        lines.append("")
        for item in items[:MAX_BULLETS]:
            n = _accounts(item, mapping)
            detail = f" — {item.detail}" if item.detail else ""
            lines.append(f"- {item.headline}{detail} ({n} account{'s' if n != 1 else ''})")
        inputs[label] = "\n".join(lines) + "\n"
    return inputs


def summary_problem(summary: str, message: str) -> str | None:
    """Why a summary cannot be used, or None: a number, handle or ticker in it that its input never had."""
    found = support([{"headline": "", "detail": summary, "ids": [0]}], {0: message})
    if found.flagged:
        return "not in the input: " + ", ".join(found.flagged[0][1])
    return None


def input_hash(system: str, message: str) -> str:
    return hashlib.sha256(f"{system}\0{message}".encode("utf-8")).hexdigest()[:16]


def summarize_sections(
    model: Model, inputs: dict[str, str], system: str, cache: dict[str, str] | None = None,
    temperature: float = 0.2, on_result: Callable[[str, SectionSummary, bool], None] | None = None,
) -> dict[str, SectionSummary]:
    """One call per section, except where `cache` (input hash -> summary, updated in place) has the answer.
    A call that fails, or whose answer has a figure its input lacks, gives a SectionSummary with no text."""
    cache = {} if cache is None else cache
    results: dict[str, SectionSummary] = {}
    for label, message in inputs.items():
        key = input_hash(system, message)
        if key in cache:
            results[label] = SectionSummary(cache[key])
            if on_result:
                on_result(label, results[label], True)
            continue
        try:
            reply, _completion = model.complete_json(system, message, SECTION_SCHEMA, SECTION_TOKENS, temperature)
            text = " ".join(str(reply.get("summary", "")).split())
            problem = summary_problem(text, message) if text else "empty summary"
        except EngineError as exc:
            text, problem = "", str(exc)
        results[label] = SectionSummary(None if problem else text, problem)
        if not problem:
            cache[key] = text
        if on_result:
            on_result(label, results[label], False)
    return results


def load_cache(path: Path) -> dict[str, str]:
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")).get("summaries", {}))
    except (OSError, ValueError):
        return {}


def save_cache(path: Path, cache: dict[str, str]) -> None:
    path.write_text(json.dumps({"summaries": cache}, ensure_ascii=False, indent=1), encoding="utf-8")
