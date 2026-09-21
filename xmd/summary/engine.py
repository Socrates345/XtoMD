"""A model behind any OpenAI-compatible /v1/chat/completions endpoint.

LM Studio, llama-server, Ollama and Prism's runtime all speak it, so one small
client (httpx, already a dependency) drives every system in the bake-off the
same way. Nothing here knows about tweets; brief.py decides what is asked.

Two things a small local model gets wrong are handled here, not by the caller:
a thinking model (Qwen3.5) can spend its whole token budget reasoning, which
servers report as an empty reply, and JSON often arrives inside a code fence.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

log = logging.getLogger("xmd")

DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"  # LM Studio's local server
API_KEY_ENV_VAR = "XMD_LLM_API_KEY"  # the scripts' --api-key default: a key on the command line stays in shell history
_WARNED: set[str] = set()  # remote hosts already warned about in this process
_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


class EngineError(RuntimeError):
    """The engine gave no usable reply: unreachable, refused, empty, cut off, or not JSON."""

    def __init__(self, message: str, completion: Completion | None = None) -> None:
        super().__init__(message)
        self.completion = completion  # what did come back, when something did (finish reason, token counts)


class ModelChoiceError(ValueError):
    """--model can't be turned into one model id; the message says why and lists the options."""


def _on_this_machine(base_url: str) -> bool:
    """Whether `base_url` is a loopback address. If it is not, say so, once per server: what is sent there is the
    export, which holds posts from the accounts you follow, and the API key if there is one. Only the host is
    named, never the rest of the URL, which may carry a password."""
    host = urlparse(base_url).hostname or ""
    try:
        local = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:  # a name, not an address
        local = False
    if not local and host not in _WARNED:
        _WARNED.add(host)
        log.warning(
            "the model server %s is not on this machine: the posts you follow are sent there%s",
            host or "(no host)", ", in clear text, because its URL is http://" if base_url.startswith("http://") else "",
        )
    return local


@dataclass(frozen=True)
class Completion:
    text: str  # the answer, reasoning removed
    finish: str | None  # "stop", or "length" when the token cap cut it off
    prompt_tokens: int | None
    completion_tokens: int | None
    reasoning_chars: int  # reasoning the server kept apart, or a <think> block cut out of the reply
    seconds: float


