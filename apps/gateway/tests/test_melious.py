"""Melious: a vendor entry on the OpenAI-compatible protocol (catalogue + one plugin).

The listing is the shape ``GET /v1/models?include_meta=true`` answered with on
2026-11-06, cut to what matters. Decision answers are recorded System One
responses.
"""

from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest
from conftest import FakeUpstream, Seeded
from gateway.catalogue import load_catalogue
from gateway.models import ModelKind, Provider
from gateway.plugins.base import ProviderKind
from gateway.plugins.registry import resolve
from gateway.pricing import facts_by_id, kinds_by_id, parse_catalogue
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from test_decisions import add_decision_model, input_body, latest_record, upstream_path


def meta(kind: str, pricing: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "type": kind,
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "capabilities": {},
        "context_length": None,
        "max_output_tokens": None,
        "reasoning_type": "non_reasoning",
        "pricing": {"currency": "EUR", **pricing},
        **extra,
    }


def listed(model_id: str, **kwargs: Any) -> dict[str, Any]:
    return {"id": model_id, "object": "model", "owned_by": "x", "_meta": meta(**kwargs)}


LISTING: dict[str, Any] = {
    "object": "list",
    "data": [
        listed(
            "kimi-k3",
            kind="chat",
            pricing={
                "input_cost_per_million_eur": 2.85,
                "output_cost_per_million_eur": 14.25,
                "cache_read_cost_per_million_eur": 0.71,
            },
            input_modalities=["text", "image"],
            context_length=256000,
            reasoning_type="hybrid",
            capabilities={"function_calling": True, "structured_output": True},
        ),
        listed(
            "qwen3guard-gen-8b",
            kind="guardrail",
            pricing={"input_cost_per_million_eur": 0.0, "output_cost_per_million_eur": 0.0},
        ),
        listed("bge-m3", kind="embeddings", pricing={"input_cost_per_million_eur": 0.01}),
        listed("flux-2-dev", kind="image", pricing={"image_cost_per_image_eur": 0.015}),
        listed("whisper-large-v3", kind="audio", pricing={"audio_cost_per_minute_eur": 0.003}),
        listed("clef-flash", kind="decision", pricing={"input_cost_per_million_eur": 0.03}),
        listed("qwen3-reranker-4b", kind="reranker", pricing={"input_cost_per_million_eur": 0.15}),
        listed("brand-new-kind", kind="hologram", pricing={"input_cost_per_million_eur": 1}),
    ],
}


async def discovered() -> tuple[dict[str, Any], list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=LISTING)

    plugin = resolve("melious")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        payload = await plugin.fetch_catalogue(http, "https://api.melious.ai/v1/models", "k")  # type: ignore[attr-defined]
    return payload, seen


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

    def test_it_speaks_the_systemone_dialect(self) -> None:
        support = resolve("melious").decisions  # type: ignore[attr-defined]
        assert dict(support.paths) == {"systemone": "/systemone"}

    def test_the_entry_ships_no_price_table(self) -> None:
        assert not load_catalogue(override_dir=None).entries["melious"].prices
        assert date.today() >= date(2026, 11, 6)


class TestDiscovery:
    async def test_the_meta_listing_is_asked_for(self) -> None:
        _, seen = await discovered()
        assert seen == ["https://api.melious.ai/v1/models?include_meta=true"]

    async def test_kinds_come_from_the_providers_own_type(self) -> None:
        payload, _ = await discovered()
        kinds = kinds_by_id(payload)
        assert kinds == {
            "kimi-k3": ModelKind.CHAT,
            "qwen3guard-gen-8b": ModelKind.CHAT,
            "bge-m3": ModelKind.EMBEDDING,
            "flux-2-dev": ModelKind.IMAGE,
            "whisper-large-v3": ModelKind.TRANSCRIPTION,
            "clef-flash": ModelKind.DECISION,
        }
        # The reranker (no surface here) and a type nobody has measured stay out.

    async def test_prices_limits_and_features_come_with_each_model(self) -> None:
        payload, _ = await discovered()
        prices, unpriced = parse_catalogue(payload)
        by_id = {p.model_id: p for p in prices}
        assert unpriced == []
        kimi = by_id["kimi-k3"]
        assert (kimi.input_per_mtok, kimi.output_per_mtok) == (Decimal("2.85"), Decimal("14.25"))
        assert kimi.cache_read_per_mtok == Decimal("0.71") and kimi.currency == "EUR"
        assert kimi.context_window == 256000
        assert {"tools", "json_mode", "reasoning"} <= set(kimi.supported_features)
        assert "image" in kimi.input_modalities
        assert by_id["bge-m3"].output_per_mtok == 0
        assert by_id["flux-2-dev"].per_image == Decimal("0.015")
        assert by_id["whisper-large-v3"].per_minute == Decimal("0.003")
        assert by_id["clef-flash"].input_per_mtok == Decimal("0.03")
        assert facts_by_id(payload)["clef-flash"]["kind"] is ModelKind.DECISION


