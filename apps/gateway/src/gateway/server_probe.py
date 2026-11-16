"""Probing a self-hosted OpenAI-compatible server (vLLM, SGLang, llama.cpp).

What a self-hosted server can do depends on how it was started, and nothing in
``/v1/models`` says so: a vLLM started without ``--tool-call-parser`` accepts a
tool request and answers with the model's raw tool markup as plain text, and a
llama.cpp without ``--jinja`` cannot parse one at all. An administrator adding
such a model by hand has no way to know, and the chat then fails in ways that look
like the model's fault. This module asks the server, once, when its models are
listed, imported or refreshed in the console, and reports what it found.

**It proposes; it never decides.** The result is values the console shows and the
administrator confirms (``context_window``, ``supported_features``,
``input_modalities``) plus warnings. Nothing here writes to the database, so a
value an administrator set is never overwritten: the callers only offer a
proposal where the model has no value of its own.

**These requests are not metered.** They go through the control-plane HTTP client
straight to the administrator's own server, never through ``routers/_metered``,
so they create no usage record, reserve no quota and bill nobody. They cost
tokens on the administrator's own hardware only, which is why the tool request
is one small non-streaming call (``max_tokens`` 128) and the whole probe is at
most one identity request and one chat request per model, each time-bounded.

The parsing is separate from the HTTP so each server's response can be tested
from a recorded fixture.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)

VLLM: Final = "vllm"
SGLANG: Final = "sglang"
LLAMACPP: Final = "llamacpp"
FLAVOURS: Final = (VLLM, SGLANG, LLAMACPP)

LABELS: Final = {VLLM: "vLLM", SGLANG: "SGLang", LLAMACPP: "llama.cpp"}

#: The most models probed in one listing. A llama.cpp router or a vLLM behind a
#: proxy can list many; each probe is a (tiny) generation on the administrator's
#: own server, and nobody needs forty of them at once.
MAX_PROBED_MODELS: Final = 8
#: Generations in flight at once. A llama-server often has two slots.
CONCURRENCY: Final = 2
IDENTITY_TIMEOUT: Final = httpx.Timeout(5.0, connect=3.0)
#: A CPU server may need most of this to evaluate a tool-laden prompt once.
TOOL_TIMEOUT: Final = httpx.Timeout(60.0, connect=3.0)

#: What each server needs started with, to parse tool calls and reasoning.
TOOL_FLAGS: Final = {
    VLLM: "--enable-auto-tool-choice --tool-call-parser <parser>",
    SGLANG: "--tool-call-parser <parser>",
    LLAMACPP: "--jinja",
}
REASONING_FLAGS: Final = {
    VLLM: "--reasoning-parser <parser>",
    SGLANG: "--reasoning-parser <parser>",
    LLAMACPP: "--reasoning-format deepseek",
}

#: Tool-call markup a model emits when the server's parser did not take it out of
#: the text: ``(marker, {flavour: parser name})``. The parser names are the ones
#: each server documents for that model family; a flavour with no entry gets the
#: generic advice (the parser for the model).
_TOOL_MARKUP: Final[tuple[tuple[str, Mapping[str, str]], ...]] = (
    # DeepSeek's DSML markup uses FULLWIDTH VERTICAL LINE (U+FF5C).
    ("<\uff5cDSML\uff5c", {SGLANG: "deepseekv4"}),
    ("<tool_call>", {VLLM: "hermes", SGLANG: "qwen25"}),
    ("<|tool_call_begin|>", {VLLM: "kimi_k2", SGLANG: "kimi_k2"}),
    ("[TOOL_CALLS]", {VLLM: "mistral", SGLANG: "mistral"}),
    ("<|python_tag|>", {VLLM: "llama3_json", SGLANG: "llama3"}),
    ("<function=", {}),
)
_THINK_MARKUP: Final = ("<think>", "</think>")

#: The tool the probe offers. Its only job is to make a tool-capable model call it.
_PROBE_TOOL: Final = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "The city name."}},
            "required": ["city"],
        },
    },
}


@dataclass(frozen=True, slots=True)
class ServerIdentity:
    """What the server says it is. Every field is optional: a server that
    answers nothing is still usable, it is just not described."""

    flavour: str | None = None
    version: str | None = None
    #: llama.cpp: ``/props`` ``default_generation_settings.n_ctx``, which is the
    #: context of one slot (the configured context divided by ``-np``).
    context_window: int | None = None
    slots: int | None = None

    @property
    def label(self) -> str | None:
        if self.flavour is None:
            return None
        name = LABELS[self.flavour]
        return f"{name} {self.version}" if self.version else name


@dataclass(frozen=True, slots=True)
class ModelProbe:
    """What one model on the server turned out to support."""

    #: ``True``: the server returned proper ``tool_calls``. ``False``: tools are
    #: present but the server's parser is missing (markup in the answer, or the
    #: server refused the request for want of the flag). ``None``: unknown.
    tools: bool | None = None
    #: ``True`` when the server separated reasoning from the answer or the model
    #: visibly reasons; ``None`` when the probe did not see any.
    reasoning: bool | None = None
    context_window: int | None = None
    warnings: tuple[str, ...] = ()

    @property
    def supported_features(self) -> tuple[str, ...]:
        features: list[str] = []
        if self.tools:
            features.append("tools")
        if self.reasoning:
            features.append("reasoning")
        return tuple(features)

    @property
    def input_modalities(self) -> tuple[str, ...]:
        # Text is the only modality a probe can establish by asking in text.
        return ("text",)


@dataclass(frozen=True, slots=True)
class ServerProbe:
    """The whole result: the server, warnings about the server, one probe per model."""

    server: str | None = None
    warnings: tuple[str, ...] = ()
    models: Mapping[str, ModelProbe] = field(default_factory=dict)


# -- parsing: pure, tested from fixtures ---------------------------------------


def root_url(base_url: str) -> str:
    """The server's root from its OpenAI base URL: ``http://h:8000/v1`` -> ``http://h:8000``.

    ``/props``, ``/version`` and ``/server_info`` are not under ``/v1``.
    """
    parts = urlsplit(base_url.strip())
    path = parts.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def parse_identity(flavour: str, body: Any) -> ServerIdentity | None:
    """Read one server's own identity endpoint, or ``None`` if it is not that.

    * vLLM ``GET /version``: ``{"version": "0.11.0"}``.
    * SGLang ``GET /server_info`` (``/get_server_info`` before it): the launch
      arguments flat, with ``version`` among them.
    * llama.cpp ``GET /props``: ``default_generation_settings.n_ctx``,
      ``total_slots`` and ``build_info`` (``"b6123-abcdef0"``).
    """
    if not isinstance(body, dict):
        return None
    if flavour == VLLM:
        version = body.get("version")
        return ServerIdentity(VLLM, version) if isinstance(version, str) else None
    if flavour == SGLANG:
        # The launch arguments carry no marker of their own, so anything that
        # looks like SGLang's: a version string plus a server-args field.
        version = body.get("version")
        if not isinstance(version, str) and "model_path" not in body:
            return None
        return ServerIdentity(SGLANG, version if isinstance(version, str) else None)
    if flavour == LLAMACPP:
        settings = body.get("default_generation_settings")
        if not isinstance(settings, dict) and "total_slots" not in body:
            return None
        n_ctx = _positive_int(settings.get("n_ctx")) if isinstance(settings, dict) else None
        build = body.get("build_info")
        return ServerIdentity(
            LLAMACPP,
            build if isinstance(build, str) else None,
            context_window=n_ctx,
            slots=_positive_int(body.get("total_slots")),
        )
    return None


def flavour_from_models(payload: Any) -> str | None:
    """Which server a ``/v1/models`` listing came from, by its ``owned_by``.

    The fallback when the identity endpoint answered nothing: vLLM says
    ``vllm``, SGLang ``sglang`` and llama.cpp ``llamacpp``.
    """
    for entry in _model_entries(payload):
        owner = str(entry.get("owned_by") or "").lower().replace(".", "").replace("-", "")
        for flavour in FLAVOURS:
            if owner == flavour:
                return flavour
    return None


def _model_entries(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else payload
    return [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []


def context_from_models(payload: Any, model_id: str) -> int | None:
    """``max_model_len`` of one model in a vLLM or SGLang ``/v1/models`` listing."""
    for entry in _model_entries(payload):
        if entry.get("id") == model_id:
            return _positive_int(entry.get("max_model_len"))
    return None


def find_tool_markup(text: str) -> str | None:
    """The first tool-call marker found in answer text, or ``None``."""
    for marker, _ in _TOOL_MARKUP:
        if marker in text:
            return marker
    return None


def _tool_advice(flavour: str, marker: str | None) -> str:
    flags = TOOL_FLAGS[flavour]
    for known, parsers in _TOOL_MARKUP:
        if known == marker and flavour in parsers:
            return flags.replace("<parser>", parsers[flavour])
    return flags


def _error_text(body: Any) -> str:
    if isinstance(body, dict):
        for key in ("error", "message", "detail"):
            value = body.get(key)
            if isinstance(value, dict):
                value = value.get("message")
            if isinstance(value, str):
                return value
    return ""


def analyse_tool_response(flavour: str, status: int, body: Any) -> ModelProbe:
    """Judge the answer to the probe's single tool request.

    * ``tool_calls`` present: tools are supported.
    * Tool-call markup in ``content``: the model wants to call a tool and the
      server's parser is missing. Warned, with the flag to add.
    * A refusal that names the missing flag (vLLM: "requires
      --enable-auto-tool-choice and --tool-call-parser"; llama.cpp: "tools param
      requires --jinja"): the same finding.
    * Anything else: unknown. A model that simply answered in words did not
      show that it cannot call tools, so nothing is claimed.

    Reasoning is noted where it shows for free: a ``reasoning_content`` (vLLM,
    SGLang, DeepSeek) or ``reasoning`` field. ``<think>`` markup in the answer is
    reasoning the server did not separate, and is warned about.
    """
    if status >= 400:
        text = _error_text(body).lower()
        if any(
            hint in text
            for hint in ("tool-call-parser", "enable-auto-tool-choice", "--jinja", "tool choice")
        ):
            return ModelProbe(
                tools=False,
                warnings=(
                    f"The server refused a tool request. Start it with "
                    f"{TOOL_FLAGS[flavour]} to use tools.",
                ),
            )
        return ModelProbe()

    choices = body.get("choices") if isinstance(body, dict) else None
    message = choices[0].get("message") if isinstance(choices, list) and choices else None
    if not isinstance(message, dict):
        return ModelProbe()

    warnings: list[str] = []
    reasoning: bool | None = None
    for key in ("reasoning_content", "reasoning"):
        if isinstance(message.get(key), str) and message[key].strip():
            reasoning = True
    content = message.get("content")
    content = content if isinstance(content, str) else ""

    if any(mark in content for mark in _THINK_MARKUP):
        reasoning = True
        warnings.append(
            "The answer contains the model's thinking inline. Start the server with "
            f"{REASONING_FLAGS[flavour]} so it arrives as reasoning_content."
        )

    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        return ModelProbe(tools=True, reasoning=reasoning, warnings=tuple(warnings))

    marker = find_tool_markup(content)
    if marker is not None:
        warnings.append(
            f"The model wrote a tool call ({marker}) as plain text, so the server's tool-call "
            f"parser is missing. Start it with {_tool_advice(flavour, marker)}."
        )
        return ModelProbe(tools=False, reasoning=reasoning, warnings=tuple(warnings))
    return ModelProbe(reasoning=reasoning, warnings=tuple(warnings))


def tool_request(model_id: str) -> dict[str, Any]:
    """The probe's one chat request: non-streaming, tiny, deterministic."""
    return {
        "model": model_id,
        "messages": [
            {"role": "user", "content": "What is the weather in Paris? Use the get_weather tool."}
        ],
        "tools": [_PROBE_TOOL],
        "max_tokens": 128,
        "temperature": 0,
        "stream": False,
    }


# -- the requests --------------------------------------------------------------


def _headers(api_key: str | None) -> dict[str, str]:
    headers = {"accept": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    return headers


async def _get_json(http: Any, url: str, api_key: str | None) -> Any:
    try:
        response = await http.get(url, headers=_headers(api_key), timeout=IDENTITY_TIMEOUT)
        if response.status_code >= 400:
            return None
        return response.json()
    except (httpx.HTTPError, ValueError):
        return None


async def identify(
    http: Any, base_url: str, api_key: str | None, flavour: str
) -> tuple[ServerIdentity, Any]:
    """One request to the server's own identity endpoint (two for SGLang, whose
    ``/server_info`` replaced the deprecated ``/get_server_info``).

    Returns the identity and, when the identity endpoint did not answer, the
    ``/v1/models`` listing read as the fallback, so the caller need not fetch it
    again.
    """
    root = root_url(base_url)
    paths = {
        VLLM: ("/version",),
        SGLANG: ("/server_info", "/get_server_info"),
        LLAMACPP: ("/props",),
    }[flavour]
    for path in paths:
        identity = parse_identity(flavour, await _get_json(http, f"{root}{path}", api_key))
        if identity is not None:
            return identity, None
    models = await _get_json(http, f"{base_url.rstrip('/')}/models", api_key)
    return ServerIdentity(flavour_from_models(models)), models


async def _probe_model(
    http: Any, base_url: str, api_key: str | None, flavour: str, model_id: str
) -> ModelProbe:
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {**_headers(api_key), "content-type": "application/json"}
    try:
        response = await http.post(
            url, headers=headers, json=tool_request(model_id), timeout=TOOL_TIMEOUT
        )
        try:
            body = response.json()
        except ValueError:
            body = None
        return analyse_tool_response(flavour, response.status_code, body)
    except httpx.TimeoutException:
        return ModelProbe(
            warnings=(
                "The tool-call check timed out, so tool support is unknown. "
                "Try again when the server is idle.",
            )
        )
    except httpx.HTTPError as exc:
        logger.info("server probe of %s failed: %s", model_id, exc)
        return ModelProbe()


async def probe_server(
    http: Any,
    *,
    base_url: str,
    api_key: str | None,
    flavour: str,
    model_ids: Sequence[str],
    models_payload: Any = None,
) -> ServerProbe:
    """Identify the server, then check each model (at most :data:`MAX_PROBED_MODELS`).

    ``models_payload`` is the ``/v1/models`` listing when the caller has it
    already; without one, vLLM and SGLang models get no context window here
    (the listing's own ``max_model_len`` is read by the catalogue parser).
    Never raises: a server that answers nothing yields an empty probe.
    """
    identity, fallback_models = await identify(http, base_url, api_key, flavour)
    warnings: list[str] = []
    if identity.flavour is not None and identity.flavour != flavour:
        warnings.append(
            f"This looks like {LABELS[identity.flavour]}, not {LABELS[flavour]}. "
            "Check the provider's type."
        )
    served = identity.flavour or flavour

    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(model_id: str) -> tuple[str, ModelProbe]:
        async with gate:
            found = await _probe_model(http, base_url, api_key, served, model_id)
        context = identity.context_window if served == LLAMACPP else None
        if context is None and served in (VLLM, SGLANG):
            context = context_from_models(models_payload or fallback_models, model_id)
        return model_id, ModelProbe(
            tools=found.tools,
            reasoning=found.reasoning,
            context_window=context,
            warnings=found.warnings,
        )

    results = await asyncio.gather(*(one(m) for m in list(model_ids)[:MAX_PROBED_MODELS]))
    return ServerProbe(server=identity.label, warnings=tuple(warnings), models=dict(results))
