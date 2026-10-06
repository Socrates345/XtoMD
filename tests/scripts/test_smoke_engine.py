import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import smoke_engine  # noqa: E402


def _serve(monkeypatch, chat: httpx.Response) -> list[dict]:
    """A server that lists one model and answers every chat call with `chat`. Returns the bodies it is sent."""
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3-5-9b"}]})
        sent.append(json.loads(request.content))
        return chat

    client = httpx.Client
    monkeypatch.setattr(smoke_engine.httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(handler), **kw))
    return sent


def _reply(content: str, finish: str = "stop") -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 4600, "completion_tokens": 900},
    })


def _items(count: int) -> str:
    return json.dumps({"items": [{"headline": f"story {n}", "detail": "what happened", "ids": [n]}
                                 for n in range(1, count + 1)]})


@pytest.mark.parametrize("status, word", [(401, "API key"), (402, "spending limit"), (429, "rate limit"), (404, "--list")])
def test_a_hosted_server_that_refuses_is_explained_without_a_word_about_lm_studio(monkeypatch, capsys, status, word):
    _serve(monkeypatch, httpx.Response(status, text="no"))

    assert smoke_engine.main(["--base-url", "https://api.example.com/v1", "--api-key", "k"]) == 1
    out = capsys.readouterr().out
    assert f"HTTP {status}" in out and word in out
    assert "LM Studio" not in out and "VRAM" not in out


def test_a_local_server_that_refuses_still_points_at_lm_studio(monkeypatch, capsys):
    _serve(monkeypatch, httpx.Response(404, text="no such model"))

    assert smoke_engine.main(["--base-url", "http://127.0.0.1:1234/v1"]) == 1
    out = capsys.readouterr().out
    assert "Load it in LM Studio" in out and "VRAM before" in out


def test_the_full_chunk_is_given_the_room_a_real_chunk_of_as_many_items_gets(monkeypatch, capsys):
    sent = _serve(monkeypatch, _reply(_items(4)))

    assert smoke_engine.main(["--base-url", "https://api.example.com/v1"]) == 0
    full = sent[-1]
    assert full["max_tokens"] == 2300  # xmd.summary.runner.reply_budget for 20 items: 100 each, plus 300
    assert full["response_format"]["json_schema"]["schema"]["properties"]["items"]["maxItems"] == 20
    assert capsys.readouterr().out.count("OK") == 3


def test_a_reply_cut_off_by_the_cap_says_whether_the_items_were_too_many_or_too_long(monkeypatch, capsys):
    cut = _items(7)[:-25]  # stops inside the seventh item
    _serve(monkeypatch, _reply(cut, finish="length"))

    assert smoke_engine.main(["--base-url", "https://api.example.com/v1"]) == 1
    short, full = [line for line in capsys.readouterr().out.splitlines() if "FAIL" in line]
    assert "after 7 items where the schema allows 5: the server does not enforce maxItems" in short
    assert "after 7 item(s) of at most 20" in full and "~328 tokens an item" in full
