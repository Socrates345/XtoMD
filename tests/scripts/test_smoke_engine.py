import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import smoke_engine  # noqa: E402


def _serve(monkeypatch, chat: httpx.Response) -> None:
    """A server that lists one model and answers every chat call with `chat`."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3-5-9b"}]})
        return chat

    client = httpx.Client
    monkeypatch.setattr(smoke_engine.httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(handler), **kw))


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
