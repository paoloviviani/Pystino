"""Melious: an EU inference network behind one OpenAI-compatible endpoint.

Everything here was **measured against the live API** (2026-11-06,
``scripts/check_melious_live.py``), not read from documentation alone.

Three findings drive the code:

* **``GET /v1/models?include_meta=true`` is the real catalogue.** The plain list
  is ids only, and one list mixes chat, embedding, image, speech-to-text,
  reranking, guardrail and decision models. With ``include_meta`` each entry
  carries ``_meta``: its ``type``, modalities, context length, capabilities and
  **its EUR prices** (per million tokens, per image, per minute). So discovery
  reads kinds, limits and prices from the provider itself; no hand-kept price
  table. ``fetch_catalogue`` reshapes the entries for ``parse_catalogue``.
* **Decision models are served on ``/v1/systemone`` only**, in the System One
  form (``state`` and a ``questions`` object whose choice and score questions
  carry ``criteria``). ``POST /v1/decisions`` is a 404 there. Callers such as
  Cerea's reviewer send the ``input`` form (``input`` and a ``questions`` array
  of predicate, choice and score questions), so the plugin translates it
  (``DecisionSupport.input_form``) and translates the answer back; a System One
  caller passes through untouched on either route.
* **Reranking is ``POST /v1/rerank``**, which this gateway has no surface for, so
  the reranker is left out of discovery rather than offered as a ``chat`` model
  that cannot work.

Chat, streaming (usage on the last frame without asking), tool calls, reasoning
(``reasoning_content``), embeddings, images (base64 only) and
``/v1/audio/transcriptions`` all take the OpenAI shape unchanged.

Responses also carry ``billing_cost.credits`` (EUR) beside ``usage``. It is not
read: the gateway bills from the configured prices, and the figure sits outside
``usage`` where ``read_reported_cost`` looks.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from gateway.errors import BadRequestError
from gateway.plugins.base import DecisionSupport, InputFormAdapter
from gateway.plugins.generic import GenericOpenAIPlugin

if TYPE_CHECKING:
    import httpx

#: Melious's ``_meta.type`` to what the catalogue parser reads. ``reranker`` is
#: absent on purpose (see the module docstring); so is anything it adds later,
#: which stays out until someone has measured what it is.
_TEXT, _EMBED, _IMAGE = ["text"], ["embeddings"], ["image"]
_OUTPUT_BY_TYPE: dict[str, list[str]] = {
    "chat": _TEXT,
    "guardrail": _TEXT,  # answers on /chat/completions ("Safety: Safe")
    "embeddings": _EMBED,
    "image": _IMAGE,
}
_TAGS_BY_TYPE = {"audio": ["Transcription"], "decision": ["Decision"]}


def _catalogue_entry(model: dict[str, Any]) -> dict[str, Any] | None:
    """One ``/v1/models?include_meta=true`` entry as ``parse_catalogue`` reads it."""
    meta = model.get("_meta")
    meta = meta if isinstance(meta, dict) else {}
    kind = str(meta.get("type") or "chat")
    if kind not in _OUTPUT_BY_TYPE and kind not in _TAGS_BY_TYPE:
        return None
    entry: dict[str, Any] = {"id": model["id"]}
    if kind in _OUTPUT_BY_TYPE:
        entry["output_modalities"] = _OUTPUT_BY_TYPE[kind]
    if kind in _TAGS_BY_TYPE:
        entry["tags"] = _TAGS_BY_TYPE[kind]
    if isinstance(meta.get("input_modalities"), list):
        entry["input_modalities"] = meta["input_modalities"]
    if isinstance(meta.get("context_length"), int):
        entry["context_length"] = meta["context_length"]
    if isinstance(meta.get("max_output_tokens"), int):
        entry["max_output_tokens"] = meta["max_output_tokens"]

    capabilities = meta.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    features = []
    if capabilities.get("function_calling"):
        features.append("tools")
    if capabilities.get("structured_output") or capabilities.get("json_schema"):
        features.append("json_mode")
    if meta.get("reasoning_type") in ("reasoning", "hybrid"):
        features.append("reasoning")
    if features:
        entry["supported_features"] = features

    quoted = meta.get("pricing")
    if isinstance(quoted, dict):
        pricing: dict[str, Any] = {"currency": quoted.get("currency") or "EUR"}
        if quoted.get("input_cost_per_million_eur") is not None:
            pricing["input"] = quoted["input_cost_per_million_eur"]
            # Embedding and decision models are billed per input token only.
            pricing["output"] = quoted.get("output_cost_per_million_eur") or 0
        if quoted.get("cache_read_cost_per_million_eur") is not None:
            pricing["cache_read"] = quoted["cache_read_cost_per_million_eur"]
        if quoted.get("image_cost_per_image_eur") is not None:
            pricing["cost_per_image"] = quoted["image_cost_per_image_eur"]
        if quoted.get("audio_cost_per_minute_eur") is not None:
            pricing["per_minute"] = quoted["audio_cost_per_minute_eur"]
        entry["pricing"] = pricing
    return entry


# -- /v1/decisions on System One ----------------------------------------------

_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


def _refuse(message: str) -> BadRequestError:
    return BadRequestError(message, code="invalid_decisions_request")


def _label(item: Any, *, keys: tuple[str, ...], fallback: str) -> Any:
    if isinstance(item, dict):
        for key in keys:
            if item.get(key) not in (None, ""):
                return item[key]
    elif item is not None:
        return item
    return fallback


def _state_of(value: Any) -> Any:
    """The ``input`` form's text as System One's ``state``: a string.

    A bare string stays. Messages (content a string or ``input_text`` parts)
    are joined: one message is its text, several are ``role: text`` lines, so a
    conversation keeps who said what.
    """
    if not isinstance(value, list):
        return value
    lines: list[tuple[str, str]] = []
    for message in value:
        content = message.get("content") if isinstance(message, dict) else message
        if isinstance(content, list):
            content = "\n".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in content
            )
        lines.append(
            (
                str(message.get("role", "user")) if isinstance(message, dict) else "user",
                str(content),
            )
        )
    if len(lines) == 1:
        return lines[0][1]
    return "\n".join(f"{role}: {text}" for role, text in lines)


def _to_system_one(body: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The ``input`` form as System One, plus what the answer needs to come back.

    Melious documents only the System One form. A predicate is its ``noul``
    question (one number in [0, 1], the probability of yes); a choice's
    ``choices`` become ``criteria`` keyed by their values, and a score's
    ``levels`` an ordered ``criteria`` array. Only ``model``, the text and the
    questions are sent: ``preference`` is a Cortecs routing hint with no
    counterpart here.
    """
    questions = body.get("questions")
    if not isinstance(questions, list) or not questions:
        raise _refuse("'questions' must be a non-empty array when 'input' is used.")
    wire: dict[str, Any] = {}
    plan: list[dict[str, Any]] = []
    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            raise _refuse(f"questions[{index}] must be an object.")
        kind = question.get("type")
        name = question.get("name")
        key = name if isinstance(name, str) and _NAME.match(name) and name not in wire else None
        key = key or f"q{index}"
        while key in wire:
            key += "_"
        rest = {k: v for k, v in question.items() if k not in ("type", "name", "choices", "levels")}
        item: dict[str, Any] = {"index": index, "key": key, "name": name, "type": kind}
        if kind == "predicate":
            wire[key] = {"type": "noul", **rest}
        elif kind == "choice":
            choices = question.get("choices")
            if not isinstance(choices, list) or len(choices) < 2:
                raise _refuse(f"questions[{index}]: a choice needs at least two 'choices'.")
            criteria: dict[str, Any] = {}
            values: dict[str, Any] = {}
            for choice in choices:
                value = choice.get("value") if isinstance(choice, dict) else choice
                label = str(value)
                criteria[label] = _label(choice, keys=("description", "label"), fallback=label)
                values[label] = value
            item["values"] = values
            wire[key] = {"type": "choice", **rest, "criteria": criteria}
        elif kind == "score":
            levels = question.get("levels")
            if not isinstance(levels, list) or len(levels) < 2:
                raise _refuse(f"questions[{index}]: a score needs at least two 'levels'.")
            labels = [
                _label(level, keys=("label", "description", "value"), fallback=str(position))
                for position, level in enumerate(levels)
            ]
            item["labels"] = labels
            wire[key] = {"type": "score", **rest, "criteria": labels}
        else:
            raise _refuse(
                f"questions[{index}]: type {kind!r} is not served for this provider "
                "through /v1/decisions (predicate, choice and score are); "
                "send the System One form to /v1/systemone for its own types."
            )
        plan.append(item)
    out: dict[str, Any] = {
        "model": body["model"],
        "state": _state_of(body["input"]),
        "questions": wire,
    }
    if body.get("images") is not None:
        out["images"] = body["images"]
    return out, plan


