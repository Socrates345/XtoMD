"""Checks on what a model wrote, from a saved chunk record (see runner.write_run).

`chunk_stats`: do the cited post numbers exist in the chunk, how much of the chunk does the
brief cover, and is the reply repeating itself. `support`: do the numbers, @handles,
$tickers and names in an item appear in the posts it cites.

Everything here is a count or a share, so a run can be checked, and its results
shared, without anyone reading the posts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADLINE = re.compile(r'"headline"\s*:\s*"((?:[^"\\]|\\.)*)"')
_DETAIL = re.compile(r'"detail"\s*:\s*"((?:[^"\\]|\\.)*)"')
_IDS = re.compile(r'"ids"\s*:\s*\[([^\]]*)\]')


@dataclass(frozen=True)
class ChunkStats:
    items: int
    ids_per_item: float
    max_ids: int  # the longest id list of any one item
    cited: int  # distinct post numbers cited
    in_chunk: float  # share of the citations that are posts of this chunk; 1.0 = none invented
    covered: float  # share of the chunk's posts cited at least once
    duplicate_headlines: int  # a reply that repeats itself is looping
    detail_words: float  # mean words per detail


def chunk_stats(record: dict) -> ChunkStats:
    """Stats for one chunk record. A reply that was cut off mid-JSON has no parsed `reply`, so its
    raw text is read as far as it goes: the numbers then describe what was written before the cut."""
    reply = record.get("reply")
    if reply is not None:
        items = reply["items"]
        headlines = [i["headline"] for i in items]
        details = [i["detail"] for i in items]
        id_lists = [list(i["ids"]) for i in items]
    else:
        text = record.get("raw_text") or ""
        headlines = _HEADLINE.findall(text)
        details = _DETAIL.findall(text)
        id_lists = [[int(n) for n in re.findall(r"\d+", found)] for found in _IDS.findall(text)]

    valid = set(record["ids"])
    cited = [n for ids in id_lists for n in ids]
    return ChunkStats(
        items=len(headlines),
        ids_per_item=sum(map(len, id_lists)) / len(id_lists) if id_lists else 0.0,
        max_ids=max(map(len, id_lists), default=0),
        cited=len(set(cited)),
        in_chunk=sum(n in valid for n in cited) / len(cited) if cited else 1.0,
        covered=len(set(cited) & valid) / len(valid) if valid else 0.0,
        duplicate_headlines=len(headlines) - len({h.strip().lower() for h in headlines}),
        detail_words=sum(len(d.split()) for d in details) / len(details) if details else 0.0,
    )


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_HANDLE = re.compile(r"@\w{2,}")
_TICKER = re.compile(r"\$[A-Za-z]{1,6}\b")
_ACRONYM = re.compile(r"\b[A-Z]{2,}\b")
_CAPITALIZED = re.compile(r"[A-Z][a-z]{3,}")
_STEM = 4  # a capitalized word counts as supported when its first four letters appear in a cited post


@dataclass(frozen=True)
class Support:
    """How much of what the items state is found in the posts they cite."""

    items: int
    uncited_items: int  # items citing no post of the chunk at all: nothing can support them
    facts: int  # numbers, @handles and $tickers written in the items
    unsupported_facts: int  # of those, absent from every post the item cites
    names: int  # acronyms, and capitalized words in mid-sentence
    unsupported_names: int
    items_with_unsupported_fact: int
    flagged: tuple[tuple[int, tuple[str, ...]], ...] = ()  # (item index, its unsupported numbers/handles/tickers)


def support(items: list[dict], post_text: dict[int, str]) -> Support:
    """Check each item's numbers, handles, tickers and names against the text of the posts it cites.
    Numbers are compared without thousands commas; names by stem, so a paraphrase like "hacks" for
    "hacked" passes. A rough net: it catches an invented figure, not a wrong emphasis."""
    uncited = facts = bad_facts = names = bad_names = bad_items = 0
    flagged = []
    for k, item in enumerate(items):
        cited = " ".join(post_text[n] for n in item["ids"] if n in post_text)
        if not cited:
            uncited += 1
        low, plain = cited.lower(), cited.replace(",", "")
        text = f"{item['headline']} {item['detail']}"
        missing = []
        for number in _NUMBER.findall(text):
            facts += 1
            wanted = number.replace(",", "")
            if not re.search(rf"(?<![\d.]){re.escape(wanted)}(?!\d)", plain):
                missing.append(number)
        for token in _HANDLE.findall(text) + _TICKER.findall(text):
            facts += 1
            if token.lower() not in low:
                missing.append(token)
        bad_facts += len(missing)
        bad_items += bool(missing)
        if missing:
            flagged.append((k, tuple(missing)))
        for acronym in _ACRONYM.findall(text):
            names += 1
            bad_names += acronym.lower() not in low
        for part in (item["headline"], item["detail"]):  # each starts a sentence, so its first word is skipped
            words = part.split()
            for k, word in enumerate(words):
                word = word.strip(".,;:!?()[]\"'“”‘’")
                if k and not words[k - 1].endswith((".", "!", "?")) and _CAPITALIZED.fullmatch(word):
                    names += 1
                    bad_names += word.lower()[:_STEM] not in low
    return Support(len(items), uncited, facts, bad_facts, names, bad_names, bad_items, tuple(flagged))
