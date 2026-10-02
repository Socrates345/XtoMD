"""The brief prompt, and the JSON shape it asks for.

prompts/brief.md holds everything the model is told: the rules every band shares,
then one short section per band (`## Band: regular`). A local model gets the shared
rules plus only its chunk's band, so the others don't distract it; Claude Code can
use the whole file as a skill. The schema lives here, next to the prose that
describes it, so the two are changed together.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

from ..core.config import Language

DEFAULT_PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "brief.md"
SECTION_PROMPT = DEFAULT_PROMPT.with_name("section.md")
DETECT_PROMPT = DEFAULT_PROMPT.with_name("detect.md")
TRANSLATE_PROMPT = DEFAULT_PROMPT.with_name("translate.md")

# the reply shape of a section summary (prompts/section.md)
SECTION_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}

# the reply shape every engine is asked for (docs/digest-summary.md, "Design")
BRIEF_SCHEMA = {
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

def bounded_schema(min_items: int, max_items: int) -> dict:
    """BRIEF_SCHEMA with the number of items limited in the schema itself, for servers that enforce it."""
    schema = copy.deepcopy(BRIEF_SCHEMA)
    schema["properties"]["items"].update(minItems=min_items, maxItems=max_items)
    return schema


_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
_BAND = re.compile(r"^## Band: (\w+)[ \t]*$", re.MULTILINE)
_LANGUAGE_LINE = re.compile(r"^- Language: .*$", re.MULTILINE)  # the rule `language:` in sources.yaml replaces


def brief_language_rule(language: Language) -> str:
    """The `- Language:` line of prompts/brief.md for a reader who reads only `language.accepted`."""
    return (f"- Language: the reader reads {language.accepted_names}. Write an item in the language of the posts it "
            f"draws on when they are all in the same one of these; otherwise (a post in any other language, or posts "
            f"in different languages) write it in {language.name(language.translate_to)}, translating faithfully.")


def section_language_rule(language: Language) -> str:
    """The `- Language:` line of prompts/section.md for a reader who reads only `language.accepted`."""
    return (f"- Language: the reader reads {language.accepted_names}. When all the bullets are in the same one of "
            f"these, write in it; otherwise write in {language.name(language.translate_to)}.")


def _with_rule(text: str, rule: str, path: Path | str) -> str:
    """`text` with its `- Language:` line replaced by `rule`. A prompt without one is an error, not a prompt
    that silently ignores the reader's languages."""
    text, found = _LANGUAGE_LINE.subn(lambda _m: rule, text, count=1)
    if not found:
        raise ValueError(f"{path} has no `- Language:` line for the language settings to replace")
    return text


def load_prompt(band: str, path: Path | str = DEFAULT_PROMPT, language: Language | None = None) -> str:
    """The system prompt for a chunk of `band` ("regular", "retweets", "recap"): the shared
    rules, then that band's section, without the front matter or the other bands. With a
    `language`, its rule replaces the prompt's own "language of the posts" line."""
    text = _FRONTMATTER.sub("", Path(path).read_text(encoding="utf-8"), count=1)
    if language:
        text = _with_rule(text, brief_language_rule(language), path)
    common, *rest = _BAND.split(text)  # [shared, name, body, name, body, ...]
    bands = {name: body.strip() for name, body in zip(rest[::2], rest[1::2])}
    if band not in bands:
        raise ValueError(f"no band {band!r} in {path} (it has: {', '.join(bands) or 'none'})")
    return f"{common.strip()}\n\n## This chunk\n\nBand: {band}. {bands[band]}\n"


def load_section_prompt(path: Path | str = SECTION_PROMPT, language: Language | None = None) -> str:
    """The system prompt for a section summary: the file without its front matter, with `language`'s rule when
    one is given."""
    text = _FRONTMATTER.sub("", Path(path).read_text(encoding="utf-8"), count=1)
    if language:
        text = _with_rule(text, section_language_rule(language), path)
    return text.strip() + "\n"


def load_detect_prompt(path: Path | str = DETECT_PROMPT) -> str:
    return _FRONTMATTER.sub("", Path(path).read_text(encoding="utf-8"), count=1).strip() + "\n"


def load_translate_prompt(target: str, path: Path | str = TRANSLATE_PROMPT) -> str:
    """The system prompt for translating one post into `target` (a language's name, e.g. "English")."""
    text = _FRONTMATTER.sub("", Path(path).read_text(encoding="utf-8"), count=1)
    return text.replace("{target}", target).strip() + "\n"
