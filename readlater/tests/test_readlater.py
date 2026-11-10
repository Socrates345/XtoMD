"""readlater/summarize.py and web.py, offline: tweetapi.com, Defuddle, Venice and the model are stand-ins. The tweets
are made up, in the shapes seen live on 2026-11-05 (details-by-ids answers null for a post it doesn't know; a
search's last page comes back empty with a cursor still set; a post with pictures ends with links to itself), and
Venice's answers in those seen live on 2026-11-10."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import summarize as s  # noqa: E402
import web  # noqa: E402
from xmd.core.config import Language  # noqa: E402
from xmd.summary.engine import Completion, EngineError  # noqa: E402

ENV_NAMES = ("XMD_LLM_BASE_URL", "XMD_LLM_MODEL", "XMD_LLM_API_KEY", "XMD_LLM_EXTRA_BODY", "XMD_TWEETAPI_KEY")
VENICE_ENV = (
    "XMD_LLM_BASE_URL=https://api.venice.ai/api/v1\n"
    "XMD_LLM_MODEL=some-model\n"
    "XMD_LLM_API_KEY=venice-key\n"
    """XMD_LLM_EXTRA_BODY='{"venice_parameters":{"disable_thinking":true}}'\n"""
)
LONG_TEXT = " ".join(f"word{k}" for k in range(300))  # 300 words, far over 280 characters
ESSAY = " ".join(f"idea{k}" for k in range(500))  # a web page's 500 words
# Venice's /models and /api_keys/rate_limits, as they answered on 2026-11-10 (trimmed)
VENICE_MODELS = {"data": [{"id": "some-model", "model_spec": {
    "availableContextTokens": 256000,
    "pricing": {"input": {"usd": 0.15, "diem": 0.15}, "output": {"usd": 1, "diem": 1}}}}]}
VENICE_LIMITS = {"data": {"accessPermitted": True, "apiTier": {"id": "paid", "isCharged": True},
                          "balances": {"USD": 6.0334955, "DIEM": 0, "BUNDLED_CREDITS": 0}, "rateLimits": []}}
COST_LINE = r"about [\d,]+ tokens in, [\d,]+ out ≈ \d+\.\d\d¢ · balance \$6\.03"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    s._rate_limiter.configure(0)  # no pacing in tests
    yield
    s._rate_limiter.configure(0)


def tw(tweet_id, text="", user="alice", name="Alice", reply_to=None, conv=None, reply_count=None, **extra) -> dict:
    tweet = {
        "id": str(tweet_id), "text": text, "author": {"username": user, "name": name},
        "conversationId": str(conv or tweet_id), "createdAt": "2026-04-02T20:42:21.000Z", "urls": [], "media": [],
        "replyTo": {"tweetId": str(reply_to[0]), "userId": "9", "username": reply_to[1]} if reply_to else None,
    }
    if reply_count is not None:
        tweet["replyCount"] = reply_count
    tweet.update(extra)
    return tweet


def http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.tweetapi.com/tw-v2/x")
    return httpx.HTTPStatusError("boom", request=request, response=httpx.Response(status, request=request))


class FakeX:
    """tweetapi.com as seen live, for the calls summarize.py makes."""

    def __init__(self, tweets=(), search=None, details=None, fail=()):
        self.tweets = {t["id"]: t for t in tweets}
        self.search = search or {}  # conversation id -> the author's tweets in it
        self.details = details or {}  # tweet id -> the full tweet
        self.fail = dict(fail)  # path -> HTTP status it answers with
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, params, key, timeout):
        path = url.split("/tw-v2", 1)[1]
        self.calls.append((path, dict(params)))
        assert key == "x-key"
        if path in self.fail:
            raise http_error(self.fail[path])
        if path == "/tweet/details-by-ids":
            return {"data": {"tweets": [self.tweets.get(i) for i in params["ids"].split(",")]}}
        if path == "/search":
            assert re.fullmatch(r"conversation_id:\d+ from:\w+", params["query"]) and params["type"] == "Latest"
            conversation = params["query"].split()[0].split(":", 1)[1]
            if params.get("cursor"):
                return {"data": [], "pagination": {"nextCursor": "more", "prevCursor": None}}
            return {"data": self.search.get(conversation, []), "pagination": {"nextCursor": "more", "prevCursor": None}}
        if path == "/tweet/details":
            return {"data": {"tweet": self.details[params["tweetId"]], "ancestors": [], "replies": []}}
        raise AssertionError(path)

    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]


class FakeEngine:
    def __init__(self, replies=None, completion=None):
        self.replies = list(replies or [])  # what each call gives: a dict, or an exception to raise
        self.completion = completion  # what the server counted, handed back with each reply
        self.asked: list[tuple[str, str, dict, int]] = []

    def __call__(self, *args, **kwargs):  # stands in for the Engine class too
        self.made = (args, kwargs)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def complete_json(self, system, user, schema, max_tokens, temperature):
        self.asked.append((system, user, schema, max_tokens))
        reply = self.replies.pop(0) if self.replies else {
            "title": "A title", "tldr": "The gist.", "points": [{"label": "One", "text": "First point."}]}
        if isinstance(reply, Exception):
            raise reply
        return reply, self.completion


class FakeVenice:
    """Venice's two free GETs: the model's prices and window, and the key's balance."""

    def __init__(self, models=VENICE_MODELS, limits=VENICE_LIMITS):
        self.answers = {"/models": models, "/api_keys/rate_limits": limits}
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, base_url, path, api_key):
        self.calls.append((base_url, path, api_key))
        return self.answers[path]


class Answers:
    """Someone at the terminal: each question gets the next answer (Enter when none is left), or an exception."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked: list[str] = []

    def __call__(self, question):
        self.asked.append(question)
        answer = self.answers.pop(0) if self.answers else ""
        if isinstance(answer, BaseException):
            raise answer
        return answer


class FakeDefuddle:
    """web.defuddle as summarize.py calls it: url -> web.Extracted, or web.PageError."""

    def __init__(self, pages):
        self.pages = pages
        self.calls: list[str] = []

    def __call__(self, url):
        self.calls.append(url)
        found = self.pages[url]
        if isinstance(found, Exception):
            raise found
        return found


def extracted(markdown=ESSAY, title="Why People Cooperate", author="David Perell", site="David Perell",
              published="2019-03-01") -> web.Extracted:
    return web.Extracted(title, author, site, published, markdown)


def thread(user="zed", size=4, start=100) -> list[dict]:
    tweets = [tw(start, f"1/ {LONG_TEXT}", user=user, reply_count=5)]
    for k in range(1, size):
        tweets.append(tw(start + k, f"{k + 1}/ more words here", user=user, conv=start,
                         reply_to=(start + k - 1, user)))
    return tweets


# --- links -------------------------------------------------------------------------------------------------------


def test_links_every_shape_once_in_order():
    text = """saved https://x.com/karpathy/status/111?s=20 and [a link](https://twitter.com/RayDalio/status/222)
    again x.com/karpathy/status/111/photo/1, https://mobile.twitter.com/i/web/status/333, https://x.com/i/status/444
    www.x.com/some_user/statuses/555 then an article https://x.com/i/article/999 twice x.com/i/article/999
    not these: https://fxtwitter.com/a/status/777 https://notx.com/a/status/888 https://x.com/a/likes"""
    assert s.parse_links(text) == (["111", "222", "333", "444", "555"], ["999"])


