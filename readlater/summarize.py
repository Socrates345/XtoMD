"""Summarize saved links: x.com posts and web pages in a .md file in, one summary.md out.

    python readlater\\summarize.py               reads readlater\\links.md
    python readlater\\summarize.py other.md      any .md file with links in it
    python readlater\\summarize.py --dry-run     look the links up and sort them; no model, no file

Each x.com link is looked up on tweetapi.com and sorted: an X Article, a thread (the author's chain of replies to
themselves, read as one piece), a long post or a post. Any other link is a web page, read with Defuddle down to its
main text (web.py). Before the model is called, the run says about what it will cost and what the key can still
spend, and asks to go on. Each one is then summarized on its own by the model the XMD_LLM_* settings name (Venice:
deploy/env.example), and the run writes one readlater/summaries/<date>-<time>-summary.md. README.md, next to this
file, has the setup.

The brief's code is imported, never changed: the request helper of the tweetapi adapter, the model client, the
settings readers and the markdown escaping.
"""

from __future__ import annotations

import argparse
import html
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))  # run from a checkout, installed or not

import httpx  # noqa: E402

from xmd.core.config import (  # noqa: E402
    DEFAULT_TIMEZONE, TWEETAPI_API_KEY_ENV_VAR, Language, _read_yaml, _section, read_language,
)
from xmd.digest.render import _md, words_md  # noqa: E402
from xmd.ingest.adapters.tweetapi import BASE_URL, _get, _published, _rate_limiter  # noqa: E402
from xmd.summary.engine import (  # noqa: E402
    API_KEY_ENV_VAR, BASE_URL_ENV_VAR, EXTRA_BODY_ENV_VAR, MODEL_ENV_VAR, Engine, EngineError, parse_extra_body,
    refusal_hint,
)

import web  # noqa: E402  (readlater/web.py: the web pages)

DEFAULT_LINKS = HERE / "links.md"
# what a links.md made by the first run says: only the links in it are read, so this line can stay
LINKS_HEADER = ("Paste x.com links or web page links below, one per line. Each link's type (X Article, thread, long "
                "post, post, web page) is detected automatically.\n\n")
SUMMARIES_DIR = HERE / "summaries"
PROMPT = HERE / "prompt.md"
PROMPT_WEB = HERE / "prompt_web.md"  # a web page's: prompt.md stays the X posts' own
ENV_FILE = REPO / ".env"
CONFIG = REPO / "sources.yaml"
ENV_EXAMPLE = "deploy/env.example"

# How long a summary is. These lines are the only place the numbers are written (README.md points here).
SHORT_WORDS = 60  # a post (or thread) of this many words or fewer is shown as posted: summarizing it saves nothing
SUMMARY_SHARE = 0.2  # a summary is about this share of the words it sums up...
MIN_SUMMARY_WORDS = 60  # ...but never shorter than this...
MAX_SUMMARY_WORDS = 400  # ...nor longer than this
WORDS_PER_POINT = 150  # one key point per this many words of the original...
MIN_POINTS = 3  # ...but never fewer than this...
MAX_POINTS = 10  # ...nor more than this
TLDR_SHARE = 0.25  # of the summary's words, the TL;DR's share (the points share the rest)...
MIN_TLDR_WORDS = 20  # ...but never fewer than this...
MAX_TLDR_WORDS = 40  # ...nor more than this

LONG_POST_CHARS = 280  # X's limit without Premium: a post longer than this is a long post
LOOKUP_BATCH = 200  # ids per details-by-ids call, the most the API takes
THREAD_PAGES = 10  # search pages read for one thread, ~20 tweets each
MAX_THREAD = 200  # tweets one thread may hold here: a net under a reply chain that loops
MAX_TOKENS = 2000  # the reply's cap: the longest budget (MAX_SUMMARY_WORDS) as JSON needs about a third of it
TEMPERATURE = 0.2
MODEL_TIMEOUT = 300.0  # seconds per model call
X_TIMEOUT = 30.0  # seconds per tweetapi.com call
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

# What a run will cost, said before it starts. Measured with Qwen 3.5's tokenizer on the test pages (2026-11-10): an
# essay is 1.27-1.37 tokens a word, a GitHub README 1.6, a Wikipedia article (references, dates) 2.0; the reply of a
# full summary (400 words as JSON) was 665 tokens.
TOKENS_PER_WORD = 1.4  # for the estimate
WINDOW_TOKENS_PER_WORD = 2.0  # for the cut to the model's window: the most measured, so a cut page always fits
REPLY_TOKENS = 700  # what one summary's reply is reckoned at
HEADER_WORDS = 200  # a message's lines before the page's text, its title with them, and room to spare
VENICE_TIMEOUT = 20.0  # seconds per call for the model's prices and the key's balance

ARTICLE, THREAD, LONG, POST, WEB = "article", "thread", "long", "post", "web"
LABELS = {ARTICLE: "X Article", THREAD: "Thread", LONG: "Long post", POST: "Post", WEB: "Web page"}
ICONS = {ARTICLE: "📄", THREAD: "🧵", LONG: "📝", POST: "💬", WEB: "🌐"}

_HOST = r"(?<![\w.-])(?:https?://)?(?:www\.|mobile\.)?(?:x|twitter)\.com/"
STATUS_LINK = re.compile(_HOST + r"(?:i/web/|i/|\w{1,15}/)status(?:es)?/(\d+)", re.IGNORECASE)
ARTICLE_LINK = re.compile(_HOST + r"i/article/(\d+)", re.IGNORECASE)
# a post's link to itself, as X appends it for its pictures (".../status/<id>/photo/1", seen live)
_STATUS_URL = re.compile(STATUS_LINK.pattern + r"(?:/(?:photo|video)/\d+)?", re.IGNORECASE)
_HANDLE = re.compile(r"\w{1,15}")
_TCO = re.compile(r"https?://t\.co/\w+")
_MENTIONS = re.compile(r"(?:@\w{1,15}\s*)+")
# any other link is a web page. Never x.com's own (a profile, a list: not posts, as before), its picture and short
# links, nor the sites that show its posts again
_WEB_URL = re.compile(r"https?://[^\s<>\"'`\[\]]+", re.IGNORECASE)
_X_HOSTS = re.compile(r"(?:^|\.)(?:x|twitter|fxtwitter|vxtwitter|fixupx|fixvx)\.com$|^t\.co$")
_ENV_LINE = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)")
_QUOTED_VALUE = re.compile(r"""(['"])(.*)\1(?:\s+#.*)?""")


class SetupError(RuntimeError):
    """Something only the user can put right (a setting, a file): the message says what, and nothing has been
    sent anywhere yet, or nothing more will be."""


# --- the links ---------------------------------------------------------------------------------------------------


def parse_links(text: str) -> tuple[list[str], list[str]]:
    """(the post ids of every x.com/twitter.com status link in `text`, in the order they first appear; the ids of
    x.com/i/article/ links, which name an Article but no post, so cannot be looked up). Anything else is ignored."""
    ids = list(dict.fromkeys(m.group(1) for m in STATUS_LINK.finditer(text)))
    articles = list(dict.fromkeys(m.group(1) for m in ARTICLE_LINK.finditer(text)))
    return ids, articles


