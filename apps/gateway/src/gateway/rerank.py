"""``POST /v1/rerank``: the request and reply, normalised.

Reranking has a de-facto shape (Cohere, Jina), and the two providers measured
live (2026-11-06) follow it with differences a caller must not have to know:

* **Documents.** Melious takes strings and ``{"text": ...}`` objects; Cortecs
  (vLLM) takes strings only and answers 400 to an object. The gateway accepts
  both from the caller and always sends strings upstream.
* **``return_documents``.** Melious honours it (default true); Cortecs ignores
  it and always returns the documents. The gateway reads neither: it builds the
  reply's ``document`` from the caller's own documents, by index, only when
  asked (default false, as Cohere's v2).
* **Usage.** Cortecs reports Chat Completions style ``usage`` (``prompt_tokens``,
  plus its own ``cost``); Melious reports ``usage: null`` and puts the count in
  ``meta.billed_units.total_tokens`` / ``meta.tokens.input_tokens``. Both are
  read here, and when neither is present the caller's route falls back to an
  estimate, exactly as for embeddings.
* ``top_n`` below 1 is a 400 upstream (Cortecs); the gateway checks it first.
"""

from __future__ import annotations

from typing import Any

from gateway.errors import BadRequestError


def _refuse(message: str) -> BadRequestError:
    return BadRequestError(message, code="invalid_rerank_request")


def document_texts(documents: list[Any]) -> list[str]:
    """The documents as plain strings; a 400 naming the offending index."""
    texts: list[str] = []
    for index, document in enumerate(documents):
        if isinstance(document, dict):
            document = document.get("text")
            if not isinstance(document, str):
                raise _refuse(
                    f"documents[{index}] must be a string or an object with a string 'text'."
                )
        if not isinstance(document, str) or not document.strip():
            raise _refuse(f"documents[{index}] must be a non-empty string.")
        texts.append(document)
    return texts


def validate(
    *,
    query: str,
    documents: list[Any],
    top_n: int | None,
    max_documents: int,
    max_total_bytes: int,
) -> list[str]:
    """Check one request against the limits; return the documents as strings."""
    if not query.strip():
        raise _refuse("'query' must be a non-empty string.")
    if not documents:
        raise _refuse("'documents' must contain at least one document.")
    if len(documents) > max_documents:
        raise _refuse(
            f"'documents' has {len(documents)} entries; at most {max_documents} are accepted "
            "per request."
        )
    if top_n is not None and top_n < 1:
        raise _refuse("'top_n' must be at least 1.")
    texts = document_texts(documents)
    total = len(query.encode()) + sum(len(text.encode()) for text in texts)
    if total > max_total_bytes:
        raise _refuse(
            f"The query and documents total {total} bytes; at most {max_total_bytes} are "
            "accepted per request."
        )
    return texts


def _as_tokens(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def reported_prompt_tokens(payload: dict[str, Any]) -> int | None:
    """The input tokens a reply reports, wherever it keeps them; None if absent."""
    usage = payload.get("usage")
    if isinstance(usage, dict):
        for key in ("prompt_tokens", "input_tokens", "total_tokens"):
            if (tokens := _as_tokens(usage.get(key))) is not None:
                return tokens
    meta = payload.get("meta")
    if isinstance(meta, dict):
        billed = meta.get("billed_units")
        if isinstance(billed, dict):
            for key in ("input_tokens", "total_tokens"):
                if (tokens := _as_tokens(billed.get(key))) is not None:
                    return tokens
        tokens_block = meta.get("tokens")
        if (
            isinstance(tokens_block, dict)
            and (tokens := _as_tokens(tokens_block.get("input_tokens"))) is not None
        ):
            return tokens
    return None


def ledger_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """The reply with ``usage`` in the Chat Completions spelling the ledger reads.

    The provider's own usage keys (such as its reported ``cost``) are kept, so
    pass-through billing still sees them; only the token count is filled in from
    ``meta`` when ``usage`` lacks it.
    """
    usage = dict(payload["usage"]) if isinstance(payload.get("usage"), dict) else {}
    tokens = reported_prompt_tokens(payload)
    if tokens is None:
        return {**payload, "usage": usage or None}
    usage.setdefault("prompt_tokens", tokens)
    if _as_tokens(usage.get("prompt_tokens")) is None:
        usage["prompt_tokens"] = tokens
    usage.setdefault("completion_tokens", 0)
    return {**payload, "usage": usage}


def reply(
    payload: dict[str, Any],
    *,
    texts: list[str],
    top_n: int | None,
    return_documents: bool,
    model: str,
    prompt_tokens: int,
) -> dict[str, Any]:
    """The caller's reply: ``{id, model, results, usage}``, whichever provider answered.

    Results are ordered by descending score and cut to ``top_n``; ``document``
    is the caller's own text for that index (never the provider's echo, which
    would be the redacted text) and only present when asked for.
    """
    raw = payload.get("results")
    if not isinstance(raw, list):
        raise ValueError("the provider's reply has no 'results' list")
    results: list[dict[str, Any]] = []
    for item in raw:
        index = item.get("index") if isinstance(item, dict) else None
        score = item.get("relevance_score", item.get("score")) if isinstance(item, dict) else None
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index < len(texts)
            or not isinstance(score, int | float)
            or isinstance(score, bool)
        ):
            raise ValueError("the provider's reply has a malformed result")
        entry: dict[str, Any] = {"index": index, "relevance_score": score}
        if return_documents:
            entry["document"] = {"text": texts[index]}
        results.append(entry)
    results.sort(key=lambda entry: -entry["relevance_score"])
    if top_n is not None:
        results = results[:top_n]
    tokens = reported_prompt_tokens(payload)
    tokens = prompt_tokens if tokens is None else tokens
    out: dict[str, Any] = {}
    if isinstance(payload.get("id"), str):
        out["id"] = payload["id"]
    out["model"] = model
    out["results"] = results
    out["usage"] = {"prompt_tokens": tokens, "total_tokens": tokens}
    return out