def test_links_none():
    assert s.parse_links("nothing here, not even x.com") == ([], [])


# --- settings ----------------------------------------------------------------------------------------------------


def test_env_file_reads_like_the_shell(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(
        b"# a comment\r\n\r\nXMD_LLM_MODEL=qwen # trailing comment\r\n"
        b"""XMD_LLM_EXTRA_BODY='{"a":{"b":true}}'\r\nexport XMD_LLM_API_KEY="k e y"\r\n#XMD_KEEP_DAYS=14\r\nnot a line\r\n"""
    )
    assert s.read_env_file(env) == {"XMD_LLM_MODEL": "qwen", "XMD_LLM_EXTRA_BODY": '{"a":{"b":true}}',
                                    "XMD_LLM_API_KEY": "k e y"}
    assert s.read_env_file(tmp_path / "missing") == {}


def test_the_shell_wins_over_the_env_file(monkeypatch):
    monkeypatch.setenv("XMD_LLM_MODEL", "from-shell")
    assert s.setting("XMD_LLM_MODEL", {"XMD_LLM_MODEL": "from-file"}) == "from-shell"
    assert s.setting("XMD_LLM_API_KEY", {"XMD_LLM_API_KEY": "from-file"}) == "from-file"


def test_model_settings_from_the_env_file(tmp_path):
    (tmp_path / ".env").write_text(VENICE_ENV, encoding="utf-8")
    got = s.model_settings(s.read_env_file(tmp_path / ".env"))
    assert got == s.ModelSettings("https://api.venice.ai/api/v1", "some-model", "venice-key",
                                  {"venice_parameters": {"disable_thinking": True}})


def test_model_settings_missing_say_where_they_go():
    with pytest.raises(s.SetupError, match="XMD_LLM_BASE_URL, XMD_LLM_MODEL, XMD_LLM_API_KEY not set.*deploy/env.example"):
        s.model_settings({})


def test_model_settings_a_local_server_needs_no_key():
    got = s.model_settings({"XMD_LLM_BASE_URL": "http://127.0.0.1:1234/v1", "XMD_LLM_MODEL": "m"})
    assert got.api_key == "" and got.extra_body == {}


def test_model_settings_bad_extra_body():
    with pytest.raises(s.SetupError, match="XMD_LLM_EXTRA_BODY"):
        s.model_settings({"XMD_LLM_BASE_URL": "http://localhost/v1", "XMD_LLM_MODEL": "m", "XMD_LLM_EXTRA_BODY": "[1]"})


def test_x_settings_key_from_yaml_then_env(tmp_path, monkeypatch):
    config = tmp_path / "sources.yaml"
    config.write_text("x:\n  tweetapi_api_key: from-yaml\n  tweetapi_rate_limit_per_minute: 60\ntimezone: UTC\n"
                      "language:\n  accepted: [en, fr]\n  translate_to: en\n", encoding="utf-8")
    got = s.x_settings(config, {"XMD_TWEETAPI_KEY": "from-env"})
    assert (got.api_key, got.rate_limit_per_minute, str(got.tz)) == ("from-yaml", 60, "UTC")
    assert got.language == Language(("en", "fr"), "en")
    config.write_text("x:\n  backend: twitterapi\n", encoding="utf-8")
    assert s.x_settings(config, {"XMD_TWEETAPI_KEY": "from-env"}).api_key == "from-env"
    assert s.x_settings(tmp_path / "none.yaml", {"XMD_TWEETAPI_KEY": "k"}).language is None


def test_x_settings_no_key(tmp_path):
    with pytest.raises(s.SetupError, match="tweetapi_api_key"):
        s.x_settings(tmp_path / "none.yaml", {})


# --- one tweet ---------------------------------------------------------------------------------------------------


def test_display_text_leaves_out_self_links_and_article_links():
    tweet = tw(5, "Look &amp; see https://t.co/abc\n\n\n\nnext https://x.com/alice/status/5/photo/1 "
                  "https://x.com/bob/status/6 http://x.com/i/article/77")
    assert s.display_text(tweet) == "Look & see\n\nnext https://x.com/bob/status/6"


def test_display_text_cuts_leading_mentions_only_when_hidden():
    reply = tw(5, "@bob @carol thanks", displayTextRange=[12, 18])
    assert s.display_text(reply) == "thanks"
    not_mentions = tw(5, "Hello there, friend", displayTextRange=[6, 19])
    assert s.display_text(not_mentions) == "Hello there, friend"


def test_display_text_keeps_a_long_post_whole():
    long_post = tw(5, LONG_TEXT, displayTextRange=[0, 275])  # where X cuts for "Show more"
    assert s.display_text(long_post) == LONG_TEXT


def test_quoted_post_is_context():
    tweet = tw(5, "So true", quotedTweet=tw(6, "The original claim", user="bob"))
    assert s.tweet_body(tweet) == "So true\n\nQuoting @bob: The original claim"


# --- what a link is ----------------------------------------------------------------------------------------------


def test_article_with_its_text_costs_no_more_calls():
    post = tw(10, "http://x.com/i/article/77", user="ray", reply_count=9,
              urls=["http://x.com/i/article/77", "https://example.com/report"],
              article={"id": "77", "title": "It&#39;s Official", "text": "Para one.\n\nPara\xa0two.", "previewText": "",
                       "coverImageUrl": None})
    x = FakeX([post])
    entries = s.collect(["10"], s.TweetApi("x-key", get=x).lookup(["10"]), s.TweetApi("x-key", get=x))
    (entry,) = entries
    assert (entry.kind, entry.article_title, entry.article_text) == (s.ARTICLE, "It's Official", "Para one.\n\nPara two.")
    assert entry.links == ["https://example.com/report"]
    assert x.paths() == ["/tweet/details-by-ids"]  # no thread search, no details call
    assert entry.text == "Title: It's Official\n\nPara one.\n\nPara two."


def test_article_without_its_text_is_fetched_whole():
    article = {"id": "77", "title": "T", "text": "", "previewText": "p", "coverImageUrl": None}
    post = tw(10, "", user="ray", article=article)
    full = dict(post, article=dict(article, text="The whole article."))
    x = FakeX([post], details={"10": full})
    api = s.TweetApi("x-key", get=x)
    (entry,) = s.collect(["10"], api.lookup(["10"]), api)
    assert entry.article_text == "The whole article." and not entry.error
    assert x.paths() == ["/tweet/details-by-ids", "/tweet/details"]


def test_article_text_that_cannot_be_had_is_said():
    post = tw(10, "", user="ray", article={"id": "77", "title": "T", "text": "", "previewText": "", "coverImageUrl": None})
    x = FakeX([post], fail={"/tweet/details": 429})
    api = s.TweetApi("x-key", get=x)
    (entry,) = s.collect(["10"], api.lookup(["10"]), api)
    assert entry.error.startswith("the Article's text could not be fetched (HTTP 429") and not entry.as_posted


def test_thread_saved_at_its_first_tweet():
    tweets = thread()
    others = [tw(200, "@zed nice", user="zed", conv=100, reply_to=(150, "bob")),  # the author answering someone
              tw(201, "great thread", user="bob", conv=100, reply_to=(101, "zed"))]  # someone else
    x = FakeX(tweets, search={"100": list(reversed(tweets[1:])) + others})
    api = s.TweetApi("x-key", get=x)
    (entry,) = s.collect(["100"], api.lookup(["100"]), api)
    assert entry.kind == s.THREAD and [t["id"] for t in entry.tweets] == ["100", "101", "102", "103"]
    assert entry.text.startswith("[1] 1/ word0") and "\n\n[4] 4/ more words here" in entry.text
    assert entry.saved == [1]
    assert x.paths() == ["/tweet/details-by-ids", "/search", "/search"]  # the empty last page ends it


def test_thread_saved_in_the_middle_finds_its_start():
    tweets = thread()
    x = FakeX(tweets, search={"100": tweets[2:]})  # search missed the first two: they are looked up
    api = s.TweetApi("x-key", get=x)
    (entry,) = s.collect(["102"], api.lookup(["102"]), api)
    assert [t["id"] for t in entry.tweets] == ["100", "101", "102", "103"] and entry.saved == [3]
    assert x.paths().count("/tweet/details-by-ids") == 3


def test_two_links_into_one_thread_make_one_entry():
    tweets = thread()
    x = FakeX(tweets, search={"100": tweets})
    api = s.TweetApi("x-key", get=x)
    ids = ["103", "100"]
    entries = s.collect(ids, api.lookup(ids), api)
    assert len(entries) == 1 and entries[0].saved == [4, 1]
    assert x.paths().count("/search") == 2  # one search, two pages: the second link costs nothing
    assert "you saved tweets 1 and 4" in "\n".join(s.entry_lines(1, entries[0]))


def test_long_post_and_post():
    long_post = tw(1, LONG_TEXT, reply_count=3)
    post = tw(2, "short and sweet", reply_count=0)  # nobody answered: it can't start a thread
    x = FakeX([long_post, post], search={"1": [long_post]})
    api = s.TweetApi("x-key", get=x)
    entries = s.collect(["1", "2"], api.lookup(["1", "2"]), api)
    assert [e.kind for e in entries] == [s.LONG, s.POST]
    assert [e.as_posted for e in entries] == [False, True]
    assert [q["query"] for p, q in x.calls if p == "/search"] == ["conversation_id:1 from:alice"] * 2


def test_a_failed_thread_search_still_gives_the_post():
    x = FakeX([tw(1, LONG_TEXT, reply_count=3)], fail={"/search": 429})
    api = s.TweetApi("x-key", get=x)
    (entry,) = s.collect(["1"], api.lookup(["1"]), api)
    assert entry.kind == s.LONG and entry.note.startswith("thread not checked (HTTP 429")


def test_a_repost_link_gives_the_original():
    original = tw(9, LONG_TEXT, user="bob", reply_count=0)
    x = FakeX([tw(1, "RT @bob: ...", retweetedTweet=original)])
    api = s.TweetApi("x-key", get=x)
    (entry,) = s.collect(["1"], api.lookup(["1"]), api)
    assert entry.author == "bob" and entry.url == "https://x.com/bob/status/9"


def test_media_is_counted_by_kind():
    entry = s.Entry(s.POST, [tw(1, "x", media=[{"type": "photo"}, {"type": "photo"}, {"type": "video"}])])
    assert entry.media_text() == "2 images and 1 video"


# --- the model ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("words, expected", [(0, (60, 3)), (610, (122, 4)), (1045, (209, 7)), (7103, (400, 10))])
def test_budget(words, expected):
    assert s.budget(words) == expected


@pytest.mark.parametrize("target, points, expected", [(60, 3, (20, 13)), (122, 4, (30, 23)), (400, 10, (40, 36))])
def test_part_budget(target, points, expected):
    assert s.part_budget(target, points) == expected


def test_language_line():
    assert s.language_line(None) == "Language: write in the language the post is written in."
    assert s.language_line(Language(("en",), "en")) == "Language: write in English."
    assert s.language_line(Language(("en", "fr"), "en")) == (
        "Language: if the post is written in English or French, write in that language; otherwise write in English.")


def test_user_message_and_schema():
    entry = s.Entry(s.THREAD, thread(), [1])
    message = s.user_message(entry, None)
    assert message.startswith("Kind: thread of 4 tweets\nAuthor: Alice (@zed), to be called by name, never \"the author\""
                              " or \"the post\"\nPosted: 2026-04-02\n"
                              "Length: about 63 words in all: a TL;DR of about 20 words, then 3 key points of about 14 "
                              "words each\n")
    schema = s.summary_schema(3)
    assert schema["properties"]["points"]["minItems"] == 2 and schema["properties"]["points"]["maxItems"] == 4
    assert schema["additionalProperties"] is False


def test_summarize_goes_on_after_a_failure_and_retries_once():
    entries = [s.Entry(s.LONG, [tw(k, LONG_TEXT)], [1]) for k in (1, 2, 3)]
    unanswered = EngineError("timeout")
    unanswered.__cause__ = httpx.ReadTimeout("slow")  # what asking again can change
    engine = FakeEngine([
        EngineError("not JSON"),
        unanswered, {"title": "T", "tldr": "Second.", "points": []},
        {"title": "T", "tldr": "Third.", "points": [{"label": "", "text": "  spaced   out  "}]},
    ])
    lines: list[str] = []
    s.summarize(entries, engine, "system", None, lines.append)
    assert entries[0].error == "not JSON" and entries[0].summary is None
    assert entries[1].summary.tldr == "Second." and entries[2].summary.points == [("", "spaced out")]
    assert len(engine.asked) == 4 and len(lines) == 3  # the retry is said on its link's line, not one of its own
    assert lines[0] == "[1/3] @alice: long post, summary of about 60 words ... FAILED: not JSON"
    assert re.fullmatch(r"\[2/3\] @alice: long post, summary of about 60 words \.\.\. ok "
                        r"\(\d+\.\d s, asked twice\)", lines[1])


def test_summarize_stops_when_the_account_is_turned_down():
    entries = [s.Entry(s.LONG, [tw(k, LONG_TEXT)], [1]) for k in (1, 2)]
    engine = FakeEngine([EngineError("HTTP 402: no credit", status=402)])
    s.summarize(entries, engine, "system", None, lambda _: None)
    assert len(engine.asked) == 1 and "provider wants payment" in entries[0].error
    assert entries[1].error.startswith("not asked")


def test_a_reply_with_nothing_in_it_fails():
    with pytest.raises(EngineError):
        s.to_summary({"title": "T", "tldr": " ", "points": []})


# --- summary.md --------------------------------------------------------------------------------------------------


def test_tweet_and_model_text_cannot_inject_markup():
    hostile = "<img src=x onerror=alert(1)> ![](https://t.example/p.gif) [[secret note]] [click](javascript:alert(1))"
    entry = s.Entry(s.LONG, [tw(1, f"# Heading {hostile} {LONG_TEXT}", user="evil_user")], [1])
    entry.summary = s.Summary(f"# {hostile}", hostile, [("<b>x</b>", f"- {hostile}")])
    page = "\n".join(s.entry_lines(1, entry))
    for raw in ("<img", "<b>", "![](", "[[secret", "[click](javascript", "\n# "):
        assert raw not in page
    assert "[@evil\\_user](https://x.com/evil_user/status/1)" in page


def test_a_short_post_is_quoted_as_posted():
    entry = s.Entry(s.POST, [tw(1, "Line one\n\nline two", quotedTweet=tw(2, "the quoted one", user="bob"))], [1])
    lines = s.entry_lines(1, entry)
    assert lines[:2] == ["## 1. Line one line two", "💬 Post · [@alice](https://x.com/alice/status/1) · 2026-04-02 · "
                         "4 words · shown as posted"]
    assert lines[2:] == ["", "> Line one", ">", "> line two", ">", "> Quoting @bob: the quoted one", ""]


def test_render_overview_and_the_rest():
    done = s.Entry(s.ARTICLE, [tw(1, "")], [1], article_title="Title", article_text=LONG_TEXT)
    done.summary = s.Summary("ignored", "Gist here.", [("Label", "A point.")])
    failed = s.Entry(s.THREAD, thread(), [1], error="HTTP 500")
    page = s.render([done, failed], ["404"], ["999"], datetime(2026, 11, 5, 19, 42, tzinfo=timezone.utc))
    assert page.startswith("# Tweet summaries — 2026-11-05 19:42\n\n4 links: 1 X Article, 1 thread · 300 words summed "
                           "up in 5 · 1 not summarized · 1 not found\n\n## 1. Title\n📄 X Article · ")
    assert "**TL;DR:** Gist here.\n\n- **Label:** A point.\n" in page
    assert "## Not summarized\n\n- [@zed](https://x.com/zed/status/100), thread of 4 tweets: HTTP 500\n" in page
    assert "## Not found\n\nDeleted, private, or not a post's link:\n\n- https://x.com/i/web/status/404\n" in page
    assert page.endswith("- https://x.com/i/article/999\n")


def test_output_never_replaces_a_summary(tmp_path):
    when = datetime(2026, 11, 5, 19, 42)
    first = s.output_path(tmp_path, when)
    first.write_text("x")
    assert first.name == "2026-11-05-1942-summary.md"
    assert s.output_path(tmp_path, when).name == "2026-11-05-1942-summary-2.md"


# --- a whole run -------------------------------------------------------------------------------------------------


def make_setup(tmp_path, links: str, env: str = VENICE_ENV, x_key: bool = True):
    """The files a run reads, and stand-ins for Venice's two GETs and for the one at the terminal (who presses
    Enter): no run in a test reaches the network or waits on the input."""
    (tmp_path / "links.md").write_text(links, encoding="utf-8")
    key = "  tweetapi_api_key: x-key\n" if x_key else ""
    (tmp_path / "sources.yaml").write_text(
        f"x:\n{key}  tweetapi_rate_limit_per_minute: 0\ntimezone: UTC\n", encoding="utf-8")
    (tmp_path / ".env").write_text(env, encoding="utf-8")
    return dict(config=tmp_path / "sources.yaml", env_path=tmp_path / ".env", out_dir=tmp_path / "out",
                venice=FakeVenice(), ask=Answers())


def test_run_writes_one_summary(tmp_path):
    paths = make_setup(tmp_path, "https://x.com/alice/status/1\nhttps://x.com/alice/status/2\nx.com/a/status/404\n")
    x = FakeX([tw(1, LONG_TEXT, reply_count=0), tw(2, "short one", reply_count=0)])
    engine = FakeEngine()
    lines: list[str] = []
    code = s.run(tmp_path / "links.md", False, lines.append, get=x, make_engine=engine,
                 now=datetime(2026, 11, 5, 19, 42, tzinfo=timezone.utc), **paths)
    assert code == 0
    assert engine.made == (("some-model", "https://api.venice.ai/api/v1", "venice-key", s.MODEL_TIMEOUT),
                           {"extra_body": {"venice_parameters": {"disable_thinking": True}}})
    assert len(engine.asked) == 1  # the short post is shown as posted
    system, user, _, max_tokens = engine.asked[0]
    assert system == s.PROMPT.read_text(encoding="utf-8").strip() + "\n" and max_tokens == s.MAX_TOKENS
    assert "Language: write in the language the post is written in." in user
    (written,) = (tmp_path / "out").iterdir()
    page = written.read_text(encoding="utf-8")
    assert written.name == "2026-11-05-1942-summary.md"
    assert page.startswith("# Tweet summaries — 2026-11-05 19:42\n\n3 links: 1 long post, 1 post · ")
    assert "## 1. A title\n📝 Long post" in page and "## 2. short one\n💬 Post" in page and "## Not found" in page
    assert lines[:2] == ["3 links in links.md, looking them up on tweetapi.com",
                         "2 of 3 found: 1 long post, 1 post, 1 short enough to show as posted · 1 tweetapi.com call"]
    assert re.fullmatch(COST_LINE, lines[2])  # said before the question, on an x.com-only run too
    assert lines[3] == "summarizing 1 with some-model on api.venice.ai"
    assert re.fullmatch(r"\[1/1\] @alice: long post, summary of about 60 words \.\.\. ok \(\d+\.\d s\)", lines[4])
    assert lines[5:] == [f"wrote {written}"]  # nothing per link at lookup: none of them went wrong
    assert paths["ask"].asked == [s.QUESTION]
    assert [path for _, path, _ in paths["venice"].calls] == ["/models", "/api_keys/rate_limits"]
    assert x.paths() == ["/tweet/details-by-ids"]  # nobody answered either post: no thread search
    assert logging.getLogger("xmd").filters == []  # the warning filter does not outlive the engine's making


def test_run_exit_code_when_something_is_not_summarized(tmp_path):
    paths = make_setup(tmp_path, "https://x.com/alice/status/1\n")
    x = FakeX([tw(1, LONG_TEXT, reply_count=0)])
    code = s.run(tmp_path / "links.md", False, lambda _: None, get=x, make_engine=FakeEngine([EngineError("bad")]),
                 **paths)
    assert code == 1
    assert "## Not summarized" in next((tmp_path / "out").iterdir()).read_text(encoding="utf-8")


def test_dry_run_needs_no_model_and_writes_nothing(tmp_path):
    paths = make_setup(tmp_path, "https://x.com/alice/status/1\n", env="")
    x = FakeX([tw(1, LONG_TEXT, reply_count=0)])
    lines: list[str] = []
    assert s.run(tmp_path / "links.md", True, lines.append, get=x, **paths) == 0
    assert lines == ["1 link in links.md, looking them up on tweetapi.com",
                     "1 of 1 found: 1 long post · 1 tweetapi.com call",
                     "[1/1] @alice: long post, 300 words → summary of about 60 words"]
    assert not (tmp_path / "out").exists()
    assert paths["venice"].calls == [] and paths["ask"].asked == []  # no model settings: no cost to say


def test_missing_model_settings_stop_before_any_call(tmp_path):
    paths = make_setup(tmp_path, "https://x.com/alice/status/1\n", env="")
    x = FakeX([tw(1, LONG_TEXT)])
    with pytest.raises(s.SetupError, match="deploy/env.example"):
        s.run(tmp_path / "links.md", False, lambda _: None, get=x, make_engine=FakeEngine(), **paths)
    assert x.calls == []


def test_tweetapi_refusing_the_key_is_one_clear_line(tmp_path):
    paths = make_setup(tmp_path, "https://x.com/alice/status/1\n")
    x = FakeX(fail={"/tweet/details-by-ids": 401})
    with pytest.raises(s.SetupError, match=r"tweetapi.com: HTTP 401, the key was turned down"):
        s.run(tmp_path / "links.md", False, lambda _: None, get=x, make_engine=FakeEngine(), **paths)


def test_the_first_run_makes_links_md(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "DEFAULT_LINKS", tmp_path / "links.md")
    with pytest.raises(s.SetupError, match="made .*links.md: paste your x.com links in it"):
        s.run(tmp_path / "links.md")
    assert (tmp_path / "links.md").read_text(encoding="utf-8") == s.LINKS_HEADER
    assert s.parse_links(s.LINKS_HEADER) == ([], [])  # the line it starts with is never taken for a link
    with pytest.raises(s.SetupError, match="no x.com post links .*paste them in"):
        s.run(tmp_path / "links.md")


def test_another_file_that_is_not_there_is_not_made(tmp_path):
    with pytest.raises(s.SetupError, match="other.md not found"):
        s.run(tmp_path / "other.md")
    assert not (tmp_path / "other.md").exists()


def test_bare_links_are_all_it_takes(tmp_path):
    """No label says what a link is: the same paste gives an Article, a thread and a long post."""
    paths = make_setup(tmp_path, "https://x.com/ray/status/10\nhttps://x.com/zed/status/100\nhttps://x.com/alice/status/1\n")
    article = tw(10, "http://x.com/i/article/77", user="ray",
                 article={"id": "77", "title": "T", "text": LONG_TEXT, "previewText": "", "coverImageUrl": None})
    tweets = thread()
    x = FakeX([article, tweets[0], tw(1, LONG_TEXT, reply_count=0)], search={"100": tweets})
    lines: list[str] = []
    assert s.run(tmp_path / "links.md", True, lines.append, get=x, **paths) == 0
    assert lines[1:5] == ["3 of 3 found: 1 X Article, 1 thread, 1 long post · 3 tweetapi.com calls",
                          "[1/3] @ray: X Article, 300 words → summary of about 60 words",
                          "[2/3] @zed: thread of 4 tweets, 313 words → summary of about 63 words",
                          "[3/3] @alice: long post, 300 words → summary of about 60 words"]
    assert re.fullmatch(COST_LINE, lines[5]) and len(lines) == 6  # a dry run says the cost, and asks nothing
    assert paths["ask"].asked == []


def test_a_real_run_names_only_the_links_that_went_wrong(tmp_path):
    paths = make_setup(tmp_path, "https://x.com/alice/status/1\nhttps://x.com/alice/status/2\n")
    x = FakeX([tw(1, LONG_TEXT, reply_count=0), tw(2, LONG_TEXT, reply_count=4)], fail={"/search": 429})
    lines: list[str] = []
    s.run(tmp_path / "links.md", False, lines.append, get=x, make_engine=FakeEngine(), **paths)
    assert lines[1:3] == [
        "2 of 2 found: 2 long posts · 2 tweetapi.com calls",
        "[2/2] @alice: long post, 300 words → summary of about 60 words · thread not checked "
        "(HTTP 429, rate limit, even after retries: wait a minute and run it again)",
    ]
    assert re.fullmatch(COST_LINE, lines[3]) and lines[4] == "summarizing 2 with some-model on api.venice.ai"


def test_entry_line_for_each_case():
    short = s.Entry(s.POST, [tw(1, "two words")], [1])
    failed = s.Entry(s.ARTICLE, [tw(2, "")], [1], error="no text")
    assert s.entry_line(1, 2, short) == "[1/2] @alice: post, 2 words → shown as posted"
    assert s.entry_line(2, 2, failed) == "[2/2] @alice: X Article, 0 words: no text"


# --- web links ---------------------------------------------------------------------------------------------------


def test_web_links_every_shape_once_in_order():
    text = """read https://paulgraham.com/brandage.html, then <https://perell.com/essay/why-people-cooperate/>
    [an essay](https://arctotherium.substack.com/p/the-closure-of-the-internet) and
    **https://en.wikipedia.org/wiki/Quartz_(crisis)**. again https://paulgraham.com/brandage.html#notes (see
    https://example.com/a?b=1&c=2) [https://example.com/a?b=1&c=2](https://example.com/a?b=1&c=2)
    not these: https://x.com/karpathy/status/111 https://x.com/a/likes https://pic.x.com/abc https://t.co/xyz
    https://fxtwitter.com/a/status/777 x.com/i/article/9 example.org/no-scheme http://localhost/notes"""
    assert s.parse_web_links(text) == [
        "https://paulgraham.com/brandage.html", "https://perell.com/essay/why-people-cooperate/",
        "https://arctotherium.substack.com/p/the-closure-of-the-internet", "https://en.wikipedia.org/wiki/Quartz_(crisis)",
        "https://example.com/a?b=1&c=2",
    ]
    assert s.parse_links(text) == (["111"], ["9"])  # the x.com side reads the same file as it always did
    assert s.parse_web_links(s.LINKS_HEADER) == []


def test_clean_page_leaves_the_words_and_the_code():
    markdown = ("## Intro\n\nA <svg viewBox=\"0 0 9 9\"><g><text>chart</text></g></svg> point, see "
                "[the source](https://a.example/b_(c)) ![](https://i.example/p.png) and <div class=x>this</div>.\n\n\n\n"
                "Use `a  <b>` here   and there  \n\n```js\nif (a<b && c>d) {\n    x  =  1\n}\n```\nafter")
    assert web.clean_page(markdown) == ("## Intro\n\nA point, see the source and this .\n\n"
                                        "Use `a  <b>` here and there\n\n```js\nif (a<b && c>d) {\n    x  =  1\n}\n```\nafter")
    assert web.image_count(markdown) == 1


def test_defuddle_runs_node_on_its_cli_and_reads_the_json(tmp_path):
    seen = {}

    def run(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        page = {"content": "# Hi\n\nwords", "title": "  The   Brand Age ", "author": "", "site": "paulgraham.com",
                "published": "2026-08-09T11:56:27+00:00", "wordCount": 2}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(page), stderr="")

    cli = tmp_path / "cli.js"
    found = web.defuddle("https://a.example/p?b=1&c=2", "C:\\node\\node.exe", cli, run)
    assert found == web.Extracted("The Brand Age", "", "paulgraham.com", "2026-08-09", "# Hi\n\nwords")
    # node itself, the link one argument of its own: no shell, no .cmd, nothing for cmd.exe to read the & of
    assert seen["argv"] == ["C:\\node\\node.exe", str(cli), "parse", "https://a.example/p?b=1&c=2", "--json",
                            "--markdown"]
    assert seen["encoding"] == "utf-8" and seen["stdin"] == subprocess.DEVNULL and seen["timeout"] == web.DEFUDDLE_TIMEOUT
    assert "shell" not in seen


@pytest.mark.parametrize("stderr, expected", [
    ("Error: Failed to fetch: 403 Forbidden\n",
     "Failed to fetch: 403 Forbidden: the site turns scripts away (Medium does), or needs a login"),
    ("Error: Not an HTML page (content-type: application/pdf)\n", "Not an HTML page (content-type: application/pdf)"),
    ("Error: fetch failed\n", "the site could not be reached (no connection, or a wrong address)"),
    ("Error: No content could be extracted from https://a.example/\n", "no text found: the page may build it with JavaScript"),
    ("", "Defuddle failed without saying why"),
])
def test_defuddle_failures_are_one_line(stderr, expected):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=stderr)

    with pytest.raises(web.PageError) as caught:
        web.defuddle("https://a.example/", "node", Path("cli.js"), run)
    assert str(caught.value) == expected