def _trim(url: str) -> str:
    """A link without the prose around it: a sentence's punctuation, bold marks, and a ")" with no "(" to match are
    not part of it (the GFM autolink rule). Its #part is left out: it is the same page."""
    url = url.split("#", 1)[0]
    while url and (url[-1] in ".,;:!?*" or (url[-1] == ")" and url.count(")") > url.count("("))):
        url = url[:-1]
    return url


def parse_web_links(text: str) -> list[str]:
    """The links in `text` that are not x.com's, in the order they first appear: each one a web page to read. A bare
    link, <link> and [words](link) all work; a link needs its https:// (a note's words are never taken for one)."""
    links: list[str] = []
    for match in _WEB_URL.finditer(text):
        url = _trim(match.group(0))
        host = (urlparse(url).hostname or "").lower()
        if "." in host and not _X_HOSTS.search(host) and url not in links:
            links.append(url)
    return links


def positions(text: str) -> dict[str, int]:
    """Where each post id and web link first appears in `text`: the summary keeps the file's order."""
    found: dict[str, int] = {}
    for match in STATUS_LINK.finditer(text):
        found.setdefault(match.group(1), match.start())
    for match in _WEB_URL.finditer(text):
        found.setdefault(_trim(match.group(0)), match.start())
    return found


# --- settings ----------------------------------------------------------------------------------------------------


def read_env_file(path: Path) -> dict[str, str]:
    """The KEY=value lines of a .env, read the way deploy/brief.sh's `.` reads it: `#` comments, values in single
    or double quotes. For this script only: nothing is put into the environment."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = _ENV_LINE.fullmatch(line)
        if not match:
            continue
        value = match.group(2).strip()
        quoted = _QUOTED_VALUE.fullmatch(value)
        values[match.group(1)] = quoted.group(2) if quoted else re.split(r"\s+#", value, maxsplit=1)[0].strip()
    return values


def setting(name: str, env_file: dict[str, str]) -> str:
    """`name` from the environment, else from the .env: a variable set in the shell wins."""
    return os.environ.get(name) or env_file.get(name, "")


@dataclass(frozen=True)
class ModelSettings:
    base_url: str
    model: str
    api_key: str
    extra_body: dict


def model_settings(env_file: dict[str, str]) -> ModelSettings:
    """The model server, from the same XMD_LLM_* settings the VPS uses. There is no default: without them this
    would quietly ask LM Studio instead of Venice."""
    base_url = setting(BASE_URL_ENV_VAR, env_file)
    model = setting(MODEL_ENV_VAR, env_file)
    api_key = setting(API_KEY_ENV_VAR, env_file)
    missing = [name for name, value in ((BASE_URL_ENV_VAR, base_url), (MODEL_ENV_VAR, model)) if not value]
    if not api_key and (urlparse(base_url).hostname or "").lower() not in LOCAL_HOSTS:
        missing.append(API_KEY_ENV_VAR)
    if missing:
        raise SetupError(
            f"{', '.join(missing)} not set: copy the model lines of {ENV_EXAMPLE} into .env at the repo root and put "
            f"your Venice key after {API_KEY_ENV_VAR}= (readlater/README.md, Setup)"
        )
    try:
        extra_body = parse_extra_body(setting(EXTRA_BODY_ENV_VAR, env_file))
    except ValueError as exc:
        raise SetupError(f"{EXTRA_BODY_ENV_VAR}: {exc}") from None
    return ModelSettings(base_url.rstrip("/"), model, api_key, extra_body)


@dataclass(frozen=True)
class XSettings:
    api_key: str
    rate_limit_per_minute: int
    tz: ZoneInfo
    language: Language | None


def x_settings(config: Path, env_file: dict[str, str], need_key: bool = True) -> XSettings:
    """What this needs from sources.yaml: the tweetapi.com key (or XMD_TWEETAPI_KEY, the same order as the brief),
    its pacing, the clock and `language:`. The follow list is not needed, and the key only when there are x.com
    links to look up (`need_key`)."""
    try:
        raw = _read_yaml(config) if config.exists() else {}
        x = _section(raw, "x", config)
        language = read_language(config)
    except ValueError as exc:
        raise SetupError(f"config error: {exc}") from None
    api_key = str(x.get("tweetapi_api_key") or "") or setting(TWEETAPI_API_KEY_ENV_VAR, env_file)
    if not api_key and need_key:
        raise SetupError(
            f"x.tweetapi_api_key in {config.name} (or {TWEETAPI_API_KEY_ENV_VAR}) is not set: the links are looked up "
            "on tweetapi.com"
        )
    try:
        rate = int(x.get("tweetapi_rate_limit_per_minute", 10))
    except (TypeError, ValueError):
        raise SetupError(f"{config.name}: x.tweetapi_rate_limit_per_minute must be a number") from None
    tz_name = str(raw.get("timezone") or DEFAULT_TIMEZONE)
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise SetupError(f"{config.name}: can't read timezone {tz_name!r} (on Windows: pip install tzdata)") from None
    return XSettings(api_key, rate, tz, language)


# --- tweetapi.com ------------------------------------------------------------------------------------------------


def short_error(exc: httpx.HTTPError) -> str:
    """A failed tweetapi.com call in a few words, and what to do about it. Never the request itself: its headers
    hold the key."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        hint = {
            401: f"the key was turned down: check x.tweetapi_api_key in sources.yaml (or {TWEETAPI_API_KEY_ENV_VAR})",
            403: "blocked, even after retries: wait a minute and run it again",
            429: "rate limit, even after retries: wait a minute and run it again",
            503: "tweetapi.com has no calls left to give: the plan's monthly quota may be used up",
        }.get(code, "")
        return f"HTTP {code}" + (f", {hint}" if hint else "")
    return f"{type(exc).__name__}: {exc}"


class TweetApi:
    """The tweetapi.com calls this needs, through the brief's own request helper (a browser User-Agent for
    Cloudflare, retries on 429 and 403, paced to x.tweetapi_rate_limit_per_minute). `calls` is what the run cost."""

    def __init__(self, api_key: str, timeout: float = X_TIMEOUT, get=_get) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self._get = get
        self.calls = 0

    def _call(self, path: str, params: dict) -> dict:
        self.calls += 1
        data = self._get(f"{BASE_URL}{path}", params, self.api_key, self.timeout)
        return data if isinstance(data, dict) else {}

    def lookup(self, ids: list[str]) -> dict[str, dict]:
        """{id: tweet} for the ids tweetapi.com knows; a deleted or private post is simply not in it."""
        found: dict[str, dict] = {}
        for start in range(0, len(ids), LOOKUP_BATCH):
            data = self._call("/tweet/details-by-ids", {"ids": ",".join(ids[start : start + LOOKUP_BATCH])})
            for tweet in (data.get("data") or {}).get("tweets") or []:
                if isinstance(tweet, dict) and tweet.get("id"):
                    found[str(tweet["id"])] = tweet
        return found

    def details(self, tweet_id: str) -> dict:
        """One post with everything the API has on it: the only call that carries an X Article's full text."""
        return self._call("/tweet/details", {"tweetId": tweet_id}).get("data") or {}

    def conversation(self, conversation_id: str, author: str) -> list[dict]:
        """The author's own tweets in a conversation, as X search finds them (every page, up to THREAD_PAGES)."""
        tweets: list[dict] = []
        cursor = ""
        for _ in range(THREAD_PAGES):
            params = {"query": f"conversation_id:{conversation_id} from:{author}", "type": "Latest"}
            if cursor:
                params["cursor"] = cursor
            data = self._call("/search", params)
            page = [t for t in data.get("data") or [] if isinstance(t, dict) and t.get("id") and "text" in t]
            tweets.extend(page)
            cursor = (data.get("pagination") or {}).get("nextCursor") or ""
            if not page or not cursor:
                break
        return tweets


