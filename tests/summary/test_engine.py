import json
import logging

import httpx
import pytest

from xmd.summary import engine as engine_module
from xmd.summary.engine import Engine, EngineError, ModelChoiceError, choose_model, list_models, parse_json

SCHEMA = {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"]}


def _engine(handler, **kw) -> Engine:
    return Engine("qwen/qwen3.5-9b", client=httpx.Client(transport=httpx.MockTransport(handler)), **kw)


def _ok(content="ok", finish="stop", usage=None, **message):
    """A handler that always answers with `content`."""
    body = {
        "choices": [{"message": {"content": content, **message}, "finish_reason": finish}],
        "usage": usage or {"prompt_tokens": 12, "completion_tokens": 3},
    }
    return lambda request: httpx.Response(200, json=body)


def test_the_request_carries_the_model_both_messages_and_the_schema():
    seen = {}

    def handler(request):
        seen["url"], seen["body"], seen["auth"] = str(request.url), json.loads(request.content), request.headers.get("authorization")
        return _ok()(request)

    _engine(handler, api_key="secret").complete("be brief", "the posts", SCHEMA, max_tokens=99, temperature=0.1)
    assert seen["url"] == "http://127.0.0.1:1234/v1/chat/completions"
    assert seen["auth"] == "Bearer secret"
    body = seen["body"]
    assert body["model"] == "qwen/qwen3.5-9b" and body["max_tokens"] == 99 and body["temperature"] == 0.1
    assert body["messages"] == [{"role": "system", "content": "be brief"}, {"role": "user", "content": "the posts"}]
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert "chat_template_kwargs" not in body


def test_no_schema_means_no_response_format_and_no_think_is_forwarded_only_when_asked():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _ok()(request)

    _engine(handler).complete("s", "u")
    _engine(handler, no_think=True).complete("s", "u")
    assert "response_format" not in bodies[0] and "chat_template_kwargs" not in bodies[0]
    assert bodies[1]["chat_template_kwargs"] == {"enable_thinking": False}


def test_completion_reports_tokens_finish_reason_and_time():
    completion = _engine(_ok("fine", usage={"prompt_tokens": 4601, "completion_tokens": 386})).complete("s", "u")
    assert (completion.text, completion.finish) == ("fine", "stop")
    assert (completion.prompt_tokens, completion.completion_tokens) == (4601, 386)
    assert completion.seconds >= 0 and completion.reasoning_chars == 0


def test_reasoning_the_server_keeps_apart_is_counted_but_not_returned():
    completion = _engine(_ok("the answer", reasoning_content="x" * 40)).complete("s", "u")
    assert completion.text == "the answer" and completion.reasoning_chars == 40


def test_an_inline_think_block_is_cut_out_of_the_reply():
    completion = _engine(_ok("<think>hmm, let me see</think>\n\nthe answer")).complete("s", "u")
    assert completion.text == "the answer" and completion.reasoning_chars > 0


def test_a_reply_that_ran_out_inside_its_thinking_is_an_error_that_says_what_to_do():
    with pytest.raises(EngineError, match="thinking.*enable_thinking = false"):
        _engine(_ok("", finish="length", reasoning_content="Thinking Process: ...")).complete("s", "u")
    with pytest.raises(EngineError, match="empty reply"):
        _engine(_ok("<think>never closed, cut by the token cap", finish="length")).complete("s", "u")


def test_an_empty_reply_without_reasoning_is_still_an_error_but_does_not_blame_thinking():
    with pytest.raises(EngineError) as error:
        _engine(_ok("", finish="stop")).complete("s", "u")
    assert "empty reply" in str(error.value) and "thinking" not in str(error.value)


def test_http_errors_carry_status_and_body():
    with pytest.raises(EngineError, match=r"HTTP 400: .*not supported"):
        _engine(lambda r: httpx.Response(400, text="response_format not supported")).complete("s", "u")


def test_an_unreachable_server_is_an_engine_error():
    def down(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(EngineError, match="ConnectError"):
        _engine(down).complete("s", "u")


def test_an_unexpected_reply_shape_is_an_engine_error():
    with pytest.raises(EngineError, match="unexpected reply shape"):
        _engine(lambda r: httpx.Response(200, json={"nope": 1})).complete("s", "u")


def test_complete_json_parses_a_fenced_object_and_returns_the_completion():
    data, completion = _engine(_ok('```json\n{"items": []}\n```')).complete_json("s", "u", SCHEMA)
    assert data == {"items": []} and completion.finish == "stop"


def test_json_cut_by_the_token_cap_says_so():
    with pytest.raises(EngineError, match="token cap"):
        _engine(_ok('{"items": [{"headline": "cut', finish="length")).complete_json("s", "u", SCHEMA)


def test_json_that_is_not_json_or_not_an_object_is_rejected():
    with pytest.raises(EngineError, match="not JSON"):
        parse_json("here is your brief: nothing")
    with pytest.raises(EngineError, match="JSON object"):
        parse_json("[1, 2]")


def test_an_engine_error_keeps_what_came_back_before_it_went_wrong():
    with pytest.raises(EngineError) as empty:
        _engine(_ok("", finish="length", reasoning_content="Thinking...")).complete("s", "u")
    assert empty.value.completion.finish == "length" and empty.value.completion.reasoning_chars > 0
    with pytest.raises(EngineError) as cut:
        _engine(_ok('{"items": [', finish="length", usage={"prompt_tokens": 9, "completion_tokens": 700})).complete_json("s", "u", SCHEMA)
    assert cut.value.completion.completion_tokens == 700 and cut.value.completion.text == '{"items": ['


def _models_client(payload, status=200):
    return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, json=payload)))


