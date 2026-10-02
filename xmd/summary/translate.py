"""Translate the posts the brief shows word for word, when they are in a language the reader does not read.

The model's own text (summary bullets, section summaries) is told which language to write in by its prompt
(prompt.brief_language_rule). Priority tweets, image captions, trending stories and the one-liners of a failed
chunk never go through a summary, so they come here, at brief time and never before: the model first tags each
text with its language, in batches, then each one outside `language.accepted` is translated into
`language.translate_to`, one call apiece. The brief shows the translation with the original quoted under it, and
nothing is written back: the store and the digests keep what was posted.

Nothing the model writes is taken on trust here either: a translation that is empty, the original again, or cut
off is dropped (the post stays as posted), and a number, handle or ticker the original lacks gets a visible ⚠.
Answers are cached by their input, like the section summaries, so assembling a brief again costs no calls.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from .engine import Completion, EngineError
from .prompt import load_detect_prompt, load_translate_prompt
from .verify import support
from ..core.config import Language
from ..core.models import URL

DETECT_SCHEMA = {
    "type": "object",
    "properties": {
        "posts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"n": {"type": "integer"}, "lang": {"type": "string"}},
                "required": ["n", "lang"],
            },
        }
    },
    "required": ["posts"],
}
TRANSLATE_SCHEMA = {
    "type": "object",
    "properties": {"translation": {"type": "string"}},
    "required": ["translation"],
}

DETECT_BATCH = 25  # texts tagged per call
DETECT_CHARS = 300  # a text's start is enough to tell its language
DETECT_TOKENS_PER_TEXT = 15  # one {"n": 12, "lang": "en"} entry
MAX_TRANSLATE_CHARS = 3000  # a longer post would not fit an 8K context with its translation: it stays as posted
MIN_LETTERS = 4  # a text with fewer letters than this, links, handles and tags left out, has nothing to translate
_NOT_WORDS = re.compile(r"[@#$]\w+")
_CODE = re.compile(r"^[a-z]{2,3}$")


class Model(Protocol):
    def complete_json(
        self, system: str, user: str, schema: dict, max_tokens: int, temperature: float,
    ) -> tuple[dict, Completion]: ...


@dataclass(frozen=True)
class Translation:
    lang: str  # the ISO 639-1 code the original was tagged with
    text: str  # the original in the reader's language
    unsupported: tuple[str, ...] = ()  # numbers, handles, tickers in `text` that the original lacks


@dataclass
class Translated:
    """What translate_texts did: the translations by original text, and counts for the report."""

    translations: dict[str, Translation] = field(default_factory=dict)
    checked: int = 0  # texts whose language was asked or cached
    foreign: int = 0  # of those, the ones in a language the reader doesn't read
    failed: list[str] = field(default_factory=list)  # why a call gave nothing usable, one line each
    too_long: int = 0  # foreign texts left as posted for their length


def worth_checking(text: str) -> bool:
    """Whether `text` has words to translate at all once its links, @handles, #tags and $tickers are out."""
    return sum(ch.isalpha() for ch in _NOT_WORDS.sub(" ", URL.sub(" ", text))) >= MIN_LETTERS


def _key(system: str, text: str) -> str:
    return hashlib.sha256(f"{system}\0{text}".encode("utf-8")).hexdigest()[:16]


def _code(value: object) -> str:
    """"zh-CN", " EN " -> "zh", "en"; "" for anything that is not a language code."""
    code = re.split(r"[-_]", str(value).strip().lower(), maxsplit=1)[0]
    return code if _CODE.match(code) else ""


def _same(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def _detect(model: Model, texts: list[str], system: str, cache: dict[str, str], done: Translated) -> dict[str, str]:
    """{text: language code} for every text the model tagged (or the cache had)."""
    langs: dict[str, str] = {}
    todo = []
    for text in texts:
        key = "lang:" + _key(system, text)
        if key in cache:
            langs[text] = cache[key]
        else:
            todo.append(text)
    for start in range(0, len(todo), DETECT_BATCH):
        batch = todo[start:start + DETECT_BATCH]
        message = "\n".join(f"[{k}] {' '.join(text[:DETECT_CHARS].split())}" for k, text in enumerate(batch, 1)) + "\n"
        try:
            reply, _completion = model.complete_json(
                system, message, DETECT_SCHEMA, DETECT_TOKENS_PER_TEXT * len(batch) + 100, 0.0)
        except EngineError as exc:
            done.failed.append(f"language tags for {len(batch)} posts: {str(exc).splitlines()[0][:100]}")
            continue
        entries = reply.get("posts")
        for entry in entries if isinstance(entries, list) else []:
            n = entry.get("n") if isinstance(entry, dict) else None
            code = _code(entry.get("lang", "")) if isinstance(entry, dict) else ""
            if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= len(batch) and code:
                langs[batch[n - 1]] = cache["lang:" + _key(system, batch[n - 1])] = code
    return langs


def translate_one(model: Model, text: str, system: str) -> str:
    """`text` translated, or "" when the reply has none. Raises EngineError when the call fails."""
    budget = min(3200, max(300, len(text) + 100))  # CJK runs ~1 token a character, and English is longer than it
    reply, _completion = model.complete_json(system, text, TRANSLATE_SCHEMA, budget, 0.2)
    return str(reply.get("translation", "")).strip()


def translate_texts(
    model: Model, texts: list[str], language: Language, cache: dict[str, str] | None = None,
    on_result: Callable[[str], None] | None = None,
) -> Translated:
    """The translations of those `texts` that are in a language `language` does not accept. `cache` (updated
    in place) holds earlier answers: language tags and translations, keyed by prompt and text. `on_result` gets a
    one-line progress note per translation made."""
    cache = {} if cache is None else cache
    done = Translated()
    texts = [t for t in dict.fromkeys(texts) if t and worth_checking(t)]
    langs = _detect(model, texts, load_detect_prompt(), cache, done)
    done.checked = len(langs)
    system = load_translate_prompt(language.name(language.translate_to))
    for text, lang in langs.items():
        if lang in language.accepted:
            continue
        done.foreign += 1
        if len(text) > MAX_TRANSLATE_CHARS:
            done.too_long += 1
            continue
        key = f"{language.translate_to}:" + _key(system, text)
        cached = key in cache
        if cached:
            translation = cache[key]
        else:
            try:
                translation = translate_one(model, text, system)
            except EngineError as exc:
                done.failed.append(f"a {language.name(lang)} post: {str(exc).splitlines()[0][:100]}")
                continue
            if not translation or _same(translation, text):
                done.failed.append(f"a {language.name(lang)} post: no translation in the reply")
                continue
            cache[key] = translation
        found = support([{"headline": "", "detail": translation, "ids": [0]}], {0: text}).flagged
        done.translations[text] = Translation(lang, translation, found[0][1] if found else ())
        if on_result and not cached:
            on_result(f"translated a {language.name(lang)} post ({len(translation.split())} words)")
    return done


def load_cache(path: Path) -> dict[str, str]:
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")).get("translations", {}))
    except (OSError, ValueError):
        return {}


def save_cache(path: Path, cache: dict[str, str]) -> None:
    path.write_text(json.dumps({"translations": cache}, ensure_ascii=False, indent=1), encoding="utf-8")