@pytest.mark.parametrize("raised, stdout, expected", [
    (subprocess.TimeoutExpired("node", 60), "", "no answer in 60 s"),
    (FileNotFoundError(2, "No such file or directory"), "", "node could not be run: No such file or directory"),
    (None, "not json", "Defuddle's answer could not be read"),
])
def test_defuddle_that_does_not_answer(raised, stdout, expected):
    def run(argv, **kwargs):
        if raised:
            raise raised
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    with pytest.raises(web.PageError, match=re.escape(expected)):
        web.defuddle("https://a.example/", "node", Path("cli.js"), run)


def test_what_is_missing_to_read_web_pages(tmp_path):
    assert "install its LTS from nodejs.org" in web.missing(node="", cli=tmp_path / "cli.js")
    assert "run `npm install` in readlater" in web.missing(node="node", cli=tmp_path / "cli.js")
    (tmp_path / "cli.js").write_text("")
    assert web.missing(node="node", cli=tmp_path / "cli.js") == ""
    assert web.missing(node="C:\\Program Files\\nodejs\\node.EXE", cli=tmp_path / "cli.js") == ""
    # a .cmd shim would run through cmd.exe, which takes a link's & for a second command
    assert "is a .CMD script" in web.missing(node="C:\\nvm\\node.CMD", cli=tmp_path / "cli.js")


