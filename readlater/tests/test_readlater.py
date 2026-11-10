"""readlater/summarize.py, offline: tweetapi.com and the model are stand-ins. The tweets are made up, in the shapes
seen live on 2026-11-05 (details-by-ids answers null for a post it doesn't know; a search's last page comes back
empty with a cursor still set; a post with pictures ends with links to itself)."""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import summarize as s  # noqa: E402
from xmd.core.config import Language  # noqa: E402
from xmd.summary.engine import EngineError  # noqa: E402

ENV_NAMES = ("XMD_LLM_BASE_URL", "XMD_LLM_MODEL", "XMD_LLM_API_KEY", "XMD_LLM_EXTRA_BODY", "XMD_TWEETAPI_KEY")
VENICE_ENV = (
    "XMD_LLM_BASE_URL=https://api.venice.ai/api/v1\n"
    "XMD_LLM_MODEL=some-model\n"
    "XMD_LLM_API_KEY=venice-key\n"
    """XMD_LLM_EXTRA_BODY='{"venice_parameters":{"disable_thinking":true}}'\n"""
)
LONG_TEXT = " ".join(f"word{k}" for k in range(300))  # 300 words, far over 280 characters


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
    def __init__(self, replies=None):
        self.replies = list(replies or [])  # what each call gives: a dict, or an exception to raise
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
        return reply, None


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
    assert lines[0] == "[1/3] @alice: long post, ~60 words ... FAILED: not JSON"
    assert re.fullmatch(r"\[2/3\] @alice: long post, ~60 words \.\.\. ok \(\d+\.\d s, asked twice\)", lines[1])


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


def make_setup(tmp_path, links: str, env: str = VENICE_ENV):
    (tmp_path / "links.md").write_text(links, encoding="utf-8")
    (tmp_path / "sources.yaml").write_text(
        "x:\n  tweetapi_api_key: x-key\n  tweetapi_rate_limit_per_minute: 0\ntimezone: UTC\n", encoding="utf-8")
    (tmp_path / ".env").write_text(env, encoding="utf-8")
    return dict(config=tmp_path / "sources.yaml", env_path=tmp_path / ".env", out_dir=tmp_path / "out")


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
    assert lines[:3] == ["3 links in links.md, looking them up on tweetapi.com",
                         "2 of 3 found: 1 long post, 1 post, 1 short enough to show as posted · 1 tweetapi.com call",
                         "summarizing 1 with some-model on api.venice.ai"]
    assert re.fullmatch(r"\[1/1\] @alice: long post, ~60 words \.\.\. ok \(\d+\.\d s\)", lines[3])
    assert lines[4:] == [f"wrote {written}"]  # nothing per link at lookup: none of them went wrong
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
                     "1 of 1 found: 1 long post · 1 tweetapi.com call", "[1/1] @alice: long post, 300 words → ~60"]
    assert not (tmp_path / "out").exists()


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
    assert lines[1:] == ["3 of 3 found: 1 X Article, 1 thread, 1 long post · 3 tweetapi.com calls",
                         "[1/3] @ray: X Article, 300 words → ~60", "[2/3] @zed: thread of 4 tweets, 313 words → ~63",
                         "[3/3] @alice: long post, 300 words → ~60"]


def test_a_real_run_names_only_the_links_that_went_wrong(tmp_path):
    paths = make_setup(tmp_path, "https://x.com/alice/status/1\nhttps://x.com/alice/status/2\n")
    x = FakeX([tw(1, LONG_TEXT, reply_count=0), tw(2, LONG_TEXT, reply_count=4)], fail={"/search": 429})
    lines: list[str] = []
    s.run(tmp_path / "links.md", False, lines.append, get=x, make_engine=FakeEngine(), **paths)
    assert lines[1:3] == [
        "2 of 2 found: 2 long posts · 2 tweetapi.com calls",
        "[2/2] @alice: long post, 300 words → ~60 · thread not checked (HTTP 429, rate limit, even after retries: "
        "wait a minute and run it again)",
    ]
    assert lines[3] == "summarizing 2 with some-model on api.venice.ai"


def test_entry_line_for_each_case():
    short = s.Entry(s.POST, [tw(1, "two words")], [1])
    failed = s.Entry(s.ARTICLE, [tw(2, "")], [1], error="no text")
    assert s.entry_line(1, 2, short) == "[1/2] @alice: post, 2 words → shown as posted"
    assert s.entry_line(2, 2, failed) == "[2/2] @alice: X Article, 0 words: no text"
