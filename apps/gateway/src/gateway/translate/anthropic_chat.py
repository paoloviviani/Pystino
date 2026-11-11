"""Chat completions to Anthropic's native Messages API, and back.

Why this exists: Anthropic's own OpenAI-compatibility endpoint has no prompt
caching, no thinking signatures, no PDFs. Cerea (and every other client of
``/v1/chat/completions``) speaks the OpenAI shape, so for a model whose
provider uses the ``anthropic`` plugin the gateway calls ``/v1/messages`` and
translates. Everything above the upstream call (resolution, quota,
redaction, the ledger) keeps seeing OpenAI shapes; only the HTTP call differs.
This module is the pure part: request, response and error mapping. The stream
is ``anthropic_stream``; the seam is ``anthropic_upstream``.

Documentation this was written against (read 2026-11-02, platform.claude.com
since docs.claude.com redirects there; the API version header stays
``2023-06-01``):

* https://platform.claude.com/docs/en/api/messages (parameters, blocks)
* https://platform.claude.com/docs/en/build-with-claude/prompt-caching
* https://platform.claude.com/docs/en/build-with-claude/thinking
* https://platform.claude.com/docs/en/build-with-claude/extended-thinking
* https://platform.claude.com/docs/en/build-with-claude/effort
* https://platform.claude.com/docs/en/build-with-claude/structured-outputs
* https://platform.claude.com/docs/en/build-with-claude/streaming
* https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons
* https://platform.claude.com/docs/en/build-with-claude/pdf-support
* https://platform.claude.com/docs/en/api/errors
* https://platform.claude.com/docs/en/api/models-list

What the current docs say that changes the obvious mapping (and so what the
code below does):

* ``thinking: {type: "enabled", budget_tokens}`` is a 400 from Claude 4.7 on;
  newer models take ``thinking: {type: "adaptive"}`` plus
  ``output_config.effort``. Only the 4.5 generation and older still want a
  budget. ``reasoning_effort`` is therefore translated per model family
  (``ModelTraits``).
* Claude 4.7+ and 5.x reject non-default ``temperature`` / ``top_p`` / ``top_k``
  outright, and older models reject ``temperature`` while thinking is on. The
  sampling parameters are dropped where the model would refuse them.
* Opus 5.5, Sonnet 5.5, Fable 5.1 and Mythos 5.1 reject forced tool use
  (``tool_choice`` any / tool); ``required`` degrades to ``auto`` there.
* The API itself checks that thinking blocks come back unmodified and, from
  5.1, that nothing before them changed: the gateway stores them
  (``thinking_store``) and replays them verbatim.

Decisions the brief left open, so they are written down once:

* **Mid-conversation system / developer messages** become a user text block
  ``[system message] ...`` at that position (merged into the neighbouring user
  turn). Folding them into the top-level ``system`` would rewrite the prefix,
  which breaks the cache and, on 5.1+, the signature binding of every thinking
  block after it.
* **Tool-call arguments that are not a JSON object** become
  ``{"_invalid_arguments": "<the raw string>"}``: the model sees what it wrote
  and can correct it, where ``{}`` would hide the fault.
* **The first message must be a user message**: when the conversation (after
  the system messages) opens with an assistant turn, a user text block
  ``(conversation start)`` is inserted.
* **``max_tokens`` is required**: the caller's ``max_completion_tokens`` /
  ``max_tokens``, else the model row's ``max_output_tokens``, else
  ``AnthropicSettings.default_max_tokens``.
* **Silently dropped** (Anthropic has no equivalent and a 400 would only make
  OpenAI clients fail for a setting that changes little): ``frequency_penalty``,
  ``presence_penalty``, ``seed``, ``logit_bias``, ``store``, ``service_tier``,
  ``prediction``, ``stream_options`` and ``reasoning_content`` on messages.
  Refused with a 400: ``n`` above 1, ``logprobs``, audio, non-function tools,
  ``response_format: json_object``, ``file_id`` references.
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote_to_bytes

import orjson

from gateway.config import AnthropicSettings
from gateway.errors import BadRequestError, error_payload

CacheControl = dict[str, Any]

_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
_TOOL_ID_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")
_DATA_URL = re.compile(r"^data:(?P<mime>[^;,]*)(?P<params>(?:;[^;,]*)*),(?P<data>.*)$", re.DOTALL)

#: Interleaved thinking between tool calls on the manual-budget models needs
#: this header (accepted and ignored elsewhere, per the thinking docs).
INTERLEAVED_BETA = "interleaved-thinking-2025-05-14"

#: Manual thinking budgets for ``reasoning_effort`` on the models that still
#: take ``budget_tokens`` (4.5 generation and older). 1024 is the API minimum.
_BUDGETS = {"low": 1024, "medium": 4096, "high": 16000}


class TranslationError(Exception):
    """A request that cannot be expressed as Messages; becomes a 400."""

    def __init__(self, message: str, *, param: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.param = param


# ---------------------------------------------------------------------------
# what each model accepts
# ---------------------------------------------------------------------------

_MODEL_ID = re.compile(
    r"claude-(?P<line>opus|sonnet|haiku|fable|mythos)-(?P<major>\d+)(?:-(?P<minor>\d{1,2})(?!\d))?"
)

#: Models that return a 400 for ``tool_choice`` any / tool.
_NO_FORCED_TOOLS = frozenset({("opus", 5, 5), ("sonnet", 5, 5), ("fable", 5, 1), ("mythos", 5, 1)})


@dataclass(frozen=True, slots=True)
class ModelTraits:
    """What a Claude model accepts, derived from its id.

    Derived from the id because the gateway's model row has no such fields and
    ``GET /v1/models`` capabilities are not fetched per request. An id this
    cannot place (an alias, a cloud-vendor spelling) is treated as the newest
    generation, which is the safe direction for every rule below: parameters it
    would reject are omitted rather than sent.
    """

    line: str | None
    version: tuple[int, int]
    #: ``True`` when thinking takes a ``budget_tokens`` (4.5 and older).
    manual_thinking: bool
    #: ``False`` from 4.7 on: temperature / top_p / top_k are refused.
    sampling_allowed: bool
    effort_supported: bool
    forced_tools_allowed: bool
    #: ``True`` when the model thinks without being asked (Claude 5 and later).
    thinks_by_default: bool

    @classmethod
    def of(cls, model_id: str) -> ModelTraits:
        match = _MODEL_ID.search(model_id)
        if match is None:
            return cls(None, (99, 0), False, False, True, True, True)
        line = match["line"]
        major = int(match["major"])
        minor = int(match["minor"] or 0)
        version = (major, minor)
        return cls(
            line=line,
            version=version,
            manual_thinking=version <= (4, 5),
            sampling_allowed=version <= (4, 6),
            # Effort exists from Opus 4.5 and everything from 4.6; Sonnet 4.5
            # and Haiku 4.5 do not have it.
            effort_supported=version >= (4, 6) or (line == "opus" and version == (4, 5)),
            forced_tools_allowed=(line, major, minor) not in _NO_FORCED_TOOLS,
            thinks_by_default=major >= 5,
        )

    def thinking_off(self) -> dict[str, Any] | None:
        """The ``thinking`` value that turns it off, ``None`` when it is
        already off by default or the API would reject the request."""
        line, version = self.line, self.version
        if line is None or version < (5, 0):
            return None  # off by default (4.x) or unknown: send nothing
        if (line, version) == ("sonnet", (5, 5)):
            return {"type": "between_tools"}
        if (line, version) in {("sonnet", (5, 0)), ("opus", (5, 0)), ("haiku", (5, 5))}:
            return {"type": "disabled"}
        return None  # Opus 5.5 and the Fable/Mythos models cannot turn it off


# ---------------------------------------------------------------------------
# requests
# ---------------------------------------------------------------------------


#: The most a defaulted max_tokens may be (see build_request): room for a long reply and
#: its thinking, without reserving a model's whole output limit on every turn.
DEFAULT_MAX_TOKENS_CAP = 32_000


def reject_unsupported(body: Mapping[str, Any]) -> None:
    """The refusals that need no translation, raised before anything is spent.

    Called by the route before redaction and the reservation, so a refusal
    leaves no in-progress ledger row behind.
    """
    if (n := body.get("n")) is not None and n != 1:
        raise BadRequestError("This model serves one choice: 'n' must be 1.")
    if body.get("logprobs") or body.get("top_logprobs"):
        raise BadRequestError("This model does not return log probabilities.")
    modalities = body.get("modalities")
    if body.get("audio") is not None or (
        isinstance(modalities, list) and any(m != "text" for m in modalities)
    ):
        raise BadRequestError("This model does not produce audio output.")
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict) or tool.get("type", "function") != "function":
            raise BadRequestError(
                "Only function tools are supported on this model; "
                f"got a tool of type {tool.get('type') if isinstance(tool, dict) else tool!r}."
            )
    fmt = body.get("response_format")
    if isinstance(fmt, dict) and fmt.get("type") == "json_object":
        raise BadRequestError(
            "response_format 'json_object' is not supported on this model; "
            "use 'json_schema' with a schema."
        )


def sanitize_tool_id(value: Any, *, fallback: str = "tool_0") -> str:
    """Anthropic allows ``[a-zA-Z0-9_-]+`` for a tool use id."""
    cleaned = _TOOL_ID_UNSAFE.sub("_", str(value or ""))
    return cleaned or fallback


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _decode_data_url(url: str) -> tuple[str, str, bytes]:
    """``(mime, base64 text, raw bytes)`` of a ``data:`` URL."""
    match = _DATA_URL.match(url)
    if match is None:
        raise TranslationError("Malformed data URL.")
    mime = (match["mime"] or "").lower()
    if ";base64" in match["params"].lower():
        try:
            raw = base64.b64decode(match["data"], validate=False)
        except (binascii.Error, ValueError) as exc:
            raise TranslationError("Malformed base64 in a data URL.") from exc
    else:
        raw = unquote_to_bytes(match["data"])
    return mime, base64.b64encode(raw).decode("ascii"), raw


def _with_cache(block: dict[str, Any], part: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(control := part.get("cache_control"), dict):
        block["cache_control"] = dict(control)
    return block


def _image_block(url: str) -> dict[str, Any]:
    if url.startswith("data:"):
        mime, data, _ = _decode_data_url(url)
        if mime not in _IMAGE_TYPES:
            raise TranslationError(
                f"Images must be JPEG, PNG, GIF or WebP; got {mime or 'no media type'}."
            )
        return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}
    if url.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    raise TranslationError("An image URL must be an http(s) URL or a data URL.")


def _file_block(part: Mapping[str, Any]) -> dict[str, Any]:
    file = part.get("file")
    if not isinstance(file, dict):
        raise TranslationError("A 'file' part needs a 'file' object.")
    if file.get("file_id"):
        raise TranslationError(
            "File references (file_id) are not supported on this model; send file_data.",
            param="file_id",
        )
    data = file.get("file_data")
    if not isinstance(data, str) or not data:
        raise TranslationError("A 'file' part needs 'file_data'.")
    title = file.get("filename")
    if data.startswith("data:"):
        mime, b64, raw = _decode_data_url(data)
    else:
        mime, b64 = "application/pdf", data
        try:
            raw = base64.b64decode(data, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise TranslationError("Malformed base64 in 'file_data'.") from exc
    if mime == "application/pdf":
        source: dict[str, Any] = {"type": "base64", "media_type": mime, "data": b64}
    elif mime.startswith("text/") or mime in {"application/json", "application/xml"}:
        source = {
            "type": "text",
            "media_type": "text/plain",
            "data": raw.decode("utf-8", "replace"),
        }
    else:
        raise TranslationError(
            f"Files of type {mime or 'unknown'} are not supported; send a PDF or text."
        )
    block: dict[str, Any] = {"type": "document", "source": source}
    if isinstance(title, str) and title:
        block["title"] = title[:500]
    return block


def _content_blocks(content: Any, *, allow_files: bool = True) -> list[dict[str, Any]]:
    """Blocks for a user (or tool, or system) message's ``content``."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        raise TranslationError("'content' must be a string or a list of parts.")
    blocks: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            if part:
                blocks.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            raise TranslationError("A content part must be an object.")
        kind = part.get("type")
        if kind in ("text", "input_text", "output_text"):
            if text := _text(part.get("text")):
                blocks.append(_with_cache({"type": "text", "text": text}, part))
        elif kind == "refusal":
            if text := _text(part.get("refusal")):
                blocks.append({"type": "text", "text": text})
        elif kind == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            if not isinstance(url, str):
                raise TranslationError("An 'image_url' part needs a url.")
            blocks.append(_with_cache(_image_block(url), part))
        elif kind == "file" and allow_files:
            blocks.append(_with_cache(_file_block(part), part))
        else:
            raise TranslationError(f"Content parts of type {kind!r} are not supported.")
    return blocks


