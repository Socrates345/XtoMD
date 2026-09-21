from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from datetime import datetime

URL = re.compile(r"https?://\S+")
_CUT_OFF = re.compile(r"\s*(?:…|\.\.\.)\s*$")
_MIN_CUT_OFF_STEM = 20  # a shorter stem ("a…") would match almost anything
_MIN_TRUNCATED_COPY = 100  # X cuts a retweet's copy at ~280 chars; anything this long that opens the original is that copy


def _comparable(text: str) -> str:
    """Comparison form: entities decoded (a retweet's own text and the
    original's can disagree on "&gt;" vs ">"), URLs dropped, whitespace
    collapsed, lowercased."""
    return " ".join(URL.sub(" ", html.unescape(text)).lower().split())


def is_echo(comment: str, original: str) -> bool:
    """True when `comment` is just a copy of the `original` post it sits on,
    i.e. the retweeter added nothing. X hands a plain retweet back with the
    original's text as its own text, cut off at ~280 characters when the
    original is longer (with no marker) or with an ellipsis. So a copy is
    exact (after `_comparable`), a long comment that is the start of the
    original, or an ellipsis-cut version of it. A short bare prefix isn't
    enough, and neither is the reverse (the original followed by more words is
    a real comment), or "lol that's true" quoting "lol" would be swallowed."""
    a, b = _comparable(comment), _comparable(original)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= _MIN_TRUNCATED_COPY and b.startswith(a):
        return True
    for short, long in ((a, b), (b, a)):
        stem = _CUT_OFF.sub("", short)
        if stem != short and len(stem) >= _MIN_CUT_OFF_STEM and long.startswith(stem):
            return True
    return False


@dataclass
class FeedItem:
    """One normalized post from any source. X is the only adapter today, but
    the shape is platform-agnostic (see fetcher.py's dispatch contract) so a
    future RSS/YouTube/Substack adapter needs no changes here."""

    source: str  # display name of the source ("@dave")
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
    # derived, never stored: the poster had been silent for longer than tiers.SILENCE_DAYS before
    # this post. Set by Store.flag_after_silence when a digest loads its items, since only the
    # database knows what the poster did before (see tiers.py).
    after_silence: bool = field(default=False, compare=False)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = hashlib.sha256(self.url.encode("utf-8")).hexdigest()[:16]

    @property
    def own_comment(self) -> str:
        """What the poster wrote themselves: `full_text` (or `text`), minus a
        copy of the original post when this is a retweet (see is_echo) — the
        original is shown separately, so keeping the copy would print it twice."""
        body = self.full_text or self.text
        if self.retweet_of_author and is_echo(body, self.retweet_of_text):
            return ""
        return body

    @property
    def is_pure_retweet(self) -> bool:
        """A retweet that adds no words of its own. A bare URL doesn't count as
        a comment: for a retweet it's just the link to the retweeted post."""
        return bool(self.retweet_of_author) and not URL.sub("", self.own_comment).strip()

    def display_body(self) -> str:
        """The item's full content: its own text plus, for a retweet, the
        original tweet it's quoting/reposting."""
        body = self.own_comment
        if self.retweet_of_author:
            quoted = f"🔁 Retweeted @{self.retweet_of_author}: {self.retweet_of_text}"
            return f"{body}\n\n{quoted}" if body else quoted
        return body

    @property
    def word_count(self) -> int:
        """Words a reader meets: display_body() minus URLs. The one measure the
        reading-time figures and the brief's length targets are stated in."""
        return len(URL.sub(" ", html.unescape(self.display_body())).split())