def test_list_models_drops_embedding_models():
    payload = {"data": [{"id": "qwen/qwen3.5-9b"}, {"id": "text-embedding-nomic-embed-text-v1.5"}, {"id": "acme/other-7b-instruct"}]}
    assert list_models(client=_models_client(payload)) == ["qwen/qwen3.5-9b", "acme/other-7b-instruct"]


def test_a_server_without_a_model_listing_gives_none_but_a_down_server_raises():
    assert list_models(client=_models_client({}, status=404)) is None
    assert list_models(client=_models_client({"unexpected": True})) is None

    def down(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(EngineError, match="ConnectError"):
        list_models(client=httpx.Client(transport=httpx.MockTransport(down)))


MODELS = ["qwen/qwen3.5-9b", "acme/other-7b-chat", "acme/other-7b-instruct"]  # qwen, or something else


def test_choose_model_takes_an_exact_id_or_one_unambiguous_part_of_one():
    assert choose_model("qwen/qwen3.5-9b", MODELS) == ("qwen/qwen3.5-9b", "")
    assert choose_model("QWEN", MODELS) == ("qwen/qwen3.5-9b", "")
    assert choose_model("Instruct", MODELS) == ("acme/other-7b-instruct", "")  # the other family, by part of its id
    assert choose_model("", ["only-one"]) == ("only-one", "")


def test_choose_model_says_why_and_lists_the_options_when_it_cannot_decide():
    with pytest.raises(ModelChoiceError, match="fits 2 models") as several:
        choose_model("other", MODELS)
    options = str(several.value)
    assert "acme/other-7b-chat" in options and "acme/other-7b-instruct" in options and "qwen" not in options
    with pytest.raises(ModelChoiceError, match="several models on offer"):
        choose_model("", MODELS)
    with pytest.raises(ModelChoiceError, match="no models"):
        choose_model("x", [])
    with pytest.raises(ModelChoiceError, match="no model listing"):
        choose_model("", None)


def test_an_unlisted_name_is_sent_as_given_with_a_note():
    model, note = choose_model("llama", MODELS)
    assert model == "llama" and "no listed model fits 'llama'" in note and "qwen/qwen3.5-9b" in note
    assert choose_model("anything", None) == ("anything", "")


@pytest.fixture
def warned(monkeypatch, caplog):
    """A fresh 'already warned' memory, and the warnings the engine module logs."""
    monkeypatch.setattr(engine_module, "_WARNED", set())
    caplog.set_level(logging.WARNING, logger="xmd")
    return caplog


@pytest.mark.parametrize("base_url", [
    "http://127.0.0.1:1234/v1", "http://localhost:1234/v1", "http://[::1]:1234/v1", "http://127.0.0.2:8910/v1",
])
def test_a_server_on_this_machine_is_not_warned_about_and_ignores_proxy_settings(warned, base_url):
    """Prompts to a local model must never take a detour through a system proxy."""
    with Engine("m", base_url=base_url) as engine:
        assert engine._client.trust_env is False
    assert "not on this machine" not in warned.text


@pytest.mark.parametrize("base_url, clear_text", [
    ("https://models.example/v1", False), ("http://192.168.1.20:1234/v1", True), ("http://models.example/v1", True),
])
def test_a_server_elsewhere_is_named_in_a_warning_because_the_export_holds_the_follow_list(warned, base_url, clear_text):
    with Engine("m", base_url=base_url) as engine:
        assert engine._client.trust_env is True  # a remote server may need the proxy to be reached at all
    assert "not on this machine" in warned.text
    assert ("clear text" in warned.text) is clear_text


def test_the_warning_names_the_host_but_never_a_password_or_key_in_the_url(warned):
    with Engine("m", base_url="https://user:hunter2@models.example/v1", api_key="sk-secret"):
        pass
    assert "models.example" in warned.text
    assert "hunter2" not in warned.text and "sk-secret" not in warned.text


def test_listing_the_models_of_a_remote_server_warns_too_and_a_local_listing_does_not(warned):
    """The scripts list the models before they build an engine, so the warning must come from here first."""
    list_models("http://127.0.0.1:1234/v1", client=_models_client({"data": []}))
    assert "not on this machine" not in warned.text
    list_models("https://models.example/v1", client=_models_client({"data": []}))
    assert "models.example" in warned.text


def test_a_remote_server_is_warned_about_once_however_many_engines_and_listings_look_at_it(warned):
    list_models("https://models.example/v1", client=_models_client({"data": []}))
    for _ in range(3):
        Engine("m", base_url="https://models.example/v1").close()
    assert warned.text.count("not on this machine") == 1
