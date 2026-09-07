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
from collections.abc import Callable
from typing import Any, Protocol

from gateway.accounting.cost import TokenCounts
from gateway.models import ApiSurface

#: A text transform applied to assistant output on its way to the client.
Rewrite = Callable[[str], str]


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

    def stream_texts(self, payload: dict[str, Any]) -> list[tuple[int, str]]:
        """Incremental assistant text in this frame, needing buffered rewriting."""
        ...

    def set_stream_text(self, payload: dict[str, Any], index: int, text: str) -> None: ...

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

    def synthesise(self, template: dict[str, Any] | None, index: int, text: str) -> dict[str, Any]:
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

    def stream_texts(self, payload: dict[str, Any]) -> list[tuple[int, str]]:
        out: list[tuple[int, str]] = []
        for choice in payload.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            index = choice.get("index")
            index = index if isinstance(index, int) else 0
            delta = choice.get("delta")
            if isinstance(delta, dict) and isinstance(content := delta.get("content"), str):
                out.append((index, content))
        return out

    def set_stream_text(self, payload: dict[str, Any], index: int, text: str) -> None:
        for choice in payload.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            choice_index = choice.get("index")
            choice_index = choice_index if isinstance(choice_index, int) else 0
            if choice_index != index:
                continue
            if isinstance(delta := choice.get("delta"), dict):
                delta["content"] = text

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

    def is_terminal(self, payload: dict[str, Any]) -> bool:
        return any(
            isinstance(choice, dict) and choice.get("finish_reason")
            for choice in payload.get("choices") or []
        )

    def synthesise(self, template: dict[str, Any] | None, index: int, text: str) -> dict[str, Any]:
        # Copying the last real payload keeps `id`, `model` and `created`
        # consistent, which strict clients check.
        if template is not None:
            payload = copy.deepcopy(template)
            payload["choices"] = [{"index": index, "delta": {"content": text}}]
            payload.pop("usage", None)
            return payload
        return {
            "object": "chat.completion.chunk",
            "choices": [{"index": index, "delta": {"content": text}}],
        }


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

    def deltas(self, payload: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
        if payload.get("type") == "response.output_text.delta":
            text = payload.get("delta")
            return [(0, {"content": text})] if isinstance(text, str) else []
        # Non-streamed: take the assembled text the API already provides rather
        # than walking the output items ourselves.
        if isinstance(text := payload.get("output_text"), str) and text:
            return [(0, {"content": text})]
        return []

    def stream_texts(self, payload: dict[str, Any]) -> list[tuple[int, str]]:
        if payload.get("type") != "response.output_text.delta":
            return []
        text = payload.get("delta")
        return [(0, text)] if isinstance(text, str) else []

    def set_stream_text(self, payload: dict[str, Any], index: int, text: str) -> None:
        payload["delta"] = text

    def rewrite_whole(self, payload: dict[str, Any], rewrite: Rewrite) -> bool:
        """The completed text, wherever this surface repeats it.

        It repeats it in three places — the `.done` event, the assembled
        `output_text`, and the content blocks of the output items — and all
        three reach the client. Restoring the deltas and leaving these would
        show the caller placeholders in the very field most clients read.
        """
        changed = False

        # `response.output_text.done` carries the finished text for one item.
        if (
            payload.get("type") == "response.output_text.done"
            and isinstance(text := payload.get("text"), str)
            and (new := rewrite(text)) != text
        ):
            payload["text"] = new
            changed = True

        target = payload.get("response") if isinstance(payload.get("response"), dict) else payload
        if not isinstance(target, dict):
            return changed

        if isinstance(text := target.get("output_text"), str) and (new := rewrite(text)) != text:
            target["output_text"] = new
            changed = True

        for item in target.get("output") or []:
            if not isinstance(item, dict):
                continue
            for block in item.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if isinstance(text := block.get("text"), str) and (new := rewrite(text)) != text:
                    block["text"] = new
                    changed = True
        return changed

    def is_terminal(self, payload: dict[str, Any]) -> bool:
        kind = payload.get("type")
        # Flush at `.done` rather than at `response.completed`: the completed
        # event repeats the assembled text, so held-back deltas released after
        # it would duplicate content the client already has.
        return kind in ("response.output_text.done", "response.completed")

    def synthesise(self, template: dict[str, Any] | None, index: int, text: str) -> dict[str, Any]:
        base: dict[str, Any] = {
            "type": "response.output_text.delta",
            "delta": text,
            "output_index": 0,
            "content_index": 0,
        }
        if isinstance(template, dict):
            for key in ("item_id", "output_index", "content_index"):
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

    def stream_texts(self, payload: dict[str, Any]) -> list[tuple[int, str]]:
        if payload.get("type") != "content_block_delta":
            return []
        delta = payload.get("delta")
        # Only prose is rewritten. A tool call's `partial_json` fragment is not
        # text a placeholder can be substituted into without corrupting the
        # JSON it is half of, and `thinking` is not shown to the caller as
        # answer content.
        if isinstance(delta, dict) and isinstance(text := delta.get("text"), str):
            index = payload.get("index")
            return [(index if isinstance(index, int) else 0, text)]
        return []

    def set_stream_text(self, payload: dict[str, Any], index: int, text: str) -> None:
        delta = payload.get("delta")
        if isinstance(delta, dict):
            delta["text"] = text

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
                if not isinstance(block, dict):
                    continue
                if isinstance(text := block.get("text"), str) and (new := rewrite(text)) != text:
                    block["text"] = new
                    changed = True
        return changed

    def is_terminal(self, payload: dict[str, Any]) -> bool:
        return payload.get("type") in ("content_block_stop", "message_delta", "message_stop")

    def synthesise(self, template: dict[str, Any] | None, index: int, text: str) -> dict[str, Any]:
        return {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text},
        }


class ImagesReader:
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

    def stream_texts(self, payload: dict[str, Any]) -> list[tuple[int, str]]:
        return []

    def set_stream_text(self, payload: dict[str, Any], index: int, text: str) -> None:
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

    def synthesise(self, template: dict[str, Any] | None, index: int, text: str) -> dict[str, Any]:
        raise NotImplementedError("the image route does not stream")


class OcrReader:
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

    def deltas(self, payload: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
        """Nothing: pages are counted, never inferred from the text."""
        return []

    def stream_texts(self, payload: dict[str, Any]) -> list[tuple[int, str]]:
        return []

    def set_stream_text(self, payload: dict[str, Any], index: int, text: str) -> None:
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

    def synthesise(self, template: dict[str, Any] | None, index: int, text: str) -> dict[str, Any]:
        raise NotImplementedError("the ocr route does not stream")


_READERS: dict[ApiSurface, SurfaceProtocol] = {
    ApiSurface.CHAT_COMPLETIONS: ChatCompletionsReader(),
    ApiSurface.EMBEDDINGS: ChatCompletionsReader(),
    ApiSurface.RESPONSES: ResponsesReader(),
    ApiSurface.MESSAGES: MessagesReader(),
    ApiSurface.IMAGES: ImagesReader(),
    ApiSurface.OCR: OcrReader(),
}


def reader_for(surface: ApiSurface) -> SurfaceProtocol:
    return _READERS[surface]