# --- one tweet ---------------------------------------------------------------------------------------------------


def username(tweet: dict) -> str:
    return str((tweet.get("author") or {}).get("username") or "")


def display_name(tweet: dict) -> str:
    return " ".join(str((tweet.get("author") or {}).get("name") or "").split())


def tweet_url(tweet: dict) -> str:
    """The post's link, made from its id and handle only (never a URL from the reply, so nothing odd gets in)."""
    handle = username(tweet)
    tweet_id = str(tweet.get("id") or "")
    if not tweet_id.isdigit():
        return ""
    return f"https://x.com/{handle if _HANDLE.fullmatch(handle) else 'i/web'}/status/{tweet_id}"


def replies_to_self(tweet: dict) -> str:
    """The id of the author's own tweet this one answers (it continues their thread), or ""."""
    reply = tweet.get("replyTo") or {}
    if not isinstance(reply, dict) or str(reply.get("username") or "").lower() != username(tweet).lower():
        return ""
    return str(reply.get("tweetId") or "")


def clean_text(text: str) -> str:
    """Entities decoded (X sends "&amp;"), t.co links left out (they are a post's media, or listed apart), spaces
    collapsed; line breaks stay, at most one blank line in a row."""
    text = html.unescape(_TCO.sub("", text))
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def display_text(tweet: dict) -> str:
    """The words of a post as X shows them: a reply's leading @mentions are cut when the API says they are not
    displayed (`displayTextRange`) and they really are just mentions; the post's links to itself (its pictures)
    and to an X Article (the whole text of a post that shares one, seen live) are left out. The end of
    `displayTextRange` is never used: on a long post it is where X cuts the text for "Show more" (275 of 3,447
    characters, seen live)."""
    raw = str(tweet.get("text") or "")
    span = tweet.get("displayTextRange")
    if isinstance(span, list) and len(span) == 2 and isinstance(span[0], int) and 0 < span[0] <= len(raw):
        if _MENTIONS.fullmatch(raw[: span[0]]):
            raw = raw[span[0] :]
    own = str(tweet.get("id") or "")
    raw = _STATUS_URL.sub(lambda m: "" if m.group(1) == own else m.group(0), raw)
    return clean_text(ARTICLE_LINK.sub("", raw))


def quoted_line(tweet: dict) -> str:
    """"Quoting @x: ..." for a post that quotes another, or ""."""
    quoted = tweet.get("quotedTweet")
    if not isinstance(quoted, dict):
        return ""
    text = display_text(quoted)
    article = quoted.get("article") if isinstance(quoted.get("article"), dict) else {}
    if not text and article:
        text = clean_text(" ".join(str(article.get(k) or "") for k in ("title", "previewText")))
    return f"Quoting @{username(quoted) or 'someone'}: {text}" if text else ""


def tweet_body(tweet: dict) -> str:
    """What the model reads of one tweet: its words, then what it quotes."""
    return "\n\n".join(part for part in (display_text(tweet), quoted_line(tweet)) if part)


def published_day(tweet: dict) -> str:
    when = _published(tweet)
    return f"{when:%Y-%m-%d}" if when else ""


# --- what a link turns out to be ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Summary:
    title: str
    tldr: str
    points: list[tuple[str, str]]  # (label, text)

    @property
    def words(self) -> int:
        return sum(len(part.split()) for part in (self.tldr, *(t for p in self.points for t in p)))


@dataclass
class Entry:
    kind: str  # ARTICLE, THREAD, LONG or POST
    tweets: list[dict]  # the post, or a thread's tweets in order
    saved: list[int] = field(default_factory=list)  # which of `tweets` the list links to, from 1
    article_title: str = ""
    article_text: str = ""
    note: str = ""  # how it was read, when the reader should know (a thread that could not be checked)
    summary: Summary | None = None
    error: str = ""  # why it is not summarized

    @property
    def first(self) -> dict:
        return self.tweets[0]

    @property
    def author(self) -> str:
        return username(self.first)

    @property
    def url(self) -> str:
        return tweet_url(self.first)

    @property
    def text(self) -> str:
        """What the model reads."""
        if self.kind == ARTICLE:
            parts = [f"Title: {self.article_title}" if self.article_title else "", tweet_body(self.first),
                     self.article_text]
            return "\n\n".join(part for part in parts if part)
        if self.kind == THREAD:
            return "\n\n".join(f"[{n}] {tweet_body(t)}" for n, t in enumerate(self.tweets, 1))
        return tweet_body(self.first)

    @property
    def words(self) -> int:
        """The author's own words: what they quote is not counted."""
        own = sum(len(display_text(t).split()) for t in self.tweets)
        return own + len(self.article_text.split())

    def _media(self) -> list[dict]:
        return [m for t in self.tweets if isinstance(t.get("media"), list) for m in t["media"] if isinstance(m, dict)]

    @property
    def images(self) -> int:
        return sum(1 for m in self._media() if m.get("type") == "photo")

    @property
    def videos(self) -> int:
        """Videos and GIFs: whatever media is not a photo."""
        return sum(1 for m in self._media() if m.get("type") != "photo")

    def media_text(self) -> str:
        """"2 images and 1 video", or "" when there is none."""
        parts = [_count(n, word) for n, word in ((self.images, "image"), (self.videos, "video")) if n]
        return " and ".join(parts)

    @property
    def as_posted(self) -> bool:
        """Short enough to read as it is: shown whole, never sent to the model."""
        return not self.error and self.words <= SHORT_WORDS

    @property
    def links(self) -> list[str]:
        """The links the tweets carry (expanded by the API), minus the tweets' own and the posts they quote."""
        own = {str(t.get("id")) for t in self.tweets}
        own |= {str(t["quotedTweet"].get("id")) for t in self.tweets if isinstance(t.get("quotedTweet"), dict)}
        own_articles = {str(t["article"].get("id")) for t in self.tweets if isinstance(t.get("article"), dict)}
        out: list[str] = []
        for tweet in self.tweets:
            for url in tweet.get("urls") or []:
                if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                    continue
                status, article = STATUS_LINK.match(url), ARTICLE_LINK.match(url)
                if (status and status.group(1) in own) or (article and article.group(1) in own_articles):
                    continue
                if url not in out:
                    out.append(url)
        return out

    def kind_text(self) -> str:
        if self.kind == THREAD:
            return f"thread of {len(self.tweets)} tweets"
        return LABELS[self.kind] if self.kind == ARTICLE else LABELS[self.kind].lower()


def may_be_thread(tweet: dict) -> bool:
    """Whether a thread search can find anything: a tweet that answers its own author is in one; a tweet nobody
    replied to cannot start one (its author's next tweet would be a reply)."""
    if replies_to_self(tweet):
        return True
    count = tweet.get("replyCount")
    return not (isinstance(count, int) and count == 0)