class Engine:
    def __init__(
        self,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str = "",
        timeout: float = 600.0,
        no_think: bool = False,
        client: httpx.Client | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.no_think = no_think  # send chat_template_kwargs enable_thinking=false (not every server honours it)
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        local = _on_this_machine(base_url)  # (also warns, whoever built the client)
        # a model on this machine is never reached through a system proxy: its prompts hold the follow list
        self._client = client or httpx.Client(timeout=timeout, trust_env=not local)

    def __enter__(self) -> Engine:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def complete(
        self, system: str, user: str, schema: dict | None = None, max_tokens: int = 1500, temperature: float = 0.2,
    ) -> Completion:
        """One chat call. With a JSON `schema` the server is asked to constrain its reply to it."""
        body: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "brief", "strict": True, "schema": schema},
            }
        if self.no_think:
            body["chat_template_kwargs"] = {"enable_thinking": False}

        start = time.perf_counter()
        try:
            resp = self._client.post(f"{self.base_url}/chat/completions", json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise EngineError(f"{self.base_url}: {type(exc).__name__}: {exc}") from exc
        seconds = time.perf_counter() - start
        if resp.status_code >= 400:
            raise EngineError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
            choice = data["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise EngineError(f"unexpected reply shape: {resp.text[:300]}") from exc

        text, cut = _strip_think(message.get("content") or "")
        reasoning = len(message.get("reasoning_content") or message.get("reasoning") or "") + cut
        usage = data.get("usage") or {}
        completion = Completion(
            text, choice.get("finish_reason"), usage.get("prompt_tokens"), usage.get("completion_tokens"),
            reasoning, seconds,
        )
        if not text:
            raise EngineError(_empty_reply(completion), completion)
        return completion

    def complete_json(
        self, system: str, user: str, schema: dict, max_tokens: int = 1500, temperature: float = 0.2,
    ) -> tuple[dict, Completion]:
        completion = self.complete(system, user, schema, max_tokens, temperature)
        try:
            return parse_json(completion.text, cut_off=completion.finish == "length"), completion
        except EngineError as exc:
            exc.completion = completion
            raise


def list_models(
    base_url: str = DEFAULT_BASE_URL, api_key: str = "", timeout: float = 10.0, client: httpx.Client | None = None,
) -> list[str] | None:
    """Model ids the server offers (embedding models dropped); None if it has no listing.
    A server that is down raises EngineError, so "down" and "no /models" stay apart."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    local = _on_this_machine(base_url)
    http = client or httpx.Client(timeout=timeout, trust_env=not local)
    try:
        resp = http.get(f"{base_url.rstrip('/')}/models", headers=headers)
    except httpx.HTTPError as exc:
        raise EngineError(f"{base_url}: {type(exc).__name__}: {exc}") from exc
    finally:
        if client is None:
            http.close()
    if resp.status_code != 200:
        return None
    try:
        ids = [m["id"] for m in resp.json()["data"]]
    except (ValueError, KeyError, TypeError):
        return None
    return [i for i in ids if "embed" not in i.lower()]


def choose_model(wanted: str, models: list[str] | None) -> tuple[str, str]:
    """(the model id to send for --model `wanted`, a note to show or ""). `wanted` may be
    part of an id. Raises ModelChoiceError, listing the options, when it can't be decided."""
    if models is None:  # nothing to check against: send the name as given
        if wanted:
            return wanted, ""
        raise ModelChoiceError("the server has no model listing: name one with --model")
    if not models:
        raise ModelChoiceError("the server lists no models: download one in LM Studio (and turn on Just-In-Time loading)")
    if not wanted:
        if len(models) == 1:
            return models[0], ""
        raise ModelChoiceError(f"several models on offer, pick one with --model (part of the name is enough):\n{_bullets(models)}")
    if wanted in models:
        return wanted, ""
    hits = [m for m in models if wanted.lower() in m.lower()]
    if len(hits) == 1:
        return hits[0], ""
    if hits:
        raise ModelChoiceError(f"--model {wanted!r} fits {len(hits)} models, be more specific:\n{_bullets(hits)}")
    return wanted, f"note: no listed model fits {wanted!r}, sending it as given. On offer:\n{_bullets(models)}"


def _bullets(items: list[str]) -> str:
    return "\n".join(f"  {item}" for item in items)


def parse_json(text: str, cut_off: bool = False) -> dict:
    """The JSON object in a model reply, tolerating a code fence around it."""
    text = text.strip()
    fenced = _FENCE.match(text)
    try:
        data = json.loads(fenced.group(1) if fenced else text)
    except ValueError as exc:
        why = "the reply hit the token cap in the middle of the JSON" if cut_off else f"the reply is not JSON ({exc})"
        raise EngineError(f"{why}: {text[:80]!r}") from exc
    if not isinstance(data, dict):
        raise EngineError(f"expected a JSON object, got {type(data).__name__}")
    return data


def _strip_think(content: str) -> tuple[str, int]:
    """(reply without an inline <think> block, characters removed). A block that
    never closes means the reply ran out inside the reasoning: nothing is left."""
    text = _THINK_BLOCK.sub("", content)
    if text.lstrip().startswith("<think>"):
        text = ""
    text = text.strip()
    return text, len(content) - len(text)


def _empty_reply(completion: Completion) -> str:
    message = f"empty reply (finish_reason={completion.finish}, {completion.reasoning_chars} chars of reasoning)"
    if completion.reasoning_chars or completion.finish == "length":
        message += (
            ": the model spent its tokens thinking before it answered. Switch thinking off "
            "(LM Studio: make the first line of the model's Jinja template `{%- set enable_thinking = false %}` "
            "and reload it)"
        )
    return message
