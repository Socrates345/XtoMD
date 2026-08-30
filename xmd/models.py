from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class FeedItem:
    """One normalized post from any source. X is the only adapter today, but
    the shape is platform-agnostic (see fetcher.py's dispatch contract) so a
    future RSS/YouTube/Substack adapter needs no changes here."""

    source: str  # display name of the source ("@karpathy")
    source_type: str  # "x" today; a future adapter would add "rss" etc.
    title: str
    url: str
    author: str = ""
    published: datetime | None = None  # timezone-aware UTC when known
    text: str = ""
    full_text: str = ""  # richer body; "" -> falls back to text
    images: list[str] = field(default_factory=list)  # significant remote image URLs
    group: str = ""  # interest group from a sources/x.md `## header` ("" = none)
    id: str = field(default="")
    # set when this item is a retweet: the original tweet's author/text.
    # `text`/`full_text` hold the retweeter's own added comment instead (empty
    # for a bare retweet with nothing added) — see display_body().
    retweet_of_author: str = ""
    retweet_of_text: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = hashlib.sha256(self.url.encode("utf-8")).hexdigest()[:16]

    def display_body(self) -> str:
        """The item's full content: its own text plus, for a retweet, the
        original tweet it's quoting/reposting."""
        body = self.full_text or self.text
        if self.retweet_of_author:
            quoted = f"🔁 Retweeted @{self.retweet_of_author}: {self.retweet_of_text}"
            return f"{body}\n\n{quoted}" if body else quoted
        return body