def test_read_page():
    page = s.read_page("https://www.perell.com/essay/", FakeDefuddle({"https://www.perell.com/essay/": extracted(
        f"{ESSAY} [a link](https://b.example/x) ![](https://i.example/1.png) ![](https://i.example/2.png)")}))
    assert (page.domain, page.words, page.images, page.error) == ("perell.com", 502, 2, "")
    assert page.text.endswith("idea499 a link") and page.author == "David Perell"
    named = s.read_page("https://ccunpacked.dev/", lambda url: extracted(
        title="Claude Code Unpacked", author="Claude Code Unpacked", site="Claude Code Unpacked"))
    assert named.author == ""  # a page that names itself as its author: no one to call by name
    wiki = s.read_page("https://en.wikipedia.org/wiki/X", lambda url: extracted(author="Wikipedia", site="Wikipedia"))
    assert wiki.author == "Wikipedia"  # the site's own name can be its author (and a personal site's often is)
    thin = s.read_page("https://a.example/", lambda url: extracted("You need to enable JavaScript to run this app."))
    assert thin.error == "only 9 words found: the page may build its text with JavaScript, or need a login"
    assert s.read_page("https://a.example/", lambda url: extracted("")).error.startswith("no text found: ")
    failed = s.read_page("https://medium.com/@a/b", FakeDefuddle({"https://medium.com/@a/b": web.PageError("HTTP 403")}))
    assert failed.error == "HTTP 403" and failed.text == ""


