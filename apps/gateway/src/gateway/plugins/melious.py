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
  of predicate, choice and score questions); the plugin only declares the
  ``systemone`` dialect, and ``gateway.translate.decisions`` translates the
  ``input`` form to it and the answer back.
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

from typing import TYPE_CHECKING, Any

from gateway.plugins.base import DIALECT_SYSTEMONE, DecisionSupport
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


class MeliousPlugin(GenericOpenAIPlugin):
    """Melious: OpenAI-compatible, EU-only, priced from the configured prices."""

    name = "melious"
    label = "Melious"
    description = (
        "Melious, an EU-only inference network, OpenAI-compatible. Billed from the "
        "configured prices, which discovery reads from its catalogue."
    )
    default_base_url: str | None = "https://api.melious.ai/v1"
    #: System One only upstream (``/decisions`` is a 404 on Melious): it speaks
    #: the ``systemone`` dialect, and the gateway translates a ``decisions``
    #: request to it and back. Melious documents no routing fields (its routing
    #: is its own), so none are refused.
    decisions = DecisionSupport(dialect=DIALECT_SYSTEMONE, path="/systemone")

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
