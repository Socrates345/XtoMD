"""Find the ads among the posts of the accounts marked `ads:off` in sources/x.md, so the brief can hide them.

Keywords can't tell an account's sales pitch from its news when the pitch is worded anew every time, so the model
reads those accounts' posts, in batches, and says which are ads (prompts/ads.md). It runs when the brief is made
(scripts/run_system.py, before the summaries, so an ad is neither summarized nor shown word for word); nothing is
written back: the store and the digests keep every post.

A post with no words to read (a picture alone) is not asked about: the fetch's repeat filter is what catches a
picture posted again and again (ingest/filters.py). A call that fails keeps its posts: hiding a real post costs
the reader more than showing an ad.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .engine import EngineError
from .prompt import load_ads_prompt
from .translate import Model, worth_checking

ADS_SCHEMA = {
    "type": "object",
    "properties": {
        "posts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"n": {"type": "integer"}, "ad": {"type": "boolean"}},
                "required": ["n", "ad"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["posts"],
    "additionalProperties": False,
}

ADS_BATCH = 20  # posts judged per call
ADS_CHARS = 600  # a post's start: room for its pitch and the link it ends on
ADS_TOKENS_PER_POST = 15  # one {"n": 12, "ad": false} entry


@dataclass
class AdCheck:
    """What find_ads did: the ids of the posts it found to be ads, and counts for the report."""

    hidden: list[str] = field(default_factory=list)  # the ads' ids, in the order they were given
    checked: int = 0  # posts the model judged
    failed: list[str] = field(default_factory=list)  # why a call gave nothing usable, one line each


def find_ads(model: Model, posts: dict[str, str]) -> AdCheck:
    """Which of `posts` ({id: "@poster: text"}) are ads. The ones with too few words to judge are not asked about."""
    done = AdCheck()
    system = load_ads_prompt()
    todo = [(key, text) for key, text in posts.items() if worth_checking(text.partition(": ")[2] or text)]
    ads: set[str] = set()
    for start in range(0, len(todo), ADS_BATCH):
        batch = todo[start:start + ADS_BATCH]
        message = "\n".join(f"[{k}] {' '.join(text[:ADS_CHARS].split())}" for k, (_, text) in enumerate(batch, 1)) + "\n"
        try:
            reply, _completion = model.complete_json(
                system, message, ADS_SCHEMA, ADS_TOKENS_PER_POST * len(batch) + 100, 0.0)
        except EngineError as exc:
            done.failed.append(f"{len(batch)} posts: {str(exc).splitlines()[0][:100]}")
            continue
        judged: set[int] = set()
        entries = reply.get("posts")
        for entry in entries if isinstance(entries, list) else []:
            n = entry.get("n") if isinstance(entry, dict) else None
            if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= len(batch) and n not in judged:
                judged.add(n)
                if entry.get("ad") is True:
                    ads.add(batch[n - 1][0])
        done.checked += len(judged)
        if len(judged) < len(batch):
            done.failed.append(f"{len(batch) - len(judged)} of {len(batch)} posts: not in the reply")
    done.hidden = [key for key in posts if key in ads]
    return done