def self_chain(tweet: dict, pool: dict[str, dict], fetch) -> list[dict]:
    """The author's chain of replies to themselves that runs through `tweet`, first tweet first: up to the tweet
    that starts it, down to the last one. `pool` holds the author's tweets in the conversation by id; a parent not
    in it is asked for with `fetch(id)` (None when it can't be had). A reply to someone else ends the chain, and so
    does a reply by someone else: only the author's own thread is kept."""
    author = username(tweet).lower()
    chain = [tweet]
    seen = {str(tweet.get("id"))}
    while len(chain) < MAX_THREAD:
        parent_id = replies_to_self(chain[0])
        if not parent_id or parent_id in seen:
            break
        parent = pool.get(parent_id) or fetch(parent_id)
        if not parent or username(parent).lower() != author:
            break
        pool[parent_id] = parent
        chain.insert(0, parent)
        seen.add(parent_id)
    children: dict[str, list[dict]] = {}
    for candidate in pool.values():
        parent_id = replies_to_self(candidate)
        if parent_id and username(candidate).lower() == author:
            children.setdefault(parent_id, []).append(candidate)
    while len(chain) < MAX_THREAD:
        nexts = [c for c in children.get(str(chain[-1].get("id")), []) if str(c.get("id")) not in seen]
        if not nexts:
            break
        follow = min(nexts, key=lambda c: int(str(c.get("id"))))  # the author's first answer, if they gave two
        chain.append(follow)
        seen.add(str(follow.get("id")))
    return chain


def read_article(tweet: dict, api: TweetApi) -> tuple[str, str, str]:
    """(the Article's title, its full text, why the text could not be had or "")."""
    article = tweet.get("article") or {}
    title = " ".join(html.unescape(str(article.get("title") or "")).split())
    text = clean_text(str(article.get("text") or ""))
    if text:
        return title, text, ""
    try:
        full = api.details(str(tweet.get("id"))).get("tweet") or {}
    except httpx.HTTPError as exc:
        return title, "", f"the Article's text could not be fetched ({short_error(exc)})"
    text = clean_text(str((full.get("article") or {}).get("text") or ""))
    return title, text, "" if text else "tweetapi.com sent the Article without its text"


def classify(tweet: dict, api: TweetApi) -> Entry:
    """What a saved post is, with everything needed to summarize it: an X Article's text, a thread's tweets."""
    if isinstance(tweet.get("article"), dict):
        title, text, error = read_article(tweet, api)
        return Entry(ARTICLE, [tweet], [1], article_title=title, article_text=text, error=error)
    chain, note = [tweet], ""
    if may_be_thread(tweet):
        try:
            found = api.conversation(str(tweet.get("conversationId") or tweet.get("id")), username(tweet))
        except httpx.HTTPError as exc:
            found, note = [], f"thread not checked ({short_error(exc)})"
        if not note:
            pool = {str(t["id"]): t for t in found}
            pool[str(tweet["id"])] = tweet

            def fetch(tweet_id: str) -> dict | None:
                try:
                    return api.lookup([tweet_id]).get(tweet_id)
                except httpx.HTTPError:
                    return None

            chain = self_chain(tweet, pool, fetch)
    position = [str(t.get("id")) for t in chain].index(str(tweet.get("id"))) + 1
    if len(chain) > 1:
        return Entry(THREAD, chain, [position], note=note)
    kind = LONG if len(display_text(tweet)) > LONG_POST_CHARS else POST
    return Entry(kind, chain, [1], note=note)


def collect(ids: list[str], found: dict[str, dict], api: TweetApi) -> list[Entry]:
    """One entry per saved post, in the list's order. Two links into the same thread make one entry."""
    entries: list[Entry] = []
    placed: dict[str, Entry] = {}  # tweet id -> the entry it is in
    for tweet_id in ids:
        tweet = found.get(tweet_id)
        if tweet is None:
            continue
        if isinstance(tweet.get("retweetedTweet"), dict):  # a link to a repost: the post it reposts
            tweet = tweet["retweetedTweet"]
        own_id = str(tweet.get("id"))
        if own_id in placed or tweet_id in placed:
            entry = placed.get(own_id) or placed[tweet_id]
            position = [str(t.get("id")) for t in entry.tweets].index(own_id) + 1 if own_id in placed else 1
            if position not in entry.saved:
                entry.saved.append(position)
            continue
        entry = classify(tweet, api)
        for member in entry.tweets:
            placed[str(member.get("id"))] = entry
        placed[tweet_id] = entry
        entries.append(entry)
    return entries


def kind_counts(entries: list[Entry]) -> list[str]:
    """["1 X Article", "2 threads", ...] for the kinds there are, in a fixed order."""
    kinds = [(ARTICLE, "X Article"), (THREAD, "thread"), (LONG, "long post"), (POST, "post"), (WEB, "web page")]
    return [_count(sum(e.kind == k for e in entries), word) for k, word in kinds if any(e.kind == k for e in entries)]


def found_line(entries: list[Entry], links: int, found: int, calls: int, article_links: int) -> str:
    """The lookup in one line: what the links turned out to be, and what that cost."""
    line = f"{found} of {links} found" + (": " + ", ".join(kind_counts(entries)) if entries else "")
    posted = sum(e.as_posted for e in entries)
    if posted:
        line += f", {posted} short enough to show as posted"
    line += f" · {_count(calls, 'tweetapi.com call')}"
    if article_links:
        line += f" · {_count(article_links, 'Article link')} without a post to look up (listed in summary.md)"
    return line


def entry_line(n: int, total: int, entry: Entry) -> str:
    """One link as the lookup found it: its kind, its length and the summary's, or why it can't be summarized."""
    line = f"[{n}/{total}] @{entry.author}: {entry.kind_text()}, {_count(entry.words, 'word')}"
    if entry.error:
        line += f": {entry.error}"
    elif entry.as_posted:
        line += " → shown as posted"
    else:
        line += f" → summary of about {budget(entry.words)[0]:,} words"
    return line + (f" · {entry.note}" if entry.note else "")


# --- web pages ---------------------------------------------------------------------------------------------------


@dataclass
class WebPage:
    """A saved link that is not an x.com post: a page read with Defuddle (web.py)."""

    url: str  # as the links file has it
    title: str = ""
    author: str = ""
    site: str = ""
    published: str = ""  # YYYY-MM-DD, or ""
    text: str = ""  # its main text, as the model reads it
    images: int = 0
    cut: int = 0  # the words the model reads when the page is cut to fit its window; 0: all of them
    note: str = ""
    summary: Summary | None = None
    error: str = ""  # why it is not summarized
    kind: str = WEB

    @property
    def domain(self) -> str:
        host = (urlparse(self.url).hostname or "").lower()
        return host.removeprefix("www.")

    @property
    def words(self) -> int:
        return len(self.text.split())

    @property
    def read_words(self) -> int:
        """The words the model reads: all of them, unless the page is cut."""
        return min(self.cut, self.words) if self.cut else self.words

    @property
    def as_posted(self) -> bool:
        return False  # always summarized: a page short enough to show whole is a page that could not be read

    def kind_text(self) -> str:
        return "web page"