def _system_blocks(content: Any) -> list[dict[str, Any]]:
    blocks = _content_blocks(content, allow_files=False)
    for block in blocks:
        if block["type"] != "text":
            raise TranslationError("System messages can only hold text.")
    return blocks


def _tool_input(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    raw = arguments if isinstance(arguments, str) else ""
    if not raw.strip():
        return {}
    try:
        parsed = orjson.loads(raw)
    except orjson.JSONDecodeError:
        return {"_invalid_arguments": raw}
    return parsed if isinstance(parsed, dict) else {"_invalid_arguments": raw}


def _tool_result(message: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": sanitize_tool_id(message.get("tool_call_id")),
    }
    content = _content_blocks(message.get("content"), allow_files=False)
    if content:
        result["content"] = content
    if isinstance(control := message.get("cache_control"), dict):
        result["cache_control"] = dict(control)
    return result


def _assistant_blocks(
    message: Mapping[str, Any], record: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    texts = _content_blocks(message.get("content"), allow_files=False)
    tools: list[dict[str, Any]] = []
    for position, call in enumerate(message.get("tool_calls") or []):
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        function = function if isinstance(function, dict) else {}
        tools.append(
            {
                "type": "tool_use",
                "id": sanitize_tool_id(call.get("id"), fallback=f"tool_{position}"),
                "name": _text(function.get("name")),
                "input": _tool_input(function.get("arguments")),
            }
        )
    if record is None:
        return [*texts, *tools]

    out: list[dict[str, Any]] = []
    pending_tools = {block["id"]: block for block in tools}
    texts_placed = False
    for item in record.get("order") or []:
        if isinstance(item, dict):
            out.append(copy.deepcopy(item))
        elif item == "text":
            if not texts_placed:
                out.extend(texts)
                texts_placed = True
        elif (
            isinstance(item, str)
            and item.startswith("tool:")
            and (block := pending_tools.pop(item[5:], None))
        ):
            out.append(block)
    if not texts_placed and texts:
        lead = 0
        while lead < len(out) and out[lead].get("type") in ("thinking", "redacted_thinking"):
            lead += 1
        out[lead:lead] = texts
    out.extend(pending_tools.values())
    return out


def _merge(turns: list[tuple[str, list[dict[str, Any]]]]) -> list[dict[str, Any]]:
    """Alternate the roles: consecutive same-role messages become one.

    In a user turn the ``tool_result`` blocks come first, which the API
    requires, so a system note that arrived between a tool call and its result
    cannot break the pairing.
    """
    merged: list[tuple[str, list[dict[str, Any]]]] = []
    for role, blocks in turns:
        if not blocks:
            continue
        if merged and merged[-1][0] == role:
            merged[-1][1].extend(blocks)
        else:
            merged.append((role, list(blocks)))
    out: list[dict[str, Any]] = []
    for role, blocks in merged:
        if role == "user":
            blocks = sorted(blocks, key=lambda b: b.get("type") != "tool_result")
        out.append({"role": role, "content": blocks})
    if not out or out[0]["role"] != "user":
        out.insert(
            0, {"role": "user", "content": [{"type": "text", "text": "(conversation start)"}]}
        )
    return out


def assistant_tool_ids(message: Mapping[str, Any]) -> list[str]:
    ids = []
    for position, call in enumerate(message.get("tool_calls") or []):
        if isinstance(call, dict):
            ids.append(sanitize_tool_id(call.get("id"), fallback=f"tool_{position}"))
    return ids


def convert_messages(
    messages: list[dict[str, Any]],
    records: Mapping[int, Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(system blocks, Messages messages)``.

    *records* maps a message's position to the stored thinking record for it.
    """
    system: list[dict[str, Any]] = []
    turns: list[tuple[str, list[dict[str, Any]]]] = []
    started = False
    for position, message in enumerate(messages):
        if not isinstance(message, dict):
            raise TranslationError("Each message must be an object.")
        role = message.get("role")
        try:
            if role in ("system", "developer"):
                blocks = _system_blocks(message.get("content"))
                if not started:
                    system.extend(blocks)
                else:
                    turns.append(
                        (
                            "user",
                            [
                                {**block, "text": f"[system message]\n{block['text']}"}
                                if index == 0
                                else block
                                for index, block in enumerate(blocks)
                            ],
                        )
                    )
                continue
            started = True
            if role == "user":
                turns.append(("user", _content_blocks(message.get("content"))))
            elif role == "assistant":
                record = (records or {}).get(position)
                turns.append(("assistant", _assistant_blocks(message, record)))
            elif role == "tool":
                turns.append(("user", [_tool_result(message)]))
            else:
                raise TranslationError(f"Messages of role {role!r} are not supported.")
        except TranslationError as exc:
            raise TranslationError(exc.message, param=f"messages[{position}]") from exc
    return system, _merge(turns)


def convert_tools(tools: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict) or not function.get("name"):
            raise TranslationError("A function tool needs a name.", param="tools")
        schema = function.get("parameters")
        schema = dict(schema) if isinstance(schema, dict) else {}
        schema.setdefault("type", "object")
        converted: dict[str, Any] = {"name": function["name"], "input_schema": schema}
        if isinstance(function.get("description"), str) and function["description"]:
            converted["description"] = function["description"]
        if function.get("strict") is True:
            converted["strict"] = True
        control = tool.get("cache_control") or function.get("cache_control")
        if isinstance(control, dict):
            converted["cache_control"] = dict(control)
        out.append(converted)
    return out


def convert_tool_choice(
    choice: Any, *, parallel: Any, traits: ModelTraits
) -> dict[str, Any] | None:
    """``None`` leaves the API default (``auto``)."""
    if choice is None or choice == "auto":
        result: dict[str, Any] | None = {"type": "auto"} if parallel is False else None
    elif choice == "none":
        return {"type": "none"}
    elif choice == "required":
        result = {"type": "any"}
    elif isinstance(choice, dict) and choice.get("type") == "function":
        name = (choice.get("function") or {}).get("name")
        if not name:
            raise TranslationError("tool_choice needs a function name.", param="tool_choice")
        result = {"type": "tool", "name": name}
    else:
        raise TranslationError("Unsupported tool_choice.", param="tool_choice")
    if result is not None and result["type"] in ("any", "tool") and not traits.forced_tools_allowed:
        # Refused by this model on every request; the closest legal thing.
        result = {"type": "auto"} if parallel is False else None
    if result is not None and parallel is False:
        result["disable_parallel_tool_use"] = True
    return result


def _normalise_schema(schema: Any) -> Any:
    """``additionalProperties: false`` on every object, which structured
    outputs require and OpenAI's strict mode already demands of the caller."""
    if isinstance(schema, dict):
        out = {key: _normalise_schema(value) for key, value in schema.items()}
        if out.get("type") == "object" and "additionalProperties" not in out:
            out["additionalProperties"] = False
        return out
    if isinstance(schema, list):
        return [_normalise_schema(item) for item in schema]
    return schema


def _user_id(body: Mapping[str, Any]) -> str | None:
    metadata = body.get("metadata")
    raw = metadata.get("user_id") if isinstance(metadata, dict) else None
    raw = raw if isinstance(raw, str) and raw else body.get("user")
    if not isinstance(raw, str) or not raw:
        return None
    if "@" in raw or len(raw) > 512 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", raw):
        # Anthropic asks for an opaque id: never a name or an email.
        return hashlib.sha256(raw.encode()).hexdigest()
    return raw


def _stop_sequences(stop: Any) -> list[str] | None:
    values = [stop] if isinstance(stop, str) else stop if isinstance(stop, list) else []
    cleaned = [v for v in values if isinstance(v, str) and v]
    return cleaned or None


def _thinking_config(
    effort: Any, *, traits: ModelTraits, max_tokens: int
) -> tuple[dict[str, Any] | None, str | None]:
    """``(thinking, output_config.effort)`` for a ``reasoning_effort``."""
    if effort is None:
        return None, None
    level = str(effort).lower()
    if level == "none":
        return traits.thinking_off(), None
    level = {"minimal": "low"}.get(level, level)
    if level not in {"low", "medium", "high", "xhigh", "max"}:
        raise TranslationError(
            "reasoning_effort must be one of none, minimal, low, medium, high, xhigh, max.",
            param="reasoning_effort",
        )
    if traits.manual_thinking:
        budget = min(_BUDGETS.get(level, _BUDGETS["high"]), max_tokens // 2)
        if budget < 1024:
            return None, None  # no room under max_tokens: answer without thinking
        thinking = {"type": "enabled", "budget_tokens": budget}
        usable = level in ("low", "medium", "high")
        return thinking, (level if traits.effort_supported and usable else None)
    if level == "xhigh" and traits.version < (4, 7):
        level = "high"
    return {"type": "adaptive", "display": "summarized"}, level


def _cacheable_block(message: dict[str, Any]) -> dict[str, Any] | None:
    for block in reversed(message["content"]):
        if block.get("type") in ("text", "image", "document", "tool_result", "tool_use"):
            if block.get("type") == "text" and not block.get("text"):
                continue
            found: dict[str, Any] = block
            return found
    return None


def _count_cache_controls(body: Mapping[str, Any]) -> int:
    count = sum(1 for tool in body.get("tools") or [] if "cache_control" in tool)
    count += sum(1 for block in body.get("system") or [] if "cache_control" in block)
    for message in body["messages"]:
        for block in message["content"]:
            count += "cache_control" in block
            if isinstance(block.get("content"), list):
                count += sum("cache_control" in inner for inner in block["content"])
    return count


def apply_prompt_cache(body: dict[str, Any], settings: AnthropicSettings) -> None:
    """Put up to four ``cache_control`` breakpoints on the request.

    Priority when slots are scarce: the newest user-role turn (so the next
    request of a tool loop reads this one), the last system block, the last
    tool, and the user-role turn before the newest (the breakpoint the previous
    request wrote, which makes a multi-step tool loop hit on every step).
    Anthropic's prefix order is tools, system, messages, so the breakpoints are
    in the order the cache walks them. Breakpoints the client wrote itself
    count against the four and are never moved; when there are any, the TTL is
    left at the default, except where a client's 1-hour breakpoint follows ours
    (``_keep_longest_first``): a 1-hour entry must precede every 5-minute one.
    A prompt below the model's minimum cacheable length is simply not cached,
    which the API does without an error.
    """
    if settings.prompt_cache == "off":
        return
    existing = _count_cache_controls(body)
    slots = 4 - existing
    if slots <= 0:
        return
    control: CacheControl = {"type": "ephemeral"}
    if settings.prompt_cache_ttl == "1h" and existing == 0:
        control["ttl"] = "1h"

    users = [m for m in body["messages"] if m["role"] == "user"]
    targets: list[dict[str, Any] | None] = [
        _cacheable_block(users[-1]) if users else None,
        body["system"][-1] if body.get("system") else None,
        body["tools"][-1] if body.get("tools") else None,
        _cacheable_block(users[-2]) if len(users) >= 2 else None,
    ]
    ours: list[dict[str, Any]] = []
    for target in targets:
        if slots <= 0:
            break
        if target is None or "cache_control" in target:
            continue
        target["cache_control"] = dict(control)
        ours.append(target)
        slots -= 1
    if existing and ours:
        _keep_longest_first(body, ours)


def _ttl(block: Mapping[str, Any]) -> str:
    """The lifetime a block's ``cache_control`` asks for, the default included."""
    control = block.get("cache_control")
    return str(control.get("ttl", "5m")) if isinstance(control, dict) else "5m"


def _in_prompt_order(body: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Every block that can carry a breakpoint, in the order the cache reads them.

    A ``tool_result`` comes before the blocks nested in its ``content``.
    """
    blocks: list[Mapping[str, Any]] = [*(body.get("tools") or []), *(body.get("system") or [])]
    for message in body["messages"]:
        for block in message["content"]:
            blocks.append(block)
            if isinstance(block.get("content"), list):
                blocks.extend(block["content"])
    return blocks


def _keep_longest_first(body: dict[str, Any], ours: list[dict[str, Any]]) -> None:
    """Lengthen our breakpoints that precede a client's 1-hour one.

    "Cache entries with longer TTL must appear before shorter TTLs (that is, a
    1-hour cache entry must appear before any 5-minute cache entries)"
    (platform.claude.com, prompt caching, "Mixing different TTLs"). Ours carry no
    ``ttl`` when the client wrote breakpoints, and the prompt order is tools,
    system, messages: so a client's 1-hour mark on the system prompt, or on any
    message, followed our 5-minute mark on the last tool, and one on a message
    followed our system one too. Dropping those breakpoints would lose the ones
    that make a tool loop hit on every step; keeping them as they were breaks
    the documented order.

    Giving them the hour is not dearer: a request that carries a 1-hour
    breakpoint is billed for everything up to its last one as 1-hour writes
    whichever breakpoint wrote it, so the tokens cost what they would anyway.
    Only a breakpoint with a 1-hour one *after* it is changed; the client's own
    are never touched, and the rest keep the default.
    """
    mine = {id(block) for block in ours}
    hour_ahead = False
    for block in reversed(_in_prompt_order(body)):
        if "cache_control" not in block:
            continue
        if id(block) in mine and hour_ahead and _ttl(block) != "1h":
            block["cache_control"]["ttl"] = "1h"
        hour_ahead = hour_ahead or _ttl(block) == "1h"


@dataclass(slots=True)
class TranslatedRequest:
    """The Messages body, plus what the caller needs to send and remember it."""

    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)


def build_request(
    payload: Mapping[str, Any],
    *,
    settings: AnthropicSettings,
    max_output_tokens: int | None,
    records: Mapping[int, Mapping[str, Any]] | None = None,
) -> TranslatedRequest:
    """The Messages request for an OpenAI chat completion request.

    *payload* is the body as the route would have sent it upstream: redacted,
    with the upstream model name substituted. *records* are the stored
    thinking blocks, by message position (``thinking_store``).
    """
    reject_unsupported(payload)
    model = str(payload.get("model", ""))
    traits = ModelTraits.of(model)

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise TranslationError("'messages' must contain at least one message.")
    system, converted = convert_messages(messages, records)

    explicit = payload.get("max_completion_tokens") or payload.get("max_tokens")
    # Anthropic requires max_tokens; an OpenAI caller usually sends none. The model's
    # own output limit can be 128k, which a chat turn never needs and which sizes no
    # sensible quota reservation, so a default is capped at DEFAULT_MAX_TOKENS_CAP.
    # An explicit value from the caller is sent as given.
    max_tokens = (
        int(explicit)
        if isinstance(explicit, int) and explicit > 0
        else min(max_output_tokens or settings.default_max_tokens, DEFAULT_MAX_TOKENS_CAP)
    )

    body: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": converted}
    if system:
        body["system"] = system
    if payload.get("stream"):
        body["stream"] = True

    tools = payload.get("tools")
    if tools:
        body["tools"] = convert_tools(tools)
        if (
            choice := convert_tool_choice(
                payload.get("tool_choice"),
                parallel=payload.get("parallel_tool_calls"),
                traits=traits,
            )
        ) is not None:
            body["tool_choice"] = choice

    thinking, effort = _thinking_config(
        payload.get("reasoning_effort"), traits=traits, max_tokens=max_tokens
    )
    forced = body.get("tool_choice", {}).get("type") in ("any", "tool")
    if forced and thinking is not None and thinking["type"] == "enabled":
        thinking = None  # manual thinking cannot be combined with forced tool use
    last = converted[-1]
    if (
        thinking is not None
        and thinking["type"] == "enabled"
        and last["role"] == "user"
        and any(b.get("type") == "tool_result" for b in last["content"])
        and len(converted) >= 2
        and converted[-2]["content"][0].get("type") not in ("thinking", "redacted_thinking")
    ):
        # Mid tool loop with no stored blocks (expired, or the loop started on
        # another deployment): manual mode would be a 400, so answer without it.
        thinking = None
    if thinking is not None:
        body["thinking"] = thinking
    if effort is not None:
        body["output_config"] = {"effort": effort}

    if traits.sampling_allowed:
        temperature, top_p = payload.get("temperature"), payload.get("top_p")
        if thinking is not None:
            # temperature is refused while thinking is on; top_p only in 0.95-1.
            temperature = None
            top_p = top_p if isinstance(top_p, (int, float)) and 0.95 <= top_p <= 1 else None
        if isinstance(temperature, (int, float)):
            body["temperature"] = temperature
        elif isinstance(top_p, (int, float)):
            body["top_p"] = top_p
    if stops := _stop_sequences(payload.get("stop")):
        body["stop_sequences"] = stops
    if user := _user_id(payload):
        body["metadata"] = {"user_id": user}

    fmt = payload.get("response_format")
    if isinstance(fmt, dict) and fmt.get("type") == "json_schema":
        spec = fmt.get("json_schema")
        schema = spec.get("schema") if isinstance(spec, dict) else None
        if not isinstance(schema, dict):
            raise TranslationError(
                "response_format json_schema needs a 'schema'.", param="response_format"
            )
        body.setdefault("output_config", {})["format"] = {
            "type": "json_schema",
            "schema": _normalise_schema(schema),
        }

    apply_prompt_cache(body, settings)

    headers: dict[str, str] = {}
    if thinking is not None and thinking["type"] == "enabled" and tools:
        headers["anthropic-beta"] = INTERLEAVED_BETA
    return TranslatedRequest(body=body, headers=headers)


# ---------------------------------------------------------------------------
# responses
# ---------------------------------------------------------------------------

_FINISH_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
    # A server-tool loop hit its iteration limit. The gateway sends no server
    # tools, so this is not expected; "stop" is the honest closest value.
    "pause_turn": "stop",
}


def finish_reason(stop_reason: Any) -> str:
    return _FINISH_REASONS.get(stop_reason, "stop") if isinstance(stop_reason, str) else "stop"


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def usage_to_openai(usage: Mapping[str, Any] | None) -> dict[str, Any]:
    """OpenAI's usage object from Anthropic's.

    ``prompt_tokens`` includes the cached tokens (the OpenAI convention, which
    is what ``TokenCounts.from_usage`` expects), where Anthropic's
    ``input_tokens`` excludes them. Cache writes are exposed as
    ``prompt_tokens_details.cache_write_tokens``, one of the spellings
    ``accounting.cost`` reads, so the ledger bills reads at the cache-read price
    and writes at the cache-write price.
    """
    usage = usage or {}
    read = _int(usage.get("cache_read_input_tokens"))
    write = _int(usage.get("cache_creation_input_tokens"))
    prompt = _int(usage.get("input_tokens")) + read + write
    completion = _int(usage.get("output_tokens"))
    written_1h = 0
    if isinstance(split := usage.get("cache_creation"), dict):
        written_1h = min(_int(split.get("ephemeral_1h_input_tokens")), write)
    out: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {"cached_tokens": read, "cache_write_tokens": write},
    }
    if written_1h:
        # The part of the writes that went to the one-hour cache, which is
        # billed at 1.6x the write rate (accounting.cost.CACHE_WRITE_1H_FACTOR).
        out["prompt_tokens_details"]["cache_write_1h_tokens"] = written_1h
    details = usage.get("output_tokens_details")
    if isinstance(details, dict) and (thinking := _int(details.get("thinking_tokens"))):
        out["completion_tokens_details"] = {"reasoning_tokens": thinking}
    return out


def merge_usage(into: dict[str, Any], new: Mapping[str, Any] | None) -> None:
    """Key by key, later non-null values winning.

    Anthropic's ``message_start`` carries the input counts and ``message_delta``
    a cumulative output count, so the last frame must not replace the first (the
    same rule ``protocols.MessagesReader`` applies on ``/v1/messages``).
    """
    for key, value in (new or {}).items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            into[key] = {**into[key], **{k: v for k, v in value.items() if v is not None}}
        else:
            into[key] = value


def thinking_record(model: str, blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """What to store so the next request can replay the reasoning, or ``None``
    when the reply has nothing to replay (no thinking, or no tool call)."""
    order: list[Any] = []
    tool_ids: list[str] = []
    has_thinking = False
    for block in blocks:
        kind = block.get("type")
        if kind in ("thinking", "redacted_thinking"):
            has_thinking = True
            order.append(block)
        elif kind == "text":
            if "text" not in order:
                order.append("text")
        elif kind == "tool_use":
            order.append(f"tool:{sanitize_tool_id(block.get('id'))}")
            tool_ids.append(sanitize_tool_id(block.get("id")))
    if not (has_thinking and tool_ids):
        return None
    return {"model": model, "order": order, "tool_ids": tool_ids}


def message_to_chat_completion(message: Mapping[str, Any], *, client_model: str) -> dict[str, Any]:
    """A non-streamed Messages response as a ``chat.completion``."""
    texts: list[str] = []
    reasoning: list[str] = []
    calls: list[dict[str, Any]] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            texts.append(_text(block.get("text")))
        elif kind == "thinking":
            if text := _text(block.get("thinking")):
                reasoning.append(text)
        elif kind == "tool_use":
            calls.append(
                {
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": orjson.dumps(block.get("input") or {}).decode(),
                    },
                }
            )
    out_message: dict[str, Any] = {"role": "assistant", "content": "".join(texts) or None}
    if reasoning:
        out_message["reasoning_content"] = "\n\n".join(reasoning)
    if calls:
        out_message["tool_calls"] = calls
    return {
        "id": message.get("id") or "chatcmpl-anthropic",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": client_model,
        "choices": [
            {
                "index": 0,
                "message": out_message,
                "finish_reason": finish_reason(message.get("stop_reason")),
            }
        ],
        "usage": usage_to_openai(message.get("usage")),
    }


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------

_ERROR_TYPES = {
    "invalid_request_error": "invalid_request_error",
    "authentication_error": "authentication_error",
    "permission_error": "permission_error",
    "not_found_error": "invalid_request_error",
    "request_too_large": "invalid_request_error",
    "rate_limit_error": "rate_limit_error",
    "billing_error": "billing_error",
    "api_error": "api_error",
    "timeout_error": "api_error",
    "overloaded_error": "server_error",
}


def error_to_openai(status: int, payload: Mapping[str, Any] | None) -> tuple[int, dict[str, Any]]:
    """``(status, OpenAI error body)`` for an Anthropic error.

    529 (overloaded) becomes 503, the status OpenAI clients retry; the gateway's
    failover also treats 5xx as a reason to try the next target. The message
    says so, since a JSON response here carries no ``retry-after``.
    """
    error = (payload or {}).get("error") if isinstance(payload, dict) else None
    error = error if isinstance(error, dict) else {}
    kind = str(error.get("type") or "api_error")
    message = _text(error.get("message")) or "The upstream provider returned an error."
    if status == 529 or kind == "overloaded_error":
        status = 503
        message = f"{message} The provider is overloaded; retry shortly."
    request_id = (payload or {}).get("request_id") if isinstance(payload, dict) else None
    if isinstance(request_id, str):
        message = f"{message} (upstream request id {request_id})"
    return status, error_payload(
        message, type_=_ERROR_TYPES.get(kind, "api_error"), code=kind or None
    )