def test_page_message_and_line():
    page = s.read_page("https://perell.com/essay/", lambda url: extracted(f"{ESSAY} ![](https://i.example/1.png)"))
    message = s.page_message(page, Language(("en", "fr"), "en"))
    assert message.startswith(
        "Kind: web page\nSite: David Perell (perell.com)\nAuthor: David Perell, to be called by name, never \"the "
        "author\" or \"the page\"\nPublished: 2019-03-01\nLength: about 100 words in all: a TL;DR of about 25 words, "
        "then 3 key points of about 25 words each\nLanguage: if the page is written in English or French, write in that "
        "language; otherwise write in English.\nMedia: 1 image, not shown to you\n\nTitle: Why People Cooperate\n\nidea0 ")
    plain = s.WebPage("https://paulgraham.com/brandage.html", site="paulgraham.com", text=ESSAY)
    assert "\nSite: paulgraham.com\nAuthor: not given, so state the ideas directly\nLength:" in s.page_message(plain, None)
    assert s.page_line(1, 2, page) == "[1/2] perell.com: web page, 500 words → summary of about 100 words"
    failed = s.WebPage("https://medium.com/@a/b", error="Failed to fetch: 403 Forbidden")
    assert s.page_line(2, 2, failed) == "[2/2] medium.com: web page: Failed to fetch: 403 Forbidden"


