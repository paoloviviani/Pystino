"""Melious: an EU inference network behind one OpenAI-compatible endpoint.

Everything here was **measured against the live API** (2026-11-06,
``scripts/check_melious_live.py``), not read from documentation.

Three findings drive the code:

* **``GET /v1/models`` is a bare OpenAI list** (``id``, ``object``, ``created``,
  ``owned_by``): no kind, no modalities, no context size, no prices. One list
  mixes chat, embedding, image, speech-to-text, reranking and decision models,
  so left alone every one of them would be imported as ``chat``. The kind is
  read from the id (``fetch_catalogue``); prices come from the catalogue entry's
  hints (``catalogue_data/providers.d/melious.yaml``).
* **Decision models are served on ``/v1/systemone`` only.** ``POST /v1/decisions``
  answers 404 there; ``/v1/systemone`` takes the System One form (``state`` and a
  ``questions`` object) and answers with ``usage.input_tokens``. So the plugin
  declares that one route, and a ``/v1/decisions`` call is refused with a 400
  naming the route rather than forwarded to a 404.
* **Reranking is ``POST /v1/rerank``**, which this gateway has no surface for,
  so the reranker is left out of discovery rather than offered as a ``chat``
  model that cannot work.

Chat, streaming (usage on the last frame without asking), tool calls, reasoning
(``reasoning_content``), embeddings, images (base64 only) and
``/v1/audio/transcriptions`` all take the OpenAI shape unchanged.

Responses also carry ``billing_cost.credits`` (EUR, 10,000 energy = EUR 1) beside
``usage``. It is not read: the gateway bills from the configured prices, and the
figure sits outside ``usage`` where ``read_reported_cost`` looks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gateway.plugins.base import DecisionSupport
from gateway.plugins.generic import GenericOpenAIPlugin

if TYPE_CHECKING:
    import httpx

#: Substrings of a model id that make it an embedding model. Measured against the
#: 36 ids Melious listed on 2026-11-06: bge-m3, bge-multilingual-gemma2,
#: multilingual-e5-large(-instruct), qwen3-embedding-8b,
#: paraphrase-multilingual-mpnet.
_EMBEDDING_MARKERS = ("bge-", "e5-", "embedding", "mpnet")


def _catalogue_entry(model: dict[str, Any]) -> dict[str, Any] | None:
    """One ``/v1/models`` entry with the kind the id implies, or None to omit it."""
    identifier = str(model["id"])
    lowered = identifier.lower()
    entry: dict[str, Any] = {"id": identifier}
    if "rerank" in lowered:
        # `/v1/rerank` has no surface here; as `chat` it would be a trap.
        return None
    if lowered.startswith("clef"):
        # Decision models report nothing else; only a tag tells them apart.
        entry["tags"] = ["Decision"]
    elif "whisper" in lowered:
        entry["tags"] = ["Transcription"]
    elif lowered.startswith("flux"):
        entry["output_modalities"] = ["image"]
    elif any(marker in lowered for marker in _EMBEDDING_MARKERS):
        entry["output_modalities"] = ["embeddings"]
    else:
        entry["output_modalities"] = ["text"]
    return entry


class MeliousPlugin(GenericOpenAIPlugin):
    """Melious: OpenAI-compatible, EU-only, priced from the configured prices."""

    name = "melious"
    label = "Melious"
    description = (
        "Melious, an EU-only inference network, OpenAI-compatible. Billed from the "
        "configured prices, which discovery fills from the catalogue."
    )
    default_base_url: str | None = "https://api.melious.ai/v1"
    #: ``/v1/models`` lists no prices and Melious has no price API; the entry's
    #: dated hints are applied without the operator ticking "fill missing prices".
    price_hints_always = True
    #: System One only: ``/decisions`` is a 404 on Melious (2026-11-06). Melious
    #: documents no routing fields (its routing is its own), so none are refused.
    decisions = DecisionSupport(paths={"systemone": "/systemone"})

    async def fetch_catalogue(
        self, http: httpx.AsyncClient, url: str, api_key: str | None
    ) -> dict[str, Any]:
        """The bearer fetch, then each entry given the kind its id implies."""
        from gateway.pricing import fetch_catalogue

        payload = await fetch_catalogue(http, url, api_key)
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return {"data": []}
        entries = (
            _catalogue_entry(item) for item in items if isinstance(item, dict) and item.get("id")
        )
        return {"data": [entry for entry in entries if entry is not None]}