# -- /v1/decisions on System One ---------------------------------------------------

QUESTIONS: list[dict[str, Any]] = [
    {
        "type": "choice",
        "name": "route",
        "instructions": "Which team?",
        "choices": [{"value": "facilities", "description": "building"}, {"value": "billing"}],
    },
    {
        "type": "score",
        "name": "urgency",
        "instructions": "How urgent?",
        "levels": [{"label": "low"}, {"label": "medium"}, {"label": "high"}],
    },
    {"type": "predicate", "name": "inspect", "instructions": "Needs inspection?"},
]

SYSTEM_ONE_ANSWER: dict[str, Any] = {
    "model": "clef-flash",
    "answers": {
        "route": {
            "type": "choice",
            "choice": "facilities",
            "probabilities": {"facilities": 0.75, "billing": 0.25},
            "confidence": 0.5,
        },
        "urgency": {
            "type": "score",
            "score": 0.9,
            "probabilities": {"0": 0.2, "1": 0.7, "2": 0.1},
            "confidence": 0.6,
            "legend": {"0": "low", "1": "medium", "2": "high"},
        },
        "inspect": {"type": "noul", "noul": 0.0915},
    },
    "usage": {"input_tokens": 124, "output_tokens": 0},
}


@pytest.fixture
async def melious_decider(session: AsyncSession, seeded: Seeded) -> None:
    await session.execute(
        update(Provider).where(Provider.id == seeded.provider.id).values(plugin="melious")
    )
    await session.commit()
    await add_decision_model(session, seeded, upstream="clef-flash", input_per_mtok="0.03")


class TestRoute:
    async def test_decisions_input_form_is_served_through_systemone(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        melious_decider: None,
        fake_upstream: FakeUpstream,
    ) -> None:
        fake_upstream.set_json(copy.deepcopy(SYSTEM_ONE_ANSWER))
        response = await client.post("/v1/decisions", json=input_body(), headers=seeded.auth)

        assert response.status_code == 200, response.text
        assert upstream_path(fake_upstream).endswith("/systemone")
        sent = fake_upstream.last_body
        assert sent["model"] == "clef-flash" and isinstance(sent["questions"], dict)
        body = response.json()
        assert body["model"] == "decider"
        assert [a["type"] for a in body["answers"]] == ["choice", "score", "predicate"]
        assert body["answers"][2]["probability"] == 0.0915
        record = await latest_record(session)
        assert record.prompt_tokens == 124

    async def test_an_unexpressible_question_is_a_400_and_nothing_is_called(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        melious_decider: None,
        fake_upstream: FakeUpstream,
    ) -> None:
        body = input_body()
        body["questions"] = [{"type": "box", "name": "b", "instructions": "?"}]
        response = await client.post("/v1/decisions", json=body, headers=seeded.auth)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_decisions_request"
        assert not fake_upstream.urls

    async def test_the_system_one_form_passes_through_on_both_routes(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        melious_decider: None,
        fake_upstream: FakeUpstream,
    ) -> None:
        state = {
            "model": "decider",
            "state": "text",
            "questions": {"q": {"type": "noul", "instructions": "ok?"}},
        }
        answer = {"model": "clef-flash", "answers": {"q": {"type": "noul", "noul": 0.4}}}
        for route in ("/v1/systemone", "/v1/decisions"):
            fake_upstream.set_json(copy.deepcopy(answer) | {"usage": {"input_tokens": 9}})
            response = await client.post(route, json=state, headers=seeded.auth)
            assert response.status_code == 200, response.text
            assert response.json()["answers"] == answer["answers"]
            assert fake_upstream.last_body["questions"] == state["questions"]