def test_a_page_too_long_for_the_window_is_cut_to_what_fits():
    long_page = s.WebPage("https://a.example/book", text=" ".join(f"w{k}" for k in range(130_000)))
    short_page = s.WebPage("https://a.example/essay", text=ESSAY)
    assert s.fit_to_window([long_page, short_page], None, "system") == []  # a window not known cuts nothing
    assert s.fit_to_window([long_page, short_page], 256_000, "") == [long_page]
    # (256,000 - 2,000 - 2 × 200) / 2 = 126,800 words fit: a round 126,000 are read
    assert (long_page.cut, long_page.read_words, short_page.cut) == (126_000, 126_000, 0)
    message = s.page_message(long_page, None)
    assert message.startswith("Kind: web page, cut to its first 126,000 of 130,000 words: the rest is not shown to you\n")
    assert message.endswith(" w125999") and "w126000" not in message
    assert s.budget(long_page.read_words) == (400, 10)


def test_web_page_cannot_inject_markup():
    hostile = "<img src=x onerror=alert(1)> ![](https://t.example/p.gif) [[secret note]] [click](javascript:alert(1))"
    page = s.WebPage("https://a.example/p?q=(x)y)", title=f"# {hostile}", author=hostile, text=ESSAY)
    page.summary = s.Summary("t", hostile, [("<b>x</b>", f"- {hostile}")])
    lines = "\n".join(s.page_lines(1, page))
    for raw in ("<img", "<b>", "![](", "[[secret", "[click](javascript", "\n# "):
        assert raw not in lines
    assert "[a.example](https://a.example/p?q=%28x%29y%29)" in lines


