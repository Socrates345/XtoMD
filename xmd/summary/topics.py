"""Hot topics: names that many different accounts posted about in a window.

Repetition is the signal: when several of your sources keep writing about the same name
on the same day, something is going on, even when their sentences share too few words for
trending.py to call it one story. A name that is always popular is not news today, so what
counts is how far today's number of posts rises above what it usually is on the days before
(`usage_baseline`, built from the database's history), and it must come from several accounts,
not one loud one. This finds names that are louder than usual; whether a quiet one is big news
is a judgment about content, which is the section summary's job (sections.py).

Everything here is counting: no model, and nothing depends on the posts' meaning.
"""

from __future__ import annotations

import html
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from ..core.models import URL, FeedItem
from ..digest.render import section_key, section_label

MIN_SOURCES = 3  # accounts that must write a name before it can be a topic
MIN_POSTS = 6  # posts it must appear in
MIN_SCORE = 2.5  # and the posts must be at least this many times what is usual (see hot_topics)
TOP = 8  # topics shown in all: a name that is loud is rare, so few pass
MIN_DAY_POSTS = 100  # a history day with fewer posts than this (an outage, a gap) does not count

_CASHTAG = re.compile(r"\$[A-Za-z][A-Za-z0-9]{1,9}\b")
_NAME = re.compile(r"\b[A-Z][A-Za-z0-9]{2,}\b")  # Capitalized or ALLCAPS, three characters or more
_LOWER = re.compile(r"\b[a-z][a-z0-9]{2,}\b")
_STOP = frozenset("""
the and for with that this from have has are was were will would could should about into over after before just what when
where which while their there they them then than been being also more most some such only very much many other another
because between during through against without within upon your you our out get got new now today via per its here these
those one two how why who not but yes not all any can did does done off own say says said see seen use used way too
monday tuesday wednesday thursday friday saturday sunday january february march april may june july august september october
november december http https www com retweeted breaking news thread update alert live watch exclusive
""".split())


@dataclass(frozen=True)
class Topic:
    term: str  # as most often written ("ACME", "Globex")
    posts: int  # posts that mention it in the window
    sources: int  # distinct accounts among them
    usual: float  # posts on an ordinary day before; 0 when unknown or never seen


def post_text(item: FeedItem) -> str:
    """What an account wrote: its own words and, for a retweet or quote, the post it shares."""
    return " ".join(part for part in (item.own_comment, item.retweet_of_text) if part)


def _clean(text: str) -> str:
    return " ".join(URL.sub(" ", html.unescape(text)).split())


def _candidates(text: str) -> list[tuple[str, str, bool]]:
    """(name in lowercase, as written, sure) for the cashtags, ALLCAPS words and Capitalized words of a cleaned
    post. Cashtags and ALLCAPS words are names. A Capitalized word may only be an ordinary word that opens a
    sentence, so it is not sure: accounts_by_name decides it from how the window writes the word."""
    found: dict[str, tuple[str, bool]] = {}

    def add(raw: str, sure: bool) -> None:
        key = raw.lower()
        if key not in _STOP and (key not in found or (sure and not found[key][1])):
            found[key] = (raw, sure)

    for match in _CASHTAG.finditer(text):
        add(match.group()[1:], True)
    for match in _NAME.finditer(text):
        add(match.group(), match.group().isupper())
    return [(key, raw, sure) for key, (raw, sure) in found.items()]


