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

DEFAULT_PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "brief.md"
SECTION_PROMPT = DEFAULT_PROMPT.with_name("section.md")

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


def load_prompt(band: str, path: Path | str = DEFAULT_PROMPT) -> str:
    """The system prompt for a chunk of `band` ("regular", "retweets", "recap"): the shared
    rules, then that band's section, without the front matter or the other bands."""
    text = _FRONTMATTER.sub("", Path(path).read_text(encoding="utf-8"), count=1)
    common, *rest = _BAND.split(text)  # [shared, name, body, name, body, ...]
    bands = {name: body.strip() for name, body in zip(rest[::2], rest[1::2])}
    if band not in bands:
        raise ValueError(f"no band {band!r} in {path} (it has: {', '.join(bands) or 'none'})")
    return f"{common.strip()}\n\n## This chunk\n\nBand: {band}. {bands[band]}\n"


def load_section_prompt(path: Path | str = SECTION_PROMPT) -> str:
    """The system prompt for a section summary: the file without its front matter."""
    return _FRONTMATTER.sub("", Path(path).read_text(encoding="utf-8"), count=1).strip() + "\n"