def read_page(url: str, read) -> WebPage:
    """One web link, read with `read(url)` (web.defuddle): its main text and what the page says of itself, or why it
    can't be summarized."""
    try:
        found = read(url)
    except web.PageError as exc:
        return WebPage(url, error=str(exc))
    page = WebPage(url, found.title, found.author, found.site, found.published, web.clean_page(found.markdown),
                   web.image_count(found.markdown))
    if page.author.casefold() == page.title.casefold():
        page.author = ""  # a page named as its own author (ccunpacked.dev, seen live) is no one to call by name
    if page.words < web.MIN_PAGE_WORDS:
        found_text = f"only {_count(page.words, 'word')} found" if page.words else "no text found"
        page.error = f"{found_text}: the page may build its text with JavaScript, or need a login"
    return page


def read_line(pages: list[WebPage]) -> str:
    """The reading in one line."""
    read = [p for p in pages if not p.error]
    return f"{len(read)} of {len(pages)} read" + (f": {sum(p.words for p in read):,} words" if read else "")


def page_line(n: int, total: int, page: WebPage) -> str:
    """One web link as it was read: its length and the summary's, or why it can't be summarized."""
    line = f"[{n}/{total}] {page.domain}: web page"
    if page.text:
        line += f", {_count(page.words, 'word')}"
    line += f": {page.error}" if page.error else f" → summary of about {budget(page.read_words)[0]:,} words"
    return line + (f" · {page.note}" if page.note else "")


def saved_at(entry: Entry, ids: list[str], found: dict[str, dict], order: dict[str, int]) -> int:
    """Where in the links file the first link that made `entry` is (a repost's link makes the post it reposts)."""
    members = {str(t.get("id")) for t in entry.tweets}
    for tweet_id in ids:  # in the file's order: the first that belongs is the one
        repost = (found.get(tweet_id) or {}).get("retweetedTweet")
        own = str(repost.get("id")) if isinstance(repost, dict) else tweet_id
        if tweet_id in members or own in members:
            return order.get(tweet_id, 0)
    return 0