def _count(posts: list[tuple]) -> tuple[dict[str, set[str]], Counter, dict[str, Counter], dict[str, Counter]]:
    """(accounts, posts, spellings, where) for posts of (account, text) or (account, text, section): `where` is
    {name: {section: posts}}. A Capitalized word counts as a name only if the posts almost never write it in
    lowercase: "Globex" is a name, "Bright" is not, because "a bright day" is everywhere."""
    cleaned = [(post[0], _clean(post[1]), post[2] if len(post) > 2 else "") for post in posts]
    lower = Counter(word for _account, text, _section in cleaned for word in _LOWER.findall(text))
    per_post = [(account, _candidates(text), section) for account, text, section in cleaned]
    weak = Counter(key for _account, found, _section in per_post for key, _raw, sure in found if not sure)
    accounts: dict[str, set[str]] = defaultdict(set)
    counts: Counter = Counter()
    spellings: dict[str, Counter] = defaultdict(Counter)
    where: dict[str, Counter] = defaultdict(Counter)
    for account, found, section in per_post:
        for key, raw, sure in found:
            if sure or lower[key] * 4 <= weak[key]:
                accounts[key].add(account)
                counts[key] += 1
                spellings[key][raw] += 1
                where[key][section] += 1
    return accounts, counts, spellings, where


def count_names(posts: list[tuple[str, str]]) -> tuple[dict[str, set[str]], Counter, dict[str, Counter]]:
    """({name: the accounts that wrote it}, {name: posts it is in}, {name: how each spelling was used})."""
    return _count(posts)[:3]


def usage_baseline(history: list[FeedItem]) -> dict[str, float]:
    """{name: posts per ordinary day}, from earlier posts: the mean over the days that have enough posts to
    count. Empty when the history is too short to say what is usual."""
    days: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for item in history:
        if item.published:
            days[item.published.strftime("%Y-%m-%d")].append((item.source, post_text(item)))
    days = {day: posts for day, posts in days.items() if len(posts) >= MIN_DAY_POSTS}
    total: Counter = Counter()
    for posts in days.values():
        total.update(count_names(posts)[1])
    return {key: count / len(days) for key, count in total.items()} if days else {}


def hot_topics(
    posts: list[tuple], usual: dict[str, float] | None = None, top: int = TOP,
    min_sources: int = MIN_SOURCES, min_posts: int = MIN_POSTS, min_score: float = MIN_SCORE,
) -> list[Topic]:
    """The names of (account, text) `posts` that rose furthest above their usual number of posts. A name
    qualifies with `min_sources` accounts, `min_posts` posts and a score, posts / (1 + usual posts), of at least
    `min_score`: one always in ten posts a day scores ~1 however loud, one that jumps from two a day to
    twenty-five scores 8."""
    return [topic for topic, _section in _hot(posts, usual, top, min_sources, min_posts, min_score)]


def _hot(posts, usual, top, min_sources, min_posts, min_score) -> list[tuple[Topic, str]]:
    usual = usual or {}
    accounts, counts, spellings, where = _count(posts)
    scored = []
    for key, who in accounts.items():
        score = counts[key] / (1 + usual.get(key, 0.0))
        if len(who) >= min_sources and counts[key] >= min_posts and score >= min_score:
            scored.append((score, counts[key], key))
    scored.sort(key=lambda row: (-row[0], -row[1], row[2]))
    result = []
    for _score, n, key in scored[:top]:
        section = min(where[key], key=lambda label: (-where[key][label], label))  # most of its posts; a tie, alphabetical
        result.append((Topic(spellings[key].most_common(1)[0][0], n, len(accounts[key]), usual.get(key, 0.0)), section))
    return result


def topics_by_section(items: list[FeedItem], usual: dict[str, float] | None = None, top: int = TOP,
                      min_sources: int = MIN_SOURCES, min_posts: int = MIN_POSTS,
                      min_score: float = MIN_SCORE) -> dict[str, list[Topic]]:
    """{section label: its hot topics}. A name is counted over the whole feed (its accounts are often spread over
    several sections, so none of them alone would show it) and filed under the section most of its posts are in."""
    labelled = [(i.source, post_text(i), section_label(section_key(i))) for i in items]
    filed: dict[str, list[Topic]] = defaultdict(list)
    for topic, section in _hot(labelled, usual, top, min_sources, min_posts, min_score):
        filed[section].append(topic)
    return dict(filed)