# --- what it costs -----------------------------------------------------------------------------------------------


def test_prices_and_balance_from_venice():
    assert s.model_prices(VENICE_MODELS, "some-model") == s.Prices(256_000, 0.15 / 1e6, 1 / 1e6)
    assert s.model_prices(VENICE_MODELS, "other-model") == s.NO_PRICES
    assert s.model_prices({"data": [{"id": "some-model"}]}, "some-model") == s.NO_PRICES  # LM Studio's listing
    assert s.model_prices(None, "some-model") == s.NO_PRICES
    assert s.account_balance(VENICE_LIMITS) == s.Balance(6.0334955, "$6.03", True)
    staking = {"data": {"accessPermitted": False, "balances": {"USD": 1, "DIEM": 2.5, "BUNDLED_CREDITS": 0.5}}}
    assert s.account_balance(staking) == s.Balance(4.0, "$1.00 + 2.50 DIEM + 0.50 bundled credits", False)
    assert s.account_balance(None) is None and s.account_balance({"error": "Admin API key required"}) is None


def test_cost_line():
    prices, balance = s.model_prices(VENICE_MODELS, "some-model"), s.account_balance(VENICE_LIMITS)
    assert s.cost_line(60_000, 3_500, prices, balance) == "about 60,000 tokens in, 3,500 out ≈ 1.25¢ · balance $6.03"
    assert s.cost_line(60_000, 3_500, s.NO_PRICES, None) == "about 60,000 tokens in, 3,500 out, cost unknown · balance unknown"
    assert s.cost_line(60_000, 3_500, prices, s.Balance(0.01, "$0.01", True)).endswith(
        " · balance $0.01: less than this run is reckoned to cost")
    assert s.cost_line(10, 700, prices, s.Balance(6, "$6.00", False)).endswith(": Venice says this key can't spend now")
    assert (s.money(0.0028), s.money(1.234)) == ("0.28¢", "$1.23")
    assert s.estimate([("one two", "three four five"), ("six", "seven")]) == (10, 1400)


def test_usage_is_what_the_server_counted():
    usage = s.Usage()
    usage.add(Completion("{}", "stop", 9_000, 600, 0, 1.0))
    usage.add(Completion("{}", "stop", 1_000, 400, 0, 1.0))
    usage.add(None)  # a call that got no answer
    usage.add(Completion("{}", "stop", None, None, 0, 1.0))  # a server that counts nothing
    assert (usage.tokens_in, usage.tokens_out, usage.calls) == (10_000, 1_000, 2)
    assert s.used_line(usage, s.model_prices(VENICE_MODELS, "some-model")) == "used 10,000 tokens in, 1,000 out ≈ 0.25¢"
    assert s.used_line(usage, s.NO_PRICES) == "used 10,000 tokens in, 1,000 out"


@pytest.mark.parametrize("answer, expected", [
    ("", True), ("  y ", True), ("YES", True), ("s", False), ("n", False), ("stop", False), ("maybe", False),
    (EOFError(), None), (OSError("no input"), None),
])
def test_go_on(answer, expected):
    assert s.go_on(Answers(answer)) is expected


# --- a whole run with web links ----------------------------------------------------------------------------------


PERELL = "https://perell.com/essay/why-people-cooperate/"
MEDIUM = "https://medium.com/@karpathy/software-2-0-a64152b37c35"


def test_run_with_web_links_keeps_the_file_order(tmp_path):
    paths = make_setup(tmp_path, f"https://x.com/alice/status/1\n[saved]({PERELL})\n{MEDIUM}\nx.com/alice/status/2\n")
    x = FakeX([tw(1, LONG_TEXT, reply_count=0), tw(2, f"{LONG_TEXT} two", reply_count=0)])
    pages = FakeDefuddle({PERELL: extracted(), MEDIUM: web.PageError("Failed to fetch: 403 Forbidden")})
    engine = FakeEngine(completion=Completion("{}", "stop", 1_000, 100, 0, 1.0))
    lines: list[str] = []
    code = s.run(tmp_path / "links.md", False, lines.append, get=x, make_engine=engine, read=pages,
                 now=datetime(2026, 11, 10, 21, 5, tzinfo=timezone.utc), **paths)
    assert code == 1  # Medium is not summarized
    assert pages.calls == [PERELL, MEDIUM]
    prompt, prompt_web = (path.read_text(encoding="utf-8").strip() + "\n" for path in (s.PROMPT, s.PROMPT_WEB))
    # in the file's order, each with its own instructions: the posts with prompt.md, the page with prompt_web.md
    assert [(system, user.split("\n", 1)[0]) for system, user, _, _ in engine.asked] == [
        (prompt, "Kind: long post"), (prompt_web, "Kind: web page"), (prompt, "Kind: long post")]
    assert lines[:5] == [
        "2 x.com links in links.md, looking them up on tweetapi.com", "2 of 2 found: 2 long posts · 1 tweetapi.com call",
        "2 web links in links.md, reading them with Defuddle", "1 of 2 read: 500 words",
        "[2/2] medium.com: web page: Failed to fetch: 403 Forbidden"]
    assert re.fullmatch(COST_LINE, lines[5]) and lines[6] == "summarizing 3 with some-model on api.venice.ai"
    assert re.fullmatch(r"\[2/3\] perell\.com: web page, summary of about 100 words \.\.\. ok \(\d+\.\d s\)", lines[8])
    assert lines[10] == "used 3,000 tokens in, 300 out ≈ 0.07¢"
    page = next((tmp_path / "out").iterdir()).read_text(encoding="utf-8")
    assert page.startswith("# Read later — 2026-11-10 21:05\n\n4 links: 2 long posts, 2 web pages · 1,101 words summed "
                           "up in 15 · 1 not summarized\n\n## 1. A title\n📝 Long post")
    assert ("## 2. Why People Cooperate\n🌐 Web page · [perell.com](https://perell.com/essay/why-people-cooperate/) · "
            "David Perell · 2019-03-01 · 500 words\n\n**TL;DR:** The gist.\n\n- **One:** First point.\n\n## 3. A title") in page
    assert page.endswith("## Not summarized\n\n- [medium.com](https://medium.com/@karpathy/software-2-0-a64152b37c35), "
                         "web page: Failed to fetch: 403 Forbidden\n")