def _from_system_one(payload: dict[str, Any], plan: list[dict[str, Any]]) -> dict[str, Any]:
    """System One's keyed answers as the ``input`` form's array, in question order."""
    answers = payload.get("answers")
    answers = answers if isinstance(answers, dict) else {}
    out: list[dict[str, Any]] = []
    for item in plan:
        raw = answers.get(item["key"])
        if not isinstance(raw, dict):
            continue
        answer: dict[str, Any] = {"type": item["type"], "name": item["name"]}
        if raw.get("type") == "refusal" or "refusal" in raw:
            answer = {**raw, "name": item["name"]}
        elif item["type"] == "predicate":
            answer["probability"] = raw.get("noul")
        elif item["type"] == "choice":
            chosen = raw.get("choice")
            answer["choice"] = item["values"].get(chosen, chosen)
            answer["probabilities"] = [
                {"value": item["values"].get(label, label), "probability": probability}
                for label, probability in (raw.get("probabilities") or {}).items()
            ]
            answer["confidence"] = raw.get("confidence")
        else:
            answer["score"] = raw.get("score")
            labels = item["labels"]
            answer["probabilities"] = [
                {
                    "value": int(position),
                    "label": labels[int(position)] if int(position) < len(labels) else None,
                    "probability": probability,
                }
                for position, probability in sorted(
                    (raw.get("probabilities") or {}).items(), key=lambda pair: int(pair[0])
                )
            ]
            answer["confidence"] = raw.get("confidence")
        out.append(answer)
    return {**{k: v for k, v in payload.items() if k != "answers"}, "answers": out}