def fit_to_window(pages: list[WebPage], context: int | None, system: str) -> list[WebPage]:
    """Cuts each page too long for the model's window (`context` tokens, from the server) to its first words, a
    round thousand, reckoned at the most tokens a word measured: the server never turns a page down for its length.
    Returns the pages cut. Nothing is cut when the window is not known."""
    if not context:
        return []
    room = context - MAX_TOKENS - WINDOW_TOKENS_PER_WORD * (len(system.split()) + HEADER_WORDS)
    most = max(1000, int(room / WINDOW_TOKENS_PER_WORD) // 1000 * 1000)
    cut = [p for p in pages if not p.error and p.words > most]
    for page in cut:
        page.cut = most
    return cut


def _cut_words(text: str, words: int) -> str:
    """The first `words` words of `text`, its line breaks kept."""
    end = 0
    for k, match in enumerate(re.finditer(r"\S+", text), 1):
        end = match.end()
        if k == words:
            break
    return text[:end]


def _who(entry) -> str:
    """How a line names a link: a post by its author, a web page by its site."""
    return entry.domain if entry.kind == WEB else f"@{entry.author}"


def _read_words(entry) -> int:
    return entry.read_words if entry.kind == WEB else entry.words


# --- the model ---------------------------------------------------------------------------------------------------


def budget(words: int) -> tuple[int, int]:
    """(about how many words the summary has in all, how many key points) for a text of `words` words."""
    target = min(MAX_SUMMARY_WORDS, max(MIN_SUMMARY_WORDS, round(words * SUMMARY_SHARE)))
    points = min(MAX_POINTS, max(MIN_POINTS, round(words / WORDS_PER_POINT)))
    return target, points


def part_budget(target: int, points: int) -> tuple[int, int]:
    """(the TL;DR's words, each point's words) for a summary of `target` words in `points` points: a model keeps a
    length given per part much better than one given in all (live, 2026-11-05: ~215 words for "about 122")."""
    tldr = min(MAX_TLDR_WORDS, max(MIN_TLDR_WORDS, round(target * TLDR_SHARE)))
    return tldr, max(10, round((target - tldr) / points))


def summary_schema(points: int) -> dict:
    """The reply's shape. Every object closed, every field required: what a hosted strict mode insists on."""
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "tldr": {"type": "string"},
            "points": {
                "type": "array",
                "minItems": max(1, points - 1),
                "maxItems": points + 1,
                "items": {
                    "type": "object",
                    "properties": {"label": {"type": "string"}, "text": {"type": "string"}},
                    "required": ["label", "text"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["title", "tldr", "points"],
        "additionalProperties": False,
    }


def language_line(language: Language | None, thing: str = "post") -> str:
    """The brief's rule (`language:` in sources.yaml): a post in a language the reader reads is summarized in it,
    any other in `translate_to`. Without the setting, every post keeps its own language. `thing` is what is
    summarized: a post, or a page."""
    if language is None:
        return f"Language: write in the language the {thing} is written in."
    target = language.name(language.translate_to)
    if language.accepted == (language.translate_to,):
        return f"Language: write in {target}."
    names = " or ".join(language.name(code) for code in language.accepted)
    return f"Language: if the {thing} is written in {names}, write in that language; otherwise write in {target}."


def user_message(entry: Entry, language: Language | None) -> str:
    target, points = budget(entry.words)
    name = display_name(entry.first)
    lines = [
        f"Kind: {entry.kind_text()}",
        f"Author: {name} (@{entry.author})" if name else f"Author: @{entry.author}",
    ]
    # said again next to the name: the prompt's rule alone left "The author uses..." in 2 of 3 summaries (live)
    lines[-1] += ", to be called by name, never \"the author\" or \"the post\""
    if published_day(entry.first):
        lines.append(f"Posted: {published_day(entry.first)}")
    tldr, per_point = part_budget(target, points)
    lines += [f"Length: about {target} words in all: a TL;DR of about {tldr} words, then {points} key points of about "
              f"{per_point} words each", language_line(language)]
    if entry.media_text():
        lines.append(f"Media: {entry.media_text()}, not shown to you")
    return "\n".join(lines) + "\n\n" + entry.text


def page_message(page: WebPage, language: Language | None) -> str:
    """What the model is told of a web page (prompt_web.md says what to do with it)."""
    target, points = budget(page.read_words)
    kind = "Kind: web page"
    if page.cut:
        kind += f", cut to its first {page.cut:,} of {page.words:,} words: the rest is not shown to you"
    site = f"{page.site} ({page.domain})" if page.site and page.site.casefold() != page.domain else page.domain
    lines = [kind, f"Site: {site}"]
    if page.author:
        lines.append(f"Author: {page.author}, to be called by name, never \"the author\" or \"the page\"")
    else:
        lines.append("Author: not given, so state the ideas directly")
    if page.published:
        lines.append(f"Published: {page.published}")
    tldr, per_point = part_budget(target, points)
    lines += [(f"Length: about {target} words in all: a TL;DR of about {tldr} words, then {points} key points of "
               f"about {per_point} words each"), language_line(language, "page")]
    if page.images:
        lines.append(f"Media: {_count(page.images, 'image')}, not shown to you")
    text = _cut_words(page.text, page.cut) if page.cut else page.text
    return "\n".join(lines) + "\n\n" + (f"Title: {page.title}\n\n" if page.title else "") + text


def message_for(entry, language: Language | None) -> str:
    return page_message(entry, language) if entry.kind == WEB else user_message(entry, language)


@dataclass
class Usage:
    """The tokens the model server counted, call by call: what the run cost, to set beside what it was said to."""

    tokens_in: int = 0
    tokens_out: int = 0
    calls: int = 0

    def add(self, completion) -> None:
        if completion is None or not isinstance(completion.prompt_tokens, int):
            return
        self.tokens_in += completion.prompt_tokens
        self.tokens_out += completion.completion_tokens if isinstance(completion.completion_tokens, int) else 0
        self.calls += 1


def _one_line(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def to_summary(data: dict) -> Summary:
    points = [(_one_line(p.get("label")), _one_line(p.get("text"))) for p in data.get("points") or [] if isinstance(p, dict)]
    summary = Summary(_one_line(data.get("title")), _one_line(data.get("tldr")), [p for p in points if p[1]])
    if not summary.tldr and not summary.points:
        raise EngineError("the reply had neither a TL;DR nor key points")
    return summary


def model_error(exc: EngineError) -> str:
    message = str(exc)
    if "spent its tokens thinking" in message:  # the engine's hint names LM Studio: Venice has its own switch
        message += f" On Venice, {EXTRA_BODY_ENV_VAR} in .env turns thinking off ({ENV_EXAMPLE} has the line)."
    hint = refusal_hint(exc.status)
    return f"{message} {hint}".strip()


def summarize(entries: list, engine, system: str, language: Language | None, say, *, web_system: str = "",
              usage: Usage | None = None) -> None:
    """One model call per entry that needs one: an X post with `system` (prompt.md), a web page with `web_system`
    (prompt_web.md). A call left unanswered is asked once more; any other failure is noted on its entry and the rest
    go on, unless the server turned the key or the account down. `usage` adds up what the server counted."""
    todo = [e for e in entries if not e.error and not e.as_posted]
    stop = ""
    for n, entry in enumerate(todo, 1):
        words = _read_words(entry)
        head = f"[{n}/{len(todo)}] {_who(entry)}: {entry.kind_text()}, summary of about {budget(words)[0]:,} words"
        if stop:
            entry.error = stop
            continue
        start = time.perf_counter()
        status, again = None, ""
        told = (web_system or system) if entry.kind == WEB else system
        for attempt in (1, 2):
            try:
                data, completion = engine.complete_json(
                    told, message_for(entry, language), summary_schema(budget(words)[1]), MAX_TOKENS, TEMPERATURE,
                )
                if usage is not None:
                    usage.add(completion)
                entry.summary = to_summary(data)
                entry.error = ""
                break
            except EngineError as exc:
                if usage is not None:
                    usage.add(exc.completion)  # a reply that came back unusable was paid for all the same
                entry.error, status = model_error(exc), exc.status
                if attempt == 1 and exc.unanswered:
                    again = ", asked twice"
                    continue
                break
        took = time.perf_counter() - start
        say(f"{head} ... ok ({took:.1f} s{again})" if entry.summary else f"{head} ... FAILED{again}: {entry.error}")
        if not entry.summary and status in (401, 402, 403):
            stop = f"not asked, the server turned the run down: {entry.error}"
            if n < len(todo):
                say(f"stopping: the {len(todo) - n} others would be turned down the same way")


# --- what it costs -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Prices:
    """What the server says of the model; None for what it doesn't (a server that is not Venice says none of it)."""

    context: int | None  # the model's window, in tokens
    per_in: float | None  # USD a token sent
    per_out: float | None  # USD a token written


NO_PRICES = Prices(None, None, None)


@dataclass(frozen=True)
class Balance:
    total: float  # USD, DIEM and bundled credits together: Venice prices a token the same in each (/models)
    text: str  # "$6.03", or "$6.03 + 2.10 DIEM"
    permitted: bool  # whether Venice lets the key spend at all


def venice_get(base_url: str, path: str, api_key: str, timeout: float = VENICE_TIMEOUT) -> dict | None:
    """A GET on the model server, or None when it can't be had: the price or the balance is then only unknown,
    and the run goes on. Free: neither call is inference."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    local = (urlparse(base_url).hostname or "").lower() in LOCAL_HOSTS  # never through a proxy, as the engine
    try:
        with httpx.Client(timeout=timeout, trust_env=not local) as client:
            resp = client.get(f"{base_url}{path}", headers=headers)
        data = resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _per_token(price: object) -> float | None:
    """A Venice price ({"usd": 0.15, "diem": 0.15}, per million tokens) as USD a token."""
    usd = _number(price.get("usd")) if isinstance(price, dict) else None
    return usd / 1_000_000 if usd is not None and usd >= 0 else None


def model_prices(listing: dict | None, model: str) -> Prices:
    """The model's window and prices, from the `model_spec` of Venice's /models."""
    for item in (listing or {}).get("data") or []:
        if not isinstance(item, dict) or item.get("id") != model:
            continue
        spec = item.get("model_spec") if isinstance(item.get("model_spec"), dict) else {}
        pricing = spec.get("pricing") if isinstance(spec.get("pricing"), dict) else {}
        context = _number(spec.get("availableContextTokens"))
        return Prices(int(context) if context and context > 0 else None, _per_token(pricing.get("input")),
                      _per_token(pricing.get("output")))
    return NO_PRICES


def account_balance(answer: dict | None) -> Balance | None:
    """What the key can still spend, from Venice's /api_keys/rate_limits, which an inference key may read
    (/billing/balance wants an admin key: "Admin API key required", seen live 2026-11-10). None when it can't be
    read."""
    data = (answer or {}).get("data")
    balances = data.get("balances") if isinstance(data, dict) else None
    if not isinstance(balances, dict):
        return None
    amounts = [(str(name), _number(value)) for name, value in balances.items() if _number(value) is not None]
    if not amounts:
        return None
    shown = [_amount(name, value) for name, value in amounts if value]
    return Balance(sum(value for _, value in amounts), " + ".join(shown) or "$0.00",
                   data.get("accessPermitted") is not False)


def _amount(name: str, value: float) -> str:
    """"$6.03", "2.50 DIEM", "0.50 bundled credits"."""
    if name == "USD":
        return f"${value:,.2f}"
    return f"{value:,.2f} {name if name == 'DIEM' else name.replace('_', ' ').lower()}"


def money(usd: float) -> str:
    """A cost as it is read: 0.0124 is "1.24¢", 1.234 is "$1.23"."""
    return f"{usd * 100:.2f}¢" if usd < 1 else f"${usd:,.2f}"


def cost(tokens_in: int, tokens_out: int, prices: Prices) -> float | None:
    if prices.per_in is None or prices.per_out is None:
        return None
    return tokens_in * prices.per_in + tokens_out * prices.per_out


def estimate(prompts: list[tuple[str, str]]) -> tuple[int, int]:
    """(tokens in, tokens out) a run is reckoned at: the words of each (system, message) at TOKENS_PER_WORD, and
    REPLY_TOKENS a summary. Within about a third of what the server counts: no tokenizer is shipped here."""
    words = sum(len(system.split()) + len(message.split()) for system, message in prompts)
    return round(words * TOKENS_PER_WORD), REPLY_TOKENS * len(prompts)


def cost_line(tokens_in: int, tokens_out: int, prices: Prices, balance: Balance | None) -> str:
    """The run's estimate and what the key can still spend, in one line."""
    line = f"about {tokens_in:,} tokens in, {tokens_out:,} out"
    reckoned = cost(tokens_in, tokens_out, prices)
    line += f" ≈ {money(reckoned)}" if reckoned is not None else ", cost unknown"
    line += f" · balance {balance.text}" if balance else " · balance unknown"
    if balance and not balance.permitted:
        line += ": Venice says this key can't spend now"
    elif balance and reckoned is not None and reckoned > balance.total:
        line += ": less than this run is reckoned to cost"
    return line


def used_line(usage: Usage, prices: Prices) -> str:
    """What the run cost, as the server counted it."""
    line = f"used {usage.tokens_in:,} tokens in, {usage.tokens_out:,} out"
    spent = cost(usage.tokens_in, usage.tokens_out, prices)
    return line + (f" ≈ {money(spent)}" if spent is not None else "")


QUESTION = "Enter to summarize, s to stop: "


def go_on(ask) -> bool | None:
    """Whether to go on: Enter (or y, yes) goes on, any other answer stops. None when no one can answer (the input
    is not a terminal)."""
    try:
        answer = ask(QUESTION)
    except (EOFError, OSError):  # the input closed, or none to read from
        return None
    return answer.strip().lower() in ("", "y", "yes")


# --- summary.md --------------------------------------------------------------------------------------------------


def _count(n: int, word: str) -> str:
    return f"{n:,} {word}" if n == 1 else f"{n:,} {word}s"


def _positions(saved: list[int]) -> str:
    numbers = [str(n) for n in sorted(saved)]
    listed = numbers[0] if len(numbers) == 1 else ", ".join(numbers[:-1]) + " and " + numbers[-1]
    return f"you saved tweet{'s' if len(numbers) > 1 else ''} {listed}"


def _first_words(text: str, limit: int = 12) -> str:
    words = text.split()
    return " ".join(words[:limit]) + ("…" if len(words) > limit else "")


def entry_lines(n: int, entry: Entry) -> list[str]:
    summary = entry.summary
    title = entry.article_title or (summary.title if summary else "") or _first_words(display_text(entry.first))
    meta = [f"{ICONS[entry.kind]} {LABELS[entry.kind]}" + (f" · {len(entry.tweets)} tweets" if entry.kind == THREAD else "")]
    meta.append(f"[@{_md(entry.author)}]({entry.url})" if entry.url else f"@{_md(entry.author)}")
    if published_day(entry.first):
        meta.append(published_day(entry.first))
    meta.append(_count(entry.words, "word"))
    if entry.media_text():
        meta.append(entry.media_text() + ", not seen by the model")
    if entry.kind == THREAD and entry.saved != [1]:
        meta.append(_positions(entry.saved))
    if entry.as_posted:
        meta.append("shown as posted")
    if entry.note:
        meta.append(_md(entry.note))
    lines = [f"## {n}. {words_md(title or 'Untitled')}", " · ".join(meta), ""]
    if summary:
        if summary.tldr:
            lines += [f"**TL;DR:** {words_md(summary.tldr)}", ""]
        lines += [f"- **{words_md(label)}:** {words_md(text)}" if label else f"- {words_md(text)}"
                  for label, text in summary.points]
        if summary.points:
            lines.append("")
    else:  # shown as posted
        tweets = entry.tweets if entry.kind == THREAD else [entry.first]
        for k, tweet in enumerate(tweets, 1):
            number = f"\\[{k}\\] " if entry.kind == THREAD else ""
            for part in (display_text(tweet), quoted_line(tweet)):
                for paragraph in (p for p in part.split("\n\n") if p.strip()):
                    lines += [f"> {number}{words_md(paragraph)}", ">"]
                    number = ""
        if lines[-1] == ">":
            lines[-1] = ""
        if entry.kind == ARTICLE and entry.article_text:
            lines += [words_md(entry.article_text), ""]
    if entry.links:
        lines += ["Links in the post: " + words_md(" · ".join(entry.links)), ""]
    return lines


def _target(url: str) -> str:
    """A link from the links file, safe as the target of a markdown link: what could end it is percent-encoded
    (parse_web_links already keeps out spaces, quotes, <, > and brackets)."""
    return url.replace("\\", "%5C").replace("(", "%28").replace(")", "%29")


def page_lines(n: int, page: WebPage) -> list[str]:
    summary = page.summary
    title = page.title or (summary.title if summary else "") or page.domain
    meta = [f"{ICONS[WEB]} {LABELS[WEB]}", f"[{_md(page.domain)}]({_target(page.url)})"]
    if page.author:
        meta.append(words_md(page.author))
    if page.published:
        meta.append(page.published)
    meta.append(_count(page.words, "word") + (f", the first {page.cut:,} summarized" if page.cut else ""))
    if page.images:
        meta.append(_count(page.images, "image") + ", not seen by the model")
    if page.note:
        meta.append(words_md(page.note))
    lines = [f"## {n}. {words_md(title)}", " · ".join(meta), ""]
    if summary:
        if summary.tldr:
            lines += [f"**TL;DR:** {words_md(summary.tldr)}", ""]
        lines += [f"- **{words_md(label)}:** {words_md(text)}" if label else f"- {words_md(text)}"
                  for label, text in summary.points]
        if summary.points:
            lines.append("")
    return lines


def render(entries: list, missing: list[str], article_ids: list[str], when: datetime) -> str:
    done = [e for e in entries if e.summary or e.as_posted]
    failed = [e for e in entries if not (e.summary or e.as_posted)]
    counts = kind_counts(entries)
    links = len(entries) + len(missing) + len(article_ids)
    overview = _count(links, "link") + (": " + ", ".join(counts) if counts else "")
    summed = [e for e in done if e.summary]
    if summed:
        overview += (f" · {sum(_read_words(e) for e in summed):,} words summed up in "
                     f"{sum(e.summary.words for e in summed):,}")
    if failed:
        overview += f" · {len(failed)} not summarized"
    if missing:
        overview += f" · {len(missing)} not found"
    heading = "Read later" if any(e.kind == WEB for e in entries) else "Tweet summaries"
    lines = [f"# {heading} — {when:%Y-%m-%d %H:%M}", "", overview, ""]
    for n, entry in enumerate(done, 1):
        lines += page_lines(n, entry) if entry.kind == WEB else entry_lines(n, entry)
    if failed:
        lines += ["## Not summarized", ""]
        for entry in failed:
            if entry.kind == WEB:
                where = f"[{_md(entry.domain)}]({_target(entry.url)})"
            else:
                where = f"[@{_md(entry.author)}]({entry.url})" if entry.url else f"@{_md(entry.author)}"
            lines.append(f"- {where}, {entry.kind_text()}: {words_md(entry.error)}")
        lines.append("")
    if missing:
        lines += ["## Not found", "", "Deleted, private, or not a post's link:", ""]
        lines += [f"- https://x.com/i/web/status/{tweet_id}" for tweet_id in missing] + [""]
    if article_ids:
        lines += ["## Article links", "",
                  "An x.com/i/article/ link names no post: paste the link of the post that shares the Article.", ""]
        lines += [f"- https://x.com/i/article/{article_id}" for article_id in article_ids] + [""]
    return "\n".join(lines).rstrip("\n") + "\n"


def output_path(folder: Path, when: datetime) -> Path:
    """A new file each run: a second run in the same minute gets -2, and so on."""
    stem = f"{when:%Y-%m-%d-%H%M}-summary"
    path, k = folder / f"{stem}.md", 2
    while path.exists():
        path, k = folder / f"{stem}-{k}.md", k + 1
    return path


# --- the run -----------------------------------------------------------------------------------------------------


class _NotTheFollowList(logging.Filter):
    """The engine warns that "the posts you follow" go to a remote model: true of the brief, not of this run, which
    says in its own words what it sends where."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().startswith("the model server ")


def run(
    links_path: Path, dry_run: bool = False, say=print, *, config: Path = CONFIG, env_path: Path = ENV_FILE,
    out_dir: Path = SUMMARIES_DIR, get=_get, make_engine=Engine, now: datetime | None = None, read=None,
    venice=venice_get, ask=input,
) -> int:
    if not links_path.is_file():
        if links_path.resolve() != DEFAULT_LINKS.resolve():
            raise SetupError(f"{links_path} not found")
        links_path.write_text(LINKS_HEADER, encoding="utf-8")  # the first run: nothing to copy by hand
        raise SetupError(f"made {links_path}: paste your x.com links in it (or any web page's), one per line, and run "
                         "this again")
    text = links_path.read_text(encoding="utf-8-sig")
    ids, article_ids = parse_links(text)
    urls = parse_web_links(text)
    if not ids and not article_ids and not urls:
        raise SetupError(f"no x.com post links or web links in {links_path}: paste them in, one per line")
    on_x = bool(ids or article_ids)
    env_file = read_env_file(env_path)
    x = x_settings(config, env_file, need_key=on_x)
    models = None if dry_run else model_settings(env_file)  # before any call: no quota spent on a run that can't end
    if urls and read is None:  # Node and Defuddle are needed only now, and are checked before any call too
        problem = web.missing()
        if problem:
            raise SetupError(problem)
        read = web.reader()

    entries: list = []
    missing: list[str] = []
    found: dict[str, dict] = {}
    if on_x:
        links = _count(len(ids), "x.com link" if urls else "link")  # "link" alone when there are no others
        say(f"{links} in {links_path.name}, looking them up on tweetapi.com")
        api = TweetApi(x.api_key, get=get)
        _rate_limiter.configure(x.rate_limit_per_minute)
        try:
            found = api.lookup(ids)
        except httpx.HTTPError as exc:
            raise SetupError(f"tweetapi.com: {short_error(exc)}") from None
        missing = [tweet_id for tweet_id in ids if tweet_id not in found]
        entries = collect(ids, found, api)
        say(found_line(entries, len(ids), len(found), api.calls, len(article_ids)))
        for n, entry in enumerate(entries, 1):  # a real run names only the links that went wrong
            if dry_run or entry.error or entry.note:
                say(entry_line(n, len(entries), entry))
    pages: list[WebPage] = []
    if urls:
        say(f"{_count(len(urls), 'web link')} in {links_path.name}, reading {'it' if len(urls) == 1 else 'them'} "
            "with Defuddle")
        pages = [read_page(url, read) for url in urls]
        say(read_line(pages))
        for n, page in enumerate(pages, 1):
            if dry_run or page.error or page.note:
                say(page_line(n, len(pages), page))
        order = positions(text)  # one list in the file's order: the posts and the pages as they were pasted
        entries = sorted(entries + pages,
                         key=lambda e: order.get(e.url, 0) if e.kind == WEB else saved_at(e, ids, found, order))

    todo = [e for e in entries if not e.error and not e.as_posted]
    system = PROMPT.read_text(encoding="utf-8").strip() + "\n"
    web_system = PROMPT_WEB.read_text(encoding="utf-8").strip() + "\n" if pages else ""
    settings = models
    if dry_run:
        try:
            settings = model_settings(env_file)
        except SetupError:
            settings = None  # a dry run needs no model: without one it says nothing of the cost
    prices = NO_PRICES
    if settings and todo:
        prices = model_prices(venice(settings.base_url, "/models", settings.api_key), settings.model)
        balance = account_balance(venice(settings.base_url, "/api_keys/rate_limits", settings.api_key))
        for page in fit_to_window([e for e in todo if e.kind == WEB], prices.context, web_system):
            say(f"{page.domain}: {page.words:,} words, more than the model's window holds: the first {page.cut:,} "
                "are summarized")
        prompts = [(web_system if e.kind == WEB else system, message_for(e, x.language)) for e in todo]
        say(cost_line(*estimate(prompts), prices, balance))
    if dry_run:
        return 0

    if not models.extra_body and "venice" in (urlparse(models.base_url).hostname or ""):
        say(f"warning: {EXTRA_BODY_ENV_VAR} is not set. Venice's Qwen thinks by default and may answer with nothing "
            f"({ENV_EXAMPLE} has the line)")
    if todo:
        answer = go_on(ask)
        if not answer:
            say("stopped: nothing was sent to the model, and nothing was written" if answer is False else
                "no answer (the input is not a terminal): stopped, nothing was sent to the model")
            return 0
    say(f"summarizing {len(todo)} with {models.model} on {urlparse(models.base_url).hostname}")
    quiet, log = _NotTheFollowList(), logging.getLogger("xmd")
    log.addFilter(quiet)  # only while the engine is made, which is when it warns: the logger is everyone's
    try:
        engine = make_engine(models.model, models.base_url, models.api_key, MODEL_TIMEOUT, extra_body=models.extra_body)
    finally:
        log.removeFilter(quiet)
    usage = Usage()
    with engine:
        summarize(entries, engine, system, x.language, say, web_system=web_system, usage=usage)
    if usage.calls:
        say(used_line(usage, prices))

    when = now or datetime.now(x.tz)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = output_path(out_dir, when)
    path.write_text(render(entries, missing, article_ids, when), encoding="utf-8", newline="\n")
    failed = sum(1 for e in entries if not (e.summary or e.as_posted))
    try:
        shown = path.relative_to(Path.cwd())  # readlater\summaries\... from the repo root
    except ValueError:
        shown = path
    say(f"wrote {shown}" + (f" ({failed} not summarized: listed at the end)" if failed else ""))
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Summarize the x.com posts and web pages linked in a .md file into one summary.md "
                    "(readlater/README.md).")
    ap.add_argument("links", nargs="?", default=str(DEFAULT_LINKS),
                    help="the .md file with the links (default: readlater/links.md)")
    ap.add_argument("--dry-run", action="store_true",
                    help="look the links up, read the pages and say what each one is and what the run would cost; "
                         "no model is called and nothing is written")
    args = ap.parse_args(argv)
    try:
        return run(Path(args.links), args.dry_run)
    except SetupError as exc:
        print(f"readlater: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # Ctrl+C at the question, or while it runs
        print("\nreadlater: stopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
