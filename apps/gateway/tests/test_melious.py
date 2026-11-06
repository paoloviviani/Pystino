"""Melious: a vendor entry on the OpenAI-compatible protocol (catalogue + one plugin).

The listing is the shape ``GET /v1/models`` answered with on 2026-11-06: a bare
OpenAI list, ids only, chat and every other kind mixed together.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import httpx
from gateway.catalogue import load_catalogue
from gateway.models import ModelKind
from gateway.plugins.base import ProviderKind
from gateway.plugins.registry import resolve
from gateway.pricing import facts_by_id, kinds_by_id, parse_catalogue
from gateway.routers.admin import _fill_from_hints

IDS = [
    "mistral-small-3.2-24b-instruct",
    "qwen3.6-27b",
    "bge-m3",
    "multilingual-e5-large-instruct",
    "qwen3-embedding-8b",
    "paraphrase-multilingual-mpnet",
    "bge-multilingual-gemma2",
    "flux-2-klein-4b",
    "whisper-large-v3-turbo",
    "qwen3-reranker-4b",
    "qwen3guard-gen-8b",
    "clef-flash",
    "clef",
    "brand-new-model-9",
]


def listing() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": i, "object": "model", "created": 1, "owned_by": "melious"} for i in IDS],
    }


async def discovered() -> dict[str, Any]:
    plugin = resolve("melious")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=listing()))
    async with httpx.AsyncClient(transport=transport) as http:
        return await plugin.fetch_catalogue(http, "https://api.melious.ai/v1/models", "k")  # type: ignore[attr-defined,no-any-return]


class TestEntry:
    def test_the_catalogue_entry_loads_with_the_plugin(self) -> None:
        entry = load_catalogue(override_dir=None).entries["melious"]
        assert entry.plugin == "melious"
        assert entry.effective_base_url == "https://api.melious.ai/v1"
        assert entry.region == "eu"
        assert entry.retention.trains_on_customer_data is False
        assert resolve("melious").default_base_url == entry.base_url

    def test_it_is_a_provider_on_the_openai_protocol_with_no_authoritative_cost(self) -> None:
        plugin = resolve("melious")
        assert plugin.kind is ProviderKind.PROVIDER
        assert plugin.protocol == "openai"  # type: ignore[attr-defined]
        assert plugin.reports_authoritative_cost is False

    def test_decisions_are_served_on_systemone_only(self) -> None:
        support = resolve("melious").decisions  # type: ignore[attr-defined]
        assert dict(support.paths) == {"systemone": "/systemone"}


class TestDiscovery:
    async def test_kinds_are_read_from_the_ids(self) -> None:
        kinds = kinds_by_id(await discovered())
        assert kinds["mistral-small-3.2-24b-instruct"] is ModelKind.CHAT
        assert kinds["qwen3guard-gen-8b"] is ModelKind.CHAT
        assert kinds["brand-new-model-9"] is ModelKind.CHAT
        for embedding in (
            "bge-m3",
            "multilingual-e5-large-instruct",
            "qwen3-embedding-8b",
            "paraphrase-multilingual-mpnet",
            "bge-multilingual-gemma2",
        ):
            assert kinds[embedding] is ModelKind.EMBEDDING, embedding
        assert kinds["flux-2-klein-4b"] is ModelKind.IMAGE
        assert kinds["whisper-large-v3-turbo"] is ModelKind.TRANSCRIPTION
        assert kinds["clef-flash"] is ModelKind.DECISION
        assert kinds["clef"] is ModelKind.DECISION

    async def test_the_reranker_is_not_offered_as_a_chat_model(self) -> None:
        assert "qwen3-reranker-4b" not in kinds_by_id(await discovered())

    async def test_prices_come_from_the_hints_and_unpriced_stays_unpriced(self) -> None:
        payload = await discovered()
        _, unpriced = parse_catalogue(payload)
        entry = load_catalogue(override_dir=None).entries["melious"]
        filled = {
            p.model_id: p for p in _fill_from_hints(entry, [], unpriced, facts_by_id(payload))
        }
        cheap = filled["mistral-small-3.2-24b-instruct"]
        assert (cheap.input_per_mtok, cheap.output_per_mtok) == (Decimal("0.10"), Decimal("0.30"))
        assert cheap.currency == "EUR" and cheap.kind is ModelKind.CHAT
        assert filled["qwen3-embedding-8b"].kind is ModelKind.EMBEDDING
        assert filled["clef-flash"].kind is ModelKind.DECISION
        assert filled["clef-flash"].input_per_mtok == Decimal("0.03")
        # No published per-image or per-minute rate, so never imported as free.
        assert "flux-2-klein-4b" not in filled
        assert "whisper-large-v3-turbo" not in filled
        assert "brand-new-model-9" not in filled

    def test_every_hint_is_dated_and_in_euro(self) -> None:
        entry = load_catalogue(override_dir=None).entries["melious"]
        assert entry.prices
        for hint in entry.prices:
            assert hint.currency == "EUR"
            assert hint.effective_from <= date.today()
