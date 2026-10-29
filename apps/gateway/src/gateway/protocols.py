"""What each API surface's response frames look like.

One class per surface, and everything that has to know a protocol's shape asks
it here: accounting (where is usage, which model served this, what was
produced) and redaction (which fields hold assistant text, and how to inject
more of it into a stream). Two tables indexed by surface would drift; one
cannot.

Nothing in here talks to a database, prices anything, or holds request state.
It is the shape of the wire, and only that.

The two differences that are not trivia, and that a single tolerant parser
would get wrong:

**Anthropic splits usage across two frames.** ``message_start`` carries the
input tokens and ``message_delta`` carries a *cumulative* output count; the
final delta repeats the input only when server tools ran. So the frames are
merged key by key rather than the last one winning, which is the correct rule
for OpenAI's single terminal usage frame and would throw away Anthropic's input
count.

**Anthropic's ``input_tokens`` excludes the cached tokens.** Handled in
``TokenCounts.from_anthropic_usage``; mentioned here because it is why each
surface names its reader explicitly instead of sniffing the keys.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any, ClassVar, Protocol

from gateway.accounting.cost import TokenCounts
from gateway.models import ApiSurface

#: A text transform applied to assistant output on its way to the client.
Rewrite = Callable[[str], str]


#: ``(choice index, offset) -> offset``: where a position in the text a provider
#: generated ended up in the text the caller is given. Identity when nothing was
#: restored.
#:
#: The choice index is not decoration. Restoration uses one placeholder map for
#: the whole request, but the *positions* it edits differ per choice, because
#: each choice is different text — so a request asking for ``n: 2`` needs two
#: maps and the caller has to say which one it is asking about.
Shift = Callable[[int, int], int]

#: Where one run of incremental text lives in a stream: ``(choice or block
#: index, field)``. Text is buffered per slot, because the answer, the model's
#: reasoning and each tool call's arguments are separate strings that arrive
#: interleaved, and a placeholder begun in one must never be completed by the
#: next.
Slot = tuple[int, str]

#: Slots whose field starts with this are a tool call's JSON arguments, which
#: are restored differently (the original is written JSON-escaped).
ARGUMENTS = "args"


def is_arguments(slot: Slot) -> bool:
    return slot[1].startswith(ARGUMENTS)


def is_answer(slot: Slot) -> bool:
    """The answer text itself, as opposed to reasoning or tool arguments.

    Citations point into this one, so it is the only text kept for them.
    """
    return slot[1] == "content" or slot[1].startswith("text")


#: The fields OpenAI-compatible providers put a model's reasoning in. They all
#: exist in the wild: vLLM and DeepSeek say ``reasoning_content``, OpenRouter
#: and newer vLLM say ``reasoning``, and some say ``reasoning_text``.
REASONING_FIELDS = ("reasoning_content", "reasoning", "reasoning_text")


def shift_annotations(annotations: Any, shift: Shift, choice: int) -> bool:
    """Move the offsets in one OpenAI-shaped ``annotations`` array.

    A ``url_citation`` says which characters of the assistant's message a source
    supports, as ``start_index`` and ``end_index``. Those indices were computed
    against the text the provider generated, which contained our placeholders.
    Restoring a real value of a different length moves every character after it,
    so the indices move with it or they point at the wrong words (ADR 0059).

    Shared by both OpenAI-shaped surfaces because the annotation object is the
    same on each; only where it *hangs* differs, and that is what the readers
    below know.
    """
    if not isinstance(annotations, list):
        return False
    moved = False
    for annotation in annotations:
        if not isinstance(annotation, dict):
            continue
        # The offsets live one level down on chat completions and at the top
        # level on the Responses API. Both spellings, no guessing about which.
        for holder in (annotation.get("url_citation"), annotation):
            if not isinstance(holder, dict):
                continue
            for key in ("start_index", "end_index"):
                index = holder.get(key)
                if isinstance(index, int) and (new := shift(choice, index)) != index:
                    holder[key] = new
                    moved = True
            if holder is not annotation:
                # Found the nested form; do not also treat the wrapper as one.
                break
    return moved


class SurfaceProtocol(Protocol):
    """How to read one API surface's response frames."""

    #: True when usage arrives in pieces and must be merged key by key.
    accumulates_usage: bool

    def frame(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """The object inside this frame that carries usage, model and provider.

        None for a frame that carries none of them — a content delta, a
        keepalive, a lifecycle marker.
        """
        ...

    def counts(self, usage: dict[str, Any] | None) -> TokenCounts: ...

    def deltas(self, payload: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
        """``(choice index, delta)`` pairs, for reassembling the output text."""
        ...

    # -- redaction of the response ----------------------------------------
    #
    # Two kinds of text, because they need different handling. An *incremental*
    # delta may split an entity across frames, so it goes through the buffering
    # rewriter. A *whole* field arrives complete and is rewritten in place —
    # buffering it would hold back text that is already finished.

    def stream_texts(self, payload: dict[str, Any]) -> list[tuple[Slot, str]]:
        """Incremental text in this frame, needing buffered rewriting.

        The answer, reasoning and tool-call arguments, each in its own slot.
        """
        ...

    def set_stream_text(self, payload: dict[str, Any], slot: Slot, text: str) -> None: ...

    def rewrite_reasoning(self, payload: dict[str, Any], rewrite: Rewrite) -> bool:
        """Rewrite every complete reasoning field in place; True if any changed."""
        ...

    def rewrite_arguments(self, payload: dict[str, Any], rewrite: Rewrite) -> bool:
        """Rewrite every complete tool-call argument string in place.

        *rewrite* is handed JSON text and must return JSON text.
        """
        ...

    def shift_citations(self, payload: dict[str, Any], shift: Shift) -> bool:
        """Move any character offsets this frame holds into the assistant's text.

        Called whenever restoration changed the text's length. Surfaces that
        return no such offsets do nothing — Anthropic's web-search citations
        carry an ``encrypted_index`` and a copy of the ``cited_text`` rather
        than positions in our text, so there is nothing there to move.
        """
        ...

    def rewrite_whole(self, payload: dict[str, Any], rewrite: Rewrite) -> bool:
        """Rewrite every complete assistant-text field in place.

        Returns whether anything changed. Complete fields matter as much as the
        deltas: a surface that repeats the finished text in a terminal frame
        would otherwise hand the client the placeholder version of an answer it
        had already received restored.
        """
        ...

    def is_terminal(self, payload: dict[str, Any]) -> bool:
        """True for a frame after which no more text will arrive for a choice.

        The buffered rewriter flushes here, so that held-back text cannot land
        after the client believes the message is finished.
        """
        ...

    def synthesise(self, template: dict[str, Any] | None, slot: Slot, text: str) -> dict[str, Any]:
        """A frame carrying *text*, shaped like the ones the provider sends."""
        ...


class ChatCompletionsReader:
    """OpenAI Chat Completions, and the embeddings route that borrows its shape."""

    accumulates_usage = False

    def frame(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        return payload

    def counts(self, usage: dict[str, Any] | None) -> TokenCounts:
        return TokenCounts.from_usage(usage)

    def deltas(self, payload: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
        out: list[tuple[int, dict[str, Any]]] = []
        for choice in payload.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            index = choice.get("index")
            index = index if isinstance(index, int) else 0
            for key in ("delta", "message"):
                if isinstance(part := choice.get(key), dict):
                    out.append((index, part))
            if reason := choice.get("finish_reason"):
                out.append((index, {"__finish_reason__": str(reason)}))
        return out

    def stream_texts(self, payload: dict[str, Any]) -> list[tuple[Slot, str]]:
        out: list[tuple[Slot, str]] = []
        for choice in payload.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            index = choice.get("index")
            index = index if isinstance(index, int) else 0
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            if isinstance(content := delta.get("content"), str):
                out.append(((index, "content"), content))
            for field in REASONING_FIELDS:
                if isinstance(thought := delta.get(field), str) and thought:
                    out.append(((index, field), thought))
            for position, call in enumerate(delta.get("tool_calls") or []):
                function = call.get("function") if isinstance(call, dict) else None
                if not isinstance(function, dict):
                    continue
                if isinstance(arguments := function.get("arguments"), str) and arguments:
                    out.append(((index, f"{ARGUMENTS}:{_call_index(call, position)}"), arguments))
        return out

    def set_stream_text(self, payload: dict[str, Any], slot: Slot, text: str) -> None:
        index, field = slot
        for choice in payload.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            choice_index = choice.get("index")
            choice_index = choice_index if isinstance(choice_index, int) else 0
            if choice_index != index:
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            if not field.startswith(ARGUMENTS):
                delta[field] = text
                continue
            for position, call in enumerate(delta.get("tool_calls") or []):
                function = call.get("function") if isinstance(call, dict) else None
                if (
                    isinstance(function, dict)
                    and f"{ARGUMENTS}:{_call_index(call, position)}" == field
                ):
                    function["arguments"] = text

    def shift_citations(self, payload: dict[str, Any], shift: Shift) -> bool:
        """``annotations`` hangs off the message, and off the delta when streamed.

        Both are handled here because a streamed response carries them in
        ``choices[].delta.annotations`` — with offsets into the *accumulated*
        message, not into the delta they arrive on, which is why the streaming
        rewriter has to know the whole answer to shift them (ADR 0059).
        """
        moved = False
        for position, choice in enumerate(payload.get("choices") or []):
            if not isinstance(choice, dict):
                continue
            # The declared index, falling back to the position: a chunk names
            # its choice, and a non-streamed body lists them in order.
            declared = choice.get("index")
            which = declared if isinstance(declared, int) else position
            for holder in ("message", "delta"):
                part = choice.get(holder)
                if isinstance(part, dict) and shift_annotations(
                    part.get("annotations"), shift, which
                ):
                    moved = True
        return moved

    def rewrite_whole(self, payload: dict[str, Any], rewrite: Rewrite) -> bool:
        """Non-streamed bodies only; a chunk has deltas, never a whole message."""
        changed = False
        for choice in payload.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if (
                isinstance(message, dict)
                and isinstance(text := message.get("content"), str)
                and (new := rewrite(text)) != text
            ):
                message["content"] = new
                changed = True
        return changed

    def rewrite_reasoning(self, payload: dict[str, Any], rewrite: Rewrite) -> bool:
        changed = False
        for choice in payload.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if not isinstance(message, dict):
                continue
            for field in REASONING_FIELDS:
                if isinstance(text := message.get(field), str) and (new := rewrite(text)) != text:
                    message[field] = new
                    changed = True
        return changed

    def rewrite_arguments(self, payload: dict[str, Any], rewrite: Rewrite) -> bool:
        changed = False
        for choice in payload.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            for call in (message.get("tool_calls") if isinstance(message, dict) else None) or []:
                function = call.get("function") if isinstance(call, dict) else None
                if (
                    isinstance(function, dict)
                    and isinstance(text := function.get("arguments"), str)
                    and (new := rewrite(text)) != text
                ):
                    function["arguments"] = new
                    changed = True
        return changed

    def is_terminal(self, payload: dict[str, Any]) -> bool:
        return any(
            isinstance(choice, dict) and choice.get("finish_reason")
            for choice in payload.get("choices") or []
        )

    def synthesise(self, template: dict[str, Any] | None, slot: Slot, text: str) -> dict[str, Any]:
        index, field = slot
        if field.startswith(ARGUMENTS):
            _, _, number = field.partition(":")
            delta: dict[str, Any] = {
                "tool_calls": [{"index": int(number or 0), "function": {"arguments": text}}]
            }
        else:
            delta = {field: text}
        # Copying the last real payload keeps `id`, `model` and `created`
        # consistent, which strict clients check.
        if template is not None:
            payload = copy.deepcopy(template)
            payload["choices"] = [{"index": index, "delta": delta}]
            payload.pop("usage", None)
            return payload
        return {
            "object": "chat.completion.chunk",
            "choices": [{"index": index, "delta": delta}],
        }


def _call_index(call: dict[str, Any], position: int) -> int:
    """Which tool call a streamed fragment belongs to.

    The declared ``index``, falling back to the fragment's position: a few
    providers omit it on single-call responses.
    """
    declared = call.get("index")
    return declared if isinstance(declared, int) else position


class ResponsesReader:
    """OpenAI Responses.

    Streaming wraps the whole response object in a lifecycle event, so the
    numbers live one level down — but only on the terminal events. Non-streamed
    bodies *are* the response object, which is why both are accepted.
    """

    accumulates_usage = False

    #: The events that carry a complete response object. ``incomplete`` and
    #: ``failed`` are here because a truncated or failed generation still
    #: consumed tokens, and not reading their usage would bill zero for work
    #: the provider charged us for.
    TERMINAL = frozenset({"response.completed", "response.incomplete", "response.failed"})

    def frame(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        kind = payload.get("type")
        if isinstance(kind, str) and kind.startswith("response."):
            if kind not in self.TERMINAL:
                return None
            nested = payload.get("response")
            return nested if isinstance(nested, dict) else None
        # A non-streamed body: the response object itself.
        return payload if payload.get("object") == "response" or "output" in payload else None

    def counts(self, usage: dict[str, Any] | None) -> TokenCounts:
        return TokenCounts.from_responses_usage(usage)

    def shift_citations(self, payload: dict[str, Any], shift: Shift) -> bool:
        """Offsets hang off each content part of each message output item.

        Three shapes, because this surface has three: the assembled body, the
        ``response.completed`` envelope that repeats it, and the
        ``response.output_text.annotation.added`` event that carries one
        annotation on its own while streaming.
        """
        moved = False
        # A single annotation, arriving on its own event while streaming. Wrapped
        # in a list because the helper reads arrays, and this surface has no
        # choices, so the map is always the only one there is.
        if shift_annotations([payload.get("annotation")], shift, 0):
            moved = True
        for envelope in (payload, payload.get("response")):
            if not isinstance(envelope, dict):
                continue
            for item in envelope.get("output") or []:
                if not isinstance(item, dict):
                    continue
                for part in item.get("content") or []:
                    if isinstance(part, dict) and shift_annotations(
                        part.get("annotations"), shift, 0
                    ):
                        moved = True
        return moved

    def deltas(self, payload: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
        if payload.get("type") == "response.output_text.delta":
            text = payload.get("delta")
            return [(0, {"content": text})] if isinstance(text, str) else []
        # Non-streamed: take the assembled text the API already provides rather
        # than walking the output items ourselves.
        if isinstance(text := payload.get("output_text"), str) and text:
            return [(0, {"content": text})]
        return []

    #: Streamed delta events, and the slot each one's text is buffered in.
    _DELTAS: ClassVar[dict[str, str]] = {
        "response.output_text.delta": "text",
        "response.reasoning_summary_text.delta": "reasoning_summary",
        "response.reasoning_text.delta": "reasoning",
        "response.function_call_arguments.delta": ARGUMENTS,
    }
    _DELTA_TYPE: ClassVar[dict[str, str]] = {
        "text": "response.output_text.delta",
        "reasoning_summary": "response.reasoning_summary_text.delta",
        "reasoning": "response.reasoning_text.delta",
        ARGUMENTS: "response.function_call_arguments.delta",
    }

    def stream_texts(self, payload: dict[str, Any]) -> list[tuple[Slot, str]]:
        field = self._DELTAS.get(str(payload.get("type")))
        text = payload.get("delta")
        if field is None or not isinstance(text, str):
            return []
        return [((0, field), text)]

    def set_stream_text(self, payload: dict[str, Any], slot: Slot, text: str) -> None:
        payload["delta"] = text

    @staticmethod
    def _items(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Every output item this event or body carries."""
        found: list[Any] = [payload.get("item")]
        for envelope in (payload, payload.get("response")):
            if isinstance(envelope, dict):
                found.extend(envelope.get("output") or [])
        return [item for item in found if isinstance(item, dict)]

    @staticmethod
    def _rewrite_key(holder: dict[str, Any], key: str, rewrite: Rewrite) -> bool:
        if isinstance(text := holder.get(key), str) and (new := rewrite(text)) != text:
            holder[key] = new
            return True
        return False

    def rewrite_whole(self, payload: dict[str, Any], rewrite: Rewrite) -> bool:
        """The completed text, wherever this surface repeats it.

        It repeats it in several places — the `.done` event, the content part,
        the output item, the assembled `output_text`, and the content blocks of
        the output items in the terminal envelope — and all of them reach the
        client. Restoring the deltas and leaving these would show the caller
        placeholders in the very field most clients read.
        """
        changed = False
        kind = payload.get("type")
        if kind == "response.output_text.done":
            changed |= self._rewrite_key(payload, "text", rewrite)
        part = payload.get("part")
        if kind == "response.content_part.done" and isinstance(part, dict):
            changed |= self._rewrite_key(part, "text", rewrite)

        target = payload.get("response") if isinstance(payload.get("response"), dict) else payload
        if isinstance(target, dict):
            changed |= self._rewrite_key(target, "output_text", rewrite)

        for item in self._items(payload):
            if item.get("type") in ("reasoning", "function_call"):
                continue
            for block in item.get("content") or []:
                if isinstance(block, dict):
                    changed |= self._rewrite_key(block, "text", rewrite)
        return changed

    def rewrite_reasoning(self, payload: dict[str, Any], rewrite: Rewrite) -> bool:
        """Reasoning summaries and reasoning text. Never ``encrypted_content``."""
        changed = False
        kind = payload.get("type")
        if kind in ("response.reasoning_summary_text.done", "response.reasoning_text.done"):
            changed |= self._rewrite_key(payload, "text", rewrite)
        part = payload.get("part")
        if kind == "response.reasoning_summary_part.done" and isinstance(part, dict):
            changed |= self._rewrite_key(part, "text", rewrite)
        for item in self._items(payload):
            if item.get("type") != "reasoning":
                continue
            for field in ("summary", "content"):
                for block in item.get(field) or []:
                    if isinstance(block, dict):
                        changed |= self._rewrite_key(block, "text", rewrite)
        return changed

    def rewrite_arguments(self, payload: dict[str, Any], rewrite: Rewrite) -> bool:
        changed = False
        if payload.get("type") == "response.function_call_arguments.done":
            changed |= self._rewrite_key(payload, "arguments", rewrite)
        for item in self._items(payload):
            if item.get("type") == "function_call":
                changed |= self._rewrite_key(item, "arguments", rewrite)
        return changed

    #: Events after which no more text arrives for what they close.
    _CLOSERS: ClassVar[frozenset[str]] = frozenset(
        {
            "response.output_text.done",
            "response.reasoning_summary_text.done",
            "response.reasoning_text.done",
            "response.function_call_arguments.done",
            "response.content_part.done",
            "response.output_item.done",
            "response.completed",
        }
    )

    def is_terminal(self, payload: dict[str, Any]) -> bool:
        # Flush at `.done` rather than at `response.completed`: the completed
        # event repeats the assembled text, so held-back deltas released after
        # it would duplicate content the client already has.
        return payload.get("type") in self._CLOSERS

    def synthesise(self, template: dict[str, Any] | None, slot: Slot, text: str) -> dict[str, Any]:
        base: dict[str, Any] = {
            "type": self._DELTA_TYPE[slot[1]],
            "delta": text,
            "output_index": 0,
            "content_index": 0,
        }
        if isinstance(template, dict):
            for key in ("item_id", "output_index", "content_index", "summary_index"):
                if key in template:
                    base[key] = template[key]
        return base


class MessagesReader:
    """Anthropic Messages.

    ``message_start`` carries the input tokens and the served model;
    ``message_delta`` carries the cumulative output count and the stop reason.
    Both must be read, which is what ``accumulates_usage`` buys.
    """

    accumulates_usage = True

    def frame(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        kind = payload.get("type")
        if kind == "message_start":
            nested = payload.get("message")
            return nested if isinstance(nested, dict) else None
        if kind == "message_delta":
            # Usage sits at the top level here, beside `delta`, not inside it.
            return payload
        if kind == "message":
            # A non-streamed body.
            return payload
        return None

    def counts(self, usage: dict[str, Any] | None) -> TokenCounts:
        return TokenCounts.from_anthropic_usage(usage)

    def shift_citations(self, payload: dict[str, Any], shift: Shift) -> bool:
        """Nothing. This surface's web-search citations carry an
        ``encrypted_index`` and a copy of the ``cited_text``, not offsets
        into the text we rewrite, so there is nothing to move. Its
        *document* citations do use character indices — but into the
        document the caller supplied, not into the answer, so they are
        equally untouched by restoring the answer."""
        return False

    def deltas(self, payload: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
        kind = payload.get("type")
        if kind == "content_block_delta":
            delta = payload.get("delta")
            if isinstance(delta, dict):
                # `text_delta` for prose, `input_json_delta` for a tool call's
                # arguments, `thinking_delta` for reasoning. All of them are
                # generated output and all of them are billed.
                for key in ("text", "partial_json", "thinking"):
                    if isinstance(piece := delta.get(key), str) and piece:
                        return [(0, {"content": piece})]
            return []
        if kind == "message_delta":
            delta = payload.get("delta")
            if isinstance(delta, dict) and (reason := delta.get("stop_reason")):
                return [(0, {"__finish_reason__": str(reason)})]
            return []
        if kind == "message":
            pieces = [
                block.get("text")
                for block in payload.get("content") or []
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            ]
            out: list[tuple[int, dict[str, Any]]] = [
                (0, {"content": text}) for text in pieces if text
            ]
            if reason := payload.get("stop_reason"):
                out.append((0, {"__finish_reason__": str(reason)}))
            return out
        return []

    def stream_texts(self, payload: dict[str, Any]) -> list[tuple[Slot, str]]:
        if payload.get("type") != "content_block_delta":
            return []
        delta = payload.get("delta")
        if not isinstance(delta, dict):
            return []
        index = payload.get("index")
        index = index if isinstance(index, int) else 0
        # Prose, and a tool call's `partial_json` fragments. The fragments are
        # half of a JSON document, so they are restored with the original
        # JSON-escaped. `thinking` is left alone: a thinking block is signed
        # over its text, and the caller replays it, so restoring it would make
        # the provider reject the next turn.
        if isinstance(text := delta.get("text"), str):
            return [((index, "text"), text)]
        if isinstance(fragment := delta.get("partial_json"), str) and fragment:
            return [((index, ARGUMENTS), fragment)]
        return []

    def set_stream_text(self, payload: dict[str, Any], slot: Slot, text: str) -> None:
        delta = payload.get("delta")
        if isinstance(delta, dict):
            delta["partial_json" if slot[1] == ARGUMENTS else "text"] = text

    def rewrite_reasoning(self, payload: dict[str, Any], rewrite: Rewrite) -> bool:
        """Nothing: a thinking block is signed over its own text."""
        return False

    def rewrite_arguments(self, payload: dict[str, Any], rewrite: Rewrite) -> bool:
        """``tool_use`` blocks carry their arguments as an object, not as text.

        Round-tripped through JSON so the same JSON-aware *rewrite* serves every
        surface; a result that no longer parses is discarded rather than sent.
        """
        blocks: list[Any] = []
        if payload.get("type") == "content_block_start":
            blocks.append(payload.get("content_block"))
        elif payload.get("type") == "message":
            blocks.extend(payload.get("content") or [])
        changed = False
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            arguments = block.get("input")
            if not arguments:
                continue
            text = json.dumps(arguments, ensure_ascii=False)
            if (new := rewrite(text)) == text:
                continue
            try:
                block["input"] = json.loads(new)
            except ValueError:
                continue
            changed = True
        return changed

    def rewrite_whole(self, payload: dict[str, Any], rewrite: Rewrite) -> bool:
        changed = False
        kind = payload.get("type")
        if kind == "content_block_start":
            block = payload.get("content_block")
            if (
                isinstance(block, dict)
                and isinstance(text := block.get("text"), str)
                and (new := rewrite(text)) != text
            ):
                block["text"] = new
                changed = True
            return changed
        if kind == "message":
            for block in payload.get("content") or []:
                if not isinstance(block, dict) or block.get("type") not in (None, "text"):
                    continue
                if isinstance(text := block.get("text"), str) and (new := rewrite(text)) != text:
                    block["text"] = new
                    changed = True
        return changed

    def is_terminal(self, payload: dict[str, Any]) -> bool:
        return payload.get("type") in ("content_block_stop", "message_delta", "message_stop")

    def synthesise(self, template: dict[str, Any] | None, slot: Slot, text: str) -> dict[str, Any]:
        index, field = slot
        delta = (
            {"type": "input_json_delta", "partial_json": text}
            if field == ARGUMENTS
            else {"type": "text_delta", "text": text}
        )
        return {"type": "content_block_delta", "index": index, "delta": delta}


class _NoStreamedExtras:
    """The readers for surfaces that carry no reasoning and no tool calls."""

    def rewrite_reasoning(self, payload: dict[str, Any], rewrite: Rewrite) -> bool:
        return False

    def rewrite_arguments(self, payload: dict[str, Any], rewrite: Rewrite) -> bool:
        return False


class ImagesReader(_NoStreamedExtras):
    """Image generation.

    Never streamed, and the usage object is optional — a per-image-priced model
    reports no tokens at all. The image count comes from the data array rather
    than from usage, because that is the only place it is always present.

    Response text is still rewritten (`rewrite_whole` restores the revised
    prompt), it is just never *counted*.
    """

    accumulates_usage = False

    def frame(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        return payload

    def counts(self, usage: dict[str, Any] | None) -> TokenCounts:
        return TokenCounts.from_responses_usage(usage)

    def shift_citations(self, payload: dict[str, Any], shift: Shift) -> bool:
        """Nothing. An image response has no cited text."""
        return False

    def deltas(self, payload: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
        """Nothing. An image request generates no billable text.

        The `revised_prompt` is tempting — it is the only text in the response
        — but it is the provider annotating our own prompt back at us, not
        output it charged for. Feeding it through here would estimate it as
        completion tokens and label an otherwise exactly-known bill
        "estimated", which is precisely backwards: the picture count is the
        exact quantity and the token count is the meaningless one.
        """
        return []

    def stream_texts(self, payload: dict[str, Any]) -> list[tuple[Slot, str]]:
        return []

    def set_stream_text(self, payload: dict[str, Any], slot: Slot, text: str) -> None:
        return None

    def rewrite_whole(self, payload: dict[str, Any], rewrite: Rewrite) -> bool:
        """The revised prompt, which is the provider echoing our text back.

        Restored because it is derived from the prompt we redacted: leaving it
        would show the caller a placeholder where their own word had been.
        """
        changed = False
        for entry in payload.get("data") or []:
            if not isinstance(entry, dict):
                continue
            if (
                isinstance(text := entry.get("revised_prompt"), str)
                and (new := rewrite(text)) != text
            ):
                entry["revised_prompt"] = new
                changed = True
        return changed

    def is_terminal(self, payload: dict[str, Any]) -> bool:
        return True

    def synthesise(self, template: dict[str, Any] | None, slot: Slot, text: str) -> dict[str, Any]:
        raise NotImplementedError("the image route does not stream")


class OcrReader(_NoStreamedExtras):
    """Document extraction, `POST /v1/ocr`.

    Three things sit differently here from every other surface, and all three
    are why this file exists rather than each route reading its own fields:

    **Usage is under `usage_info`, not `usage`.** Cortecs and Mistral both spell
    it that way, and it carries `pages_processed` rather than any token count.
    `frame` aliases it, which keeps the recorder's "read `usage` off the frame"
    true for one more surface instead of teaching the recorder a sixth special
    case.

    **The billable quantity is exact and is not tokens.** A page count is
    measured, not estimated, so `deltas` returns nothing — the same argument the
    image reader makes. Feeding the extracted text through as completion tokens
    would label an exactly-known bill "estimated", which is backwards.

    **The text in the response is not the model's output in the usual sense.**
    It is the document, transcribed. That makes `pages[].markdown` the most
    sensitive field this gateway handles — a scanned identity card becomes
    searchable text at exactly this point — which is why it is walked here and
    why the response-side redaction has somewhere to hook.
    """

    accumulates_usage = False

    def frame(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        usage = payload.get("usage_info")
        if not isinstance(usage, dict):
            return payload
        # A shallow copy with the alias added: the caller's payload is what goes
        # back to the client, and adding a key to it would invent a field the
        # OCR API does not have.
        aliased = dict(payload)
        aliased["usage"] = usage
        return aliased

    def counts(self, usage: dict[str, Any] | None) -> TokenCounts:
        return TokenCounts.from_ocr_usage(usage)

    def shift_citations(self, payload: dict[str, Any], shift: Shift) -> bool:
        """Nothing. This surface redacts its pages rather than
        restoring them (ADR 0055), and a page carries no citations."""
        return False

    def deltas(self, payload: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
        """Nothing: pages are counted, never inferred from the text."""
        return []

    def stream_texts(self, payload: dict[str, Any]) -> list[tuple[Slot, str]]:
        return []

    def set_stream_text(self, payload: dict[str, Any], slot: Slot, text: str) -> None:
        return None

    def rewrite_whole(self, payload: dict[str, Any], rewrite: Rewrite) -> bool:
        """Every page's markdown, in place.

        Used by restoration like every other surface — a document whose text was
        redacted on the way out would come back carrying placeholders — and it is
        the hook the response-side redaction needs, because on this surface the
        text that matters is what came *back*.
        """
        changed = False
        for page in payload.get("pages") or []:
            if not isinstance(page, dict):
                continue
            if isinstance(text := page.get("markdown"), str) and (new := rewrite(text)) != text:
                page["markdown"] = new
                changed = True
        return changed

    def is_terminal(self, payload: dict[str, Any]) -> bool:
        return True

    def synthesise(self, template: dict[str, Any] | None, slot: Slot, text: str) -> dict[str, Any]:
        raise NotImplementedError("the ocr route does not stream")


class SearchReader(_NoStreamedExtras):
    """``POST /v1/search``: the one surface with nothing to read.

    Every other reader here exists to find a number in a response. This one
    finds none, and that is the surface rather than a gap in it: a search is
    metered as a **count of requests we made**, held on the recorder by
    ``observe_own_search`` and never derived from what came back
    (``LimitMetric.OWN_SEARCH_REQUESTS``). There is no usage object, no token
    count to estimate, and no rate for one to be multiplied by.

    So every method answers nothing, deliberately:

    * ``counts`` returns an empty ``TokenCounts``. Counting the returned
      snippets as completion tokens would put a token figure on a row whose
      model has no token price, and make an exactly-known quantity — one
      request — sit beside an invented one.
    * ``deltas`` returns nothing, so ``usage_source`` stays ``unavailable``
      rather than ``estimated``: nothing was estimated, because nothing is
      counted in tokens here.
    * ``rewrite_whole`` returns ``False``. Placeholders are **not** restored
      into results: the vendor never saw our real text, so it cannot have
      returned a placeholder, and running a restore over untrusted web text to
      find nothing would be theatre. See ``routers/search.py``.
    """

    accumulates_usage = False

    def frame(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        return payload

    def counts(self, usage: dict[str, Any] | None) -> TokenCounts:
        return TokenCounts()

    def shift_citations(self, payload: dict[str, Any], shift: Shift) -> bool:
        return False

    def deltas(self, payload: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
        return []

    def stream_texts(self, payload: dict[str, Any]) -> list[tuple[Slot, str]]:
        return []

    def set_stream_text(self, payload: dict[str, Any], slot: Slot, text: str) -> None:
        return None

    def rewrite_whole(self, payload: dict[str, Any], rewrite: Rewrite) -> bool:
        return False

    def is_terminal(self, payload: dict[str, Any]) -> bool:
        return True

    def synthesise(self, template: dict[str, Any] | None, slot: Slot, text: str) -> dict[str, Any]:
        raise NotImplementedError("the search route does not stream")


class DecisionsReader(_NoStreamedExtras):
    """``POST /v1/decisions`` and ``POST /v1/systemone`` (ADR 0100).

    Nothing here is read except the usage object and the model, and that is
    deliberate: an answer is probabilities, choices and scores, not text. There
    is no assistant text to restore placeholders into, no citation offsets to
    move, nothing to stream, and the one free-text field an answer may carry (a
    refusal's reason) passes through as the provider wrote it.

    Usage is the Responses spelling, ``input_tokens`` and ``output_tokens``
    (checked live against Cortecs, 2026-10-22), and the counts are exact: the
    provider tokenised the input, so ``deltas`` returns nothing and a request
    that reports no usage is estimated from the prompt alone.
    """

    accumulates_usage = False

    def frame(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        return payload

    def counts(self, usage: dict[str, Any] | None) -> TokenCounts:
        return TokenCounts.from_responses_usage(usage)

    def shift_citations(self, payload: dict[str, Any], shift: Shift) -> bool:
        return False

    def deltas(self, payload: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
        return []

    def stream_texts(self, payload: dict[str, Any]) -> list[tuple[Slot, str]]:
        return []

    def set_stream_text(self, payload: dict[str, Any], slot: Slot, text: str) -> None:
        return None

    def rewrite_whole(self, payload: dict[str, Any], rewrite: Rewrite) -> bool:
        return False

    def is_terminal(self, payload: dict[str, Any]) -> bool:
        return True

    def synthesise(self, template: dict[str, Any] | None, slot: Slot, text: str) -> dict[str, Any]:
        raise NotImplementedError("the decisions routes do not stream")


_READERS: dict[ApiSurface, SurfaceProtocol] = {
    ApiSurface.CHAT_COMPLETIONS: ChatCompletionsReader(),
    ApiSurface.EMBEDDINGS: ChatCompletionsReader(),
    ApiSurface.RESPONSES: ResponsesReader(),
    ApiSurface.MESSAGES: MessagesReader(),
    ApiSurface.IMAGES: ImagesReader(),
    ApiSurface.OCR: OcrReader(),
    ApiSurface.SEARCH: SearchReader(),
    ApiSurface.DECISIONS: DecisionsReader(),
}


def reader_for(surface: ApiSurface) -> SurfaceProtocol:
    return _READERS[surface]