def test_a_web_only_file_needs_no_tweetapi_key(tmp_path):
    paths = make_setup(tmp_path, f"{PERELL}\n", x_key=False)
    x = FakeX()
    lines: list[str] = []
    assert s.run(tmp_path / "links.md", False, lines.append, get=x, make_engine=FakeEngine(),
                 read=FakeDefuddle({PERELL: extracted()}), **paths) == 0
    assert x.calls == [] and lines[0] == "1 web link in links.md, reading it with Defuddle"
    (tmp_path / "links.md").write_text(f"{PERELL}\nhttps://x.com/a/status/1\n", encoding="utf-8")
    with pytest.raises(s.SetupError, match="tweetapi_api_key"):  # the key is still asked for when there are posts
        s.run(tmp_path / "links.md", False, lines.append, get=x, read=FakeDefuddle({}), **paths)
    assert x.calls == []


def test_web_links_without_node_stop_before_any_call(tmp_path, monkeypatch):
    paths = make_setup(tmp_path, f"https://x.com/alice/status/1\n{PERELL}\n")
    monkeypatch.setattr(web.shutil, "which", lambda name: None)
    x = FakeX([tw(1, LONG_TEXT)])
    with pytest.raises(s.SetupError, match=r"web links need Node.js: install its LTS from nodejs.org"):
        s.run(tmp_path / "links.md", False, lambda _: None, get=x, make_engine=FakeEngine(), **paths)
    assert x.calls == [] and paths["venice"].calls == []


def test_an_x_only_file_needs_no_node(tmp_path, monkeypatch):
    paths = make_setup(tmp_path, "https://x.com/alice/status/1\n")
    monkeypatch.setattr(web.shutil, "which", lambda name: None)
    assert s.run(tmp_path / "links.md", False, lambda _: None, get=FakeX([tw(1, LONG_TEXT, reply_count=0)]),
                 make_engine=FakeEngine(), **paths) == 0


@pytest.mark.parametrize("answer, said", [
    ("s", "stopped: nothing was sent to the model, and nothing was written"),
    (EOFError(), "no answer (the input is not a terminal): stopped, nothing was sent to the model"),
])
def test_stopping_at_the_question_sends_nothing(tmp_path, answer, said):
    paths = make_setup(tmp_path, f"{PERELL}\n")
    paths["ask"] = Answers(answer)
    engine = FakeEngine()
    lines: list[str] = []
    assert s.run(tmp_path / "links.md", False, lines.append, make_engine=engine,
                 read=FakeDefuddle({PERELL: extracted()}), **paths) == 0
    assert re.fullmatch(COST_LINE, lines[-2]) and lines[-1] == said
    assert engine.asked == [] and not hasattr(engine, "made") and not (tmp_path / "out").exists()


def test_nothing_to_summarize_asks_nothing(tmp_path):
    paths = make_setup(tmp_path, "https://x.com/alice/status/1\n")
    paths["ask"] = lambda question: pytest.fail("asked with nothing to summarize")
    assert s.run(tmp_path / "links.md", False, lambda _: None, get=FakeX([tw(1, "short one", reply_count=0)]),
                 make_engine=FakeEngine(), **paths) == 0
    assert paths["venice"].calls == []


def test_dry_run_with_web_links_says_the_cost_and_asks_nothing(tmp_path):
    paths = make_setup(tmp_path, f"{PERELL}\n{MEDIUM}\n")
    lines: list[str] = []
    assert s.run(tmp_path / "links.md", True, lines.append, make_engine=FakeEngine(),
                 read=FakeDefuddle({PERELL: extracted(), MEDIUM: web.PageError("Failed to fetch: 403 Forbidden")}),
                 **paths) == 0
    assert lines[:4] == ["2 web links in links.md, reading them with Defuddle", "1 of 2 read: 500 words",
                         "[1/2] perell.com: web page, 500 words → summary of about 100 words",
                         "[2/2] medium.com: web page: Failed to fetch: 403 Forbidden"]
    assert re.fullmatch(COST_LINE, lines[4]) and len(lines) == 5
    assert paths["ask"].asked == [] and not (tmp_path / "out").exists()


def test_a_run_cuts_a_page_the_window_cannot_hold(tmp_path):
    paths = make_setup(tmp_path, f"{PERELL}\n")
    small = {"data": [{"id": "some-model", "model_spec": {"availableContextTokens": 10_000, "pricing": {}}}]}
    paths["venice"] = FakeVenice(models=small)
    engine = FakeEngine()
    lines: list[str] = []
    essay = " ".join(f"idea{k}" for k in range(5_000))
    s.run(tmp_path / "links.md", False, lines.append, make_engine=engine, read=FakeDefuddle({PERELL: extracted(essay)}),
          now=datetime(2026, 11, 10, 21, 5, tzinfo=timezone.utc), **paths)
    # (10,000 - 2,000 - 2 × (prompt_web.md's words + 200)) / 2: some 3,400 words fit, a round 3,000 are read
    assert lines[2] == "perell.com: 5,000 words, more than the model's window holds: the first 3,000 are summarized"
    assert re.fullmatch(r"about [\d,]+ tokens in, 700 out, cost unknown · balance \$6\.03", lines[3])
    (_, user, _, _), = engine.asked
    assert user.startswith("Kind: web page, cut to its first 3,000 of 5,000 words") and user.endswith(" idea2999")
    page = next((tmp_path / "out").iterdir()).read_text(encoding="utf-8")
    assert "3,000 words summed up in" in page and " · 5,000 words, the first 3,000 summarized\n" in page