class MeliousPlugin(GenericOpenAIPlugin):
    """Melious: OpenAI-compatible, EU-only, priced from the configured prices."""

    name = "melious"
    label = "Melious"
    description = (
        "Melious, an EU-only inference network, OpenAI-compatible. Billed from the "
        "configured prices, which discovery reads from its catalogue."
    )
    default_base_url: str | None = "https://api.melious.ai/v1"
    #: System One only upstream (``/decisions`` is a 404 on Melious). Both routes
    #: land on ``/systemone``; the ``input`` form is translated. Melious
    #: documents no routing fields (its routing is its own), so none are refused.
    decisions = DecisionSupport(
        paths={"decisions": "/systemone", "systemone": "/systemone"},
        input_form=InputFormAdapter(to_upstream=_to_system_one, from_upstream=_from_system_one),
    )

    async def fetch_catalogue(
        self, http: httpx.AsyncClient, url: str, api_key: str | None
    ) -> dict[str, Any]:
        """``GET /models?include_meta=true``, each entry reshaped for the parser."""
        from gateway.pricing import fetch_catalogue

        payload = await fetch_catalogue(http, f"{url}?include_meta=true", api_key)
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return {"data": []}
        entries = (
            _catalogue_entry(item) for item in items if isinstance(item, dict) and item.get("id")
        )
        return {"data": [entry for entry in entries if entry is not None]}
