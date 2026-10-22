"""Decision models: ``POST /v1/decisions`` and ``POST /v1/systemone`` (ADR 0100).

The response bodies below are recorded from the live Cortecs Decisions API
(2026-10-22, models ``laya`` and ``clef-flash``), with nothing in them that
identifies an account. What they pin is what would otherwise be found on an
invoice: that ``usage.cost`` is integer micro-EUR rounded up per request, that
the request and the answer pass through in the format they arrived in, that the
deployment's routing settings cannot be overridden by a caller, and that the
text is redacted like any other.
"""

from __future__ import annotations

import copy
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
from conftest import FakeUpstream, Seeded
from gateway.models import (
    ApiSurface,
    BillingMode,
    CostSource,
    GroupModelAccess,
    LimitMetric,
    LimitRule,
    LimitScope,
    ModelDef,
    ModelKind,
    ModelPrice,
    ModelRouteTarget,
    Provider,
    UsageRecord,
    UsageSource,
    UsageStatus,
)
from gateway.plugins.cortecs import CortecsRouterPlugin
from gateway.routers.decisions import _rebuild, _strings
from gateway.schemas import DecisionsRequest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin

# -- recorded wire shapes -------------------------------------------------------

#: A choice, a score and a predicate in one request, as recorded.
INPUT_QUESTIONS: list[dict[str, Any]] = [
    {
        "type": "choice",
        "name": "route",
        "instructions": "Which team should handle this?",
        "choices": [
            {"value": "facilities", "description": "building and equipment"},
            {"value": "billing"},
            {"value": "legal"},
        ],
    },
    {
        "type": "score",
        "name": "urgency",
        "instructions": "How urgent?",
        "levels": [{"label": "low"}, {"label": "medium"}, {"label": "high"}],
    },
    {"type": "predicate", "name": "inspect", "instructions": "Needs inspection?"},
]

INPUT_ANSWER: dict[str, Any] = {
    "model": "laya",
    "answers": [
        {
            "type": "choice",
            "name": "route",
            "choice": "facilities",
            "probabilities": [
                {"value": "facilities", "probability": 0.7764},
                {"value": "billing", "probability": 0.0138},
                {"value": "legal", "probability": 0.2098},
            ],
            "confidence": 0.6646000000000001,
        },
        {
            "type": "score",
            "name": "urgency",
            "score": 1.2398,
            "probabilities": [
                {"value": 0, "label": "low", "probability": 0.0577},
                {"value": 1, "label": "medium", "probability": 0.6448},
                {"value": 2, "label": "high", "probability": 0.2975},
            ],
            "confidence": 0.46720000000000006,
        },
        {"type": "predicate", "name": "inspect", "probability": 0.0915},
    ],
    "usage": {
        "input_tokens": 143,
        "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
        "output_tokens": 0,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 143,
        "cost": 7,
        "cost_details": {"prompt_cost": 7, "completion_cost": 0},
    },
}

STATE_QUESTIONS: dict[str, Any] = {
    "inspect": {"type": "noul", "instructions": "Needs inspection?"},
    "team": {
        "type": "choice",
        "instructions": "Which team?",
        "criteria": {"facilities": "equipment", "billing": "money"},
    },
    "urg": {"type": "score", "criteria": ["low", "medium", "high"]},
}

#: System One's usage is slimmer: no *_details, no total_tokens.
STATE_ANSWER: dict[str, Any] = {
    "model": "laya",
    "answers": {
        "inspect": {"type": "noul", "noul": 0.0323},
        "team": {
            "type": "choice",
            "choice": "facilities",
            "confidence": 0.9810000000000001,
            "probabilities": {"facilities": 0.9905, "billing": 0.0095},
        },
        "urg": {
            "type": "score",
            "score": 1.3047695230476952,
            "confidence": 0.33916608339166077,
            "legend": {"0": "low", "1": "medium", "2": "high"},
            "probabilities": {"0": 0.0679, "1": 0.5594, "2": 0.3727},
        },
    },
    "usage": {
        "input_tokens": 124,
        "output_tokens": 0,
        "cost": 6,
        "cost_details": {"prompt_cost": 6, "completion_cost": 0},
    },
}

#: Where Cortecs names the provider and model that served a request: the same
#: headers as chat, not the body.
SERVED_HEADERS = {"x-cortecs-provider": "berget", "x-cortecs-model": "laya"}

#: Recorded error bodies. Code is a string; unknown model is a 404.
NOT_FOUND = {
    "error": {
        "message": "NotFoundError: Model not found.",
        "type": "invalid_request_error",
        "param": None,
        "code": "404",
    }
}
BAD_REQUEST = {
    "error": {
        "message": "BadRequestError: Invalid Decisions request: 3 validation errors",
        "type": "invalid_request_error",
        "param": None,
        "code": "400",
    }
}

INPUT_TEXT = "The cold-storage room is at 9 C, max allowed 5 C. Please send someone."
IBAN = "IT60X0542811101000000123456"


def input_body(model: str = "decider", **extra: Any) -> dict[str, Any]:
    return {
        "model": model,
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": INPUT_TEXT}]},
        ],
        "questions": copy.deepcopy(INPUT_QUESTIONS),
        **extra,
    }


def state_body(model: str = "decider", **extra: Any) -> dict[str, Any]:
    return {
        "model": model,
        "state": "The cold-storage room is at 9 C, max allowed 5 C.",
        "questions": copy.deepcopy(STATE_QUESTIONS),
        **extra,
    }


# -- fixtures -------------------------------------------------------------------


async def make_cortecs(
    session: AsyncSession, seeded: Seeded, *, mode: BillingMode = BillingMode.OWN_PRICES
) -> None:
    """The seeded provider, as a Cortecs one. An explicit UPDATE: `seeded.provider`
    belongs to the fixture's session and assigning to it would persist nothing."""
    await session.execute(
        update(Provider)
        .where(Provider.id == seeded.provider.id)
        .values(plugin="cortecs", billing_mode=mode)
    )
    await session.commit()


async def add_decision_model(
    session: AsyncSession,
    seeded: Seeded,
    *,
    name: str = "decider",
    upstream: str = "laya",
    input_per_mtok: str = "0.042",
    output_per_mtok: str = "0",
    grant: bool = True,
) -> ModelDef:
    model = ModelDef(
        name=name,
        upstream_model=upstream,
        provider_id=seeded.provider.id,
        kind=ModelKind.DECISION,
    )
    session.add(model)
    await session.flush()
    session.add(
        ModelPrice(
            model_id=model.id,
            input_per_mtok=Decimal(input_per_mtok),
            output_per_mtok=Decimal(output_per_mtok),
            currency="EUR",
        )
    )
    if grant:
        session.add(GroupModelAccess(group_id=seeded.group.id, model_id=model.id))
    await session.commit()
    return model


@pytest.fixture
async def decider(session: AsyncSession, seeded: Seeded) -> ModelDef:
    await make_cortecs(session, seeded)
    return await add_decision_model(session, seeded)


async def latest_record(session: AsyncSession) -> UsageRecord:
    stmt = select(UsageRecord).order_by(UsageRecord.created_at.desc()).limit(1)
    return (await session.execute(stmt)).scalars().one()


async def all_records(session: AsyncSession) -> list[UsageRecord]:
    return list((await session.execute(select(UsageRecord))).scalars())


def upstream_path(fake_upstream: FakeUpstream) -> str:
    return httpx.URL(fake_upstream.urls[-1]).path


# -- routing and pass-through ---------------------------------------------------


class TestPassThrough:
    async def test_the_input_format_goes_to_decisions_and_comes_back_in_kind(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        fake_upstream.set_json(INPUT_ANSWER, headers=SERVED_HEADERS)
        response = await client.post("/v1/decisions", json=input_body(), headers=seeded.auth)

        assert response.status_code == 200, response.text
        assert upstream_path(fake_upstream).endswith("/decisions")
        sent = fake_upstream.last_body
        # The question bodies are the caller's and are not touched.
        assert sent["questions"] == INPUT_QUESTIONS
        assert sent["input"][0]["content"][0]["text"] == INPUT_TEXT
        # Answers pass through byte for byte, except the model name (ours).
        expected = copy.deepcopy(INPUT_ANSWER)
        expected["model"] = "decider"
        assert response.json() == expected

    async def test_the_upstream_model_name_is_sent_and_ours_is_returned(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await make_cortecs(session, seeded)
        await add_decision_model(session, seeded, name="triage", upstream="clef-flash")
        fake_upstream.set_json(INPUT_ANSWER, headers=SERVED_HEADERS)

        response = await client.post(
            "/v1/decisions", json=input_body("triage"), headers=seeded.auth
        )
        assert fake_upstream.last_body["model"] == "clef-flash"
        assert response.json()["model"] == "triage"

    async def test_a_plain_input_string_is_accepted(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        fake_upstream.set_json(
            {
                "model": "laya",
                "answers": [{"type": "predicate", "name": None, "probability": 0.4688}],
                "usage": {"input_tokens": 32, "output_tokens": 0, "cost": 2},
            }
        )
        response = await client.post(
            "/v1/decisions",
            json={
                "model": "decider",
                "input": "hi",
                "questions": [{"type": "predicate", "instructions": "Is this a greeting?"}],
            },
            headers=seeded.auth,
        )
        assert response.status_code == 200
        assert fake_upstream.last_body["input"] == "hi"
        assert response.json()["answers"][0]["name"] is None

    async def test_the_system_one_format_goes_to_systemone(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        fake_upstream.set_json(STATE_ANSWER, headers=SERVED_HEADERS)
        response = await client.post("/v1/systemone", json=state_body(), headers=seeded.auth)

        assert response.status_code == 200, response.text
        assert upstream_path(fake_upstream).endswith("/systemone")
        assert fake_upstream.last_body["questions"] == STATE_QUESTIONS
        expected = copy.deepcopy(STATE_ANSWER)
        expected["model"] = "decider"
        assert response.json() == expected

    async def test_decisions_also_takes_the_system_one_format(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Cortecs answers it in kind, so the gateway forwards it to /decisions."""
        fake_upstream.set_json(STATE_ANSWER)
        response = await client.post("/v1/decisions", json=state_body(), headers=seeded.auth)
        assert response.status_code == 200
        assert upstream_path(fake_upstream).endswith("/decisions")
        assert set(response.json()["answers"]) == {"inspect", "team", "urg"}

    async def test_a_null_model_in_a_system_one_answer_stays_null(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        answer = copy.deepcopy(STATE_ANSWER)
        answer["model"] = None
        fake_upstream.set_json(answer)
        response = await client.post("/v1/systemone", json=state_body(), headers=seeded.auth)
        assert response.status_code == 200
        assert response.json()["model"] is None

    async def test_preference_passes_through(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        fake_upstream.set_json(INPUT_ANSWER)
        response = await client.post(
            "/v1/decisions", json=input_body(preference="cost"), headers=seeded.auth
        )
        assert response.status_code == 200
        assert fake_upstream.last_body["preference"] == "cost"

    async def test_a_refusal_answer_passes_through_and_is_charged(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Documented (`type: refusal`) but not reproduced live, so the reason's
        field name is a guess here: what is pinned is that nothing is parsed or
        dropped, and that the provider still charged for reading the text."""
        answer = copy.deepcopy(INPUT_ANSWER)
        answer["answers"][2] = {
            "type": "refusal",
            "name": "inspect",
            "refusal": "I can't answer that.",
        }
        fake_upstream.set_json(answer)
        response = await client.post("/v1/decisions", json=input_body(), headers=seeded.auth)

        assert response.status_code == 200
        assert response.json()["answers"][2] == answer["answers"][2]
        assert (await latest_record(session)).prompt_tokens == 143


# -- the request's shape --------------------------------------------------------


class TestRequestShape:
    @pytest.mark.parametrize(
        "mutate",
        [
            lambda b: b.update(state="x"),  # both formats
            lambda b: b.pop("input"),  # neither
            lambda b: b.update(questions=[]),
            lambda b: b.update(questions={"a": {"type": "noul"}}),  # object with `input`
        ],
    )
    async def test_a_body_that_is_not_exactly_one_format_is_refused_before_the_call(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        decider: ModelDef,
        fake_upstream: FakeUpstream,
        mutate: Any,
    ) -> None:
        body = input_body()
        mutate(body)
        response = await client.post("/v1/decisions", json=body, headers=seeded.auth)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_decisions_request"
        assert fake_upstream.bodies == []
        assert await all_records(session) == []

    async def test_a_state_body_needs_an_object_of_questions(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        body = state_body()
        body["questions"] = [{"type": "noul", "instructions": "x"}]
        response = await client.post("/v1/decisions", json=body, headers=seeded.auth)
        assert response.status_code == 400
        assert fake_upstream.bodies == []

    async def test_systemone_refuses_the_input_format(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        response = await client.post("/v1/systemone", json=input_body(), headers=seeded.auth)
        assert response.status_code == 400
        assert "System One" in response.json()["error"]["message"]
        assert fake_upstream.bodies == []

    async def test_an_empty_state_string_is_still_a_state(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        fake_upstream.set_json(STATE_ANSWER)
        body = state_body()
        body["state"] = ""
        response = await client.post("/v1/systemone", json=body, headers=seeded.auth)
        assert response.status_code == 200


# -- routing fields belong to the deployment ------------------------------------


class TestRoutingFields:
    @pytest.mark.parametrize(
        "field,value",
        [
            ("allowed_providers", ["acme"]),
            ("eu_native", False),
            ("provider_order", "custom"),
            ("quantizations", ["fp8"]),
            ("allow_zero_data_retention", False),
            ("enable_model_fallback", True),
            ("fallbacks", ["other-model"]),
        ],
    )
    async def test_each_is_refused_naming_the_field(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        decider: ModelDef,
        fake_upstream: FakeUpstream,
        field: str,
        value: object,
    ) -> None:
        for route, body in (("decisions", input_body()), ("systemone", state_body())):
            body[field] = value
            response = await client.post(f"/v1/{route}", json=body, headers=seeded.auth)
            assert response.status_code == 400, (route, response.text)
            error = response.json()["error"]
            assert error["code"] == "routing_field_refused"
            assert f"'{field}'" in error["message"]
        # Nothing reached the provider and nothing was reserved or recorded.
        assert fake_upstream.bodies == []
        assert await all_records(session) == []

    async def test_a_false_value_is_still_refused(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        """`eu_native: false` is the dangerous one: it widens the pool."""
        response = await client.post(
            "/v1/decisions", json=input_body(eu_native=False), headers=seeded.auth
        )
        assert response.status_code == 400

    async def test_a_null_routing_field_is_dropped_not_forwarded(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        fake_upstream.set_json(INPUT_ANSWER)
        response = await client.post(
            "/v1/decisions",
            json=input_body(eu_native=None, fallbacks=None),
            headers=seeded.auth,
        )
        assert response.status_code == 200
        assert "eu_native" not in fake_upstream.last_body
        assert "fallbacks" not in fake_upstream.last_body


# -- metering -------------------------------------------------------------------


class TestMetering:
    async def test_cortecs_cost_is_micro_eur_and_the_table_figure_is_kept(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Provider-reported: 143 tokens at 0.042 EUR/M is 6.006 micro-EUR, which
        Cortecs rounds up to 7 per request. The 7 is the charge; 6.006 is kept as
        the computed figure, so the 1 micro-EUR gap is explained, not hidden."""
        await make_cortecs(session, seeded, mode=BillingMode.PROVIDER_REPORTED)
        await add_decision_model(session, seeded)
        fake_upstream.set_json(INPUT_ANSWER, headers=SERVED_HEADERS)

        await client.post("/v1/decisions", json=input_body(), headers=seeded.auth)

        record = await latest_record(session)
        assert record.api_surface is ApiSurface.DECISIONS
        assert record.status is UsageStatus.COMPLETED
        assert record.prompt_tokens == 143
        assert record.completion_tokens == 0
        assert record.usage_source is UsageSource.UPSTREAM_EXACT
        assert record.cost_source is CostSource.PROVIDER_REPORTED
        assert record.cost == Decimal("0.000007")
        assert record.upstream_cost == Decimal("0.000007")
        assert record.upstream_cost_currency == "EUR"
        assert record.upstream_cost_details == {"prompt_cost": 7, "completion_cost": 0}
        assert record.computed_cost == Decimal("0.000006006")

    async def test_own_prices_mode_charges_the_table_and_keeps_theirs(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await make_cortecs(session, seeded)
        await add_decision_model(session, seeded)
        fake_upstream.set_json(INPUT_ANSWER)

        await client.post("/v1/decisions", json=input_body(), headers=seeded.auth)

        record = await latest_record(session)
        assert record.cost_source is CostSource.OWN_PRICES
        assert record.cost == Decimal("0.000006006")
        assert record.upstream_cost == Decimal("0.000007")

    async def test_system_one_usage_is_read_the_same_way(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await make_cortecs(session, seeded, mode=BillingMode.PROVIDER_REPORTED)
        await add_decision_model(session, seeded)
        fake_upstream.set_json(STATE_ANSWER)

        await client.post("/v1/systemone", json=state_body(), headers=seeded.auth)

        record = await latest_record(session)
        assert record.prompt_tokens == 124
        assert record.cost == Decimal("0.000006")

    async def test_the_price_table_is_the_fallback_when_no_cost_is_reported(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await make_cortecs(session, seeded, mode=BillingMode.PROVIDER_REPORTED)
        await add_decision_model(session, seeded)
        answer = copy.deepcopy(INPUT_ANSWER)
        del answer["usage"]["cost"], answer["usage"]["cost_details"]
        fake_upstream.set_json(answer)

        await client.post("/v1/decisions", json=input_body(), headers=seeded.auth)

        record = await latest_record(session)
        assert record.cost_source is CostSource.OWN_PRICES_FALLBACK
        assert record.cost == Decimal("0.000006006")

    async def test_the_served_model_and_provider_are_recorded_as_reported(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        fake_upstream.set_json(INPUT_ANSWER, headers=SERVED_HEADERS)
        await client.post("/v1/decisions", json=input_body(), headers=seeded.auth)

        record = await latest_record(session)
        assert record.model_name == "decider"
        assert record.upstream_model == "laya"
        assert record.upstream_provider == "berget"
        assert record.model_substituted is False

    async def test_a_fallback_model_named_in_the_answer_is_disclosed_as_a_substitution(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Billing follows the model that served the request (issue #3): when the
        provider reports another model, that is what the row says."""
        answer = copy.deepcopy(INPUT_ANSWER)
        answer["model"] = "clef"
        fake_upstream.set_json(
            answer, headers={"x-cortecs-provider": "berget", "x-cortecs-model": "clef"}
        )
        await client.post("/v1/decisions", json=input_body(), headers=seeded.auth)

        record = await latest_record(session)
        assert record.upstream_model == "clef"
        assert record.model_substituted is True

    async def test_missing_usage_is_estimated_not_recorded_as_free(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        answer = copy.deepcopy(INPUT_ANSWER)
        del answer["usage"]
        fake_upstream.set_json(answer)
        await client.post("/v1/decisions", json=input_body(), headers=seeded.auth)

        record = await latest_record(session)
        assert record.usage_source is UsageSource.ESTIMATED
        assert record.prompt_tokens > 0

    async def test_one_record_per_request(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        fake_upstream.set_json(INPUT_ANSWER)
        await client.post("/v1/decisions", json=input_body(), headers=seeded.auth)
        await client.post("/v1/systemone", json=state_body(), headers=seeded.auth)
        assert len(await all_records(session)) == 2

    async def test_the_provider_reported_cost_unit_is_micro_eur(self) -> None:
        """The unit conversion itself, on the recorded figures (7 -> 0.000007 EUR)."""
        plugin = CortecsRouterPlugin()
        reported = plugin.read_reported_cost(INPUT_ANSWER["usage"])
        assert reported is not None
        assert reported.amount == Decimal("0.000007")
        assert reported.currency == "EUR"
        assert reported.authoritative is True

    async def test_a_zero_cost_is_a_charge_of_zero_not_an_absence(self) -> None:
        reported = CortecsRouterPlugin().read_reported_cost({"input_tokens": 0, "cost": 0})
        assert reported is not None and reported.amount == 0


# -- errors ---------------------------------------------------------------------


class TestErrors:
    @pytest.mark.parametrize(
        "status,body", [(404, NOT_FOUND), (400, BAD_REQUEST)], ids=["not_found", "bad_request"]
    )
    async def test_the_providers_error_passes_through_and_nothing_is_charged(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        decider: ModelDef,
        fake_upstream: FakeUpstream,
        status: int,
        body: dict[str, Any],
    ) -> None:
        fake_upstream.set_json(body, status=status)
        response = await client.post("/v1/decisions", json=input_body(), headers=seeded.auth)

        assert response.status_code == status
        assert response.json() == body
        record = await latest_record(session)
        assert record.status is UsageStatus.UPSTREAM_ERROR
        assert record.cost == 0
        assert record.upstream_status == status

    async def test_an_unreachable_provider_is_a_502_and_settles_the_reservation(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        def boom(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        fake_upstream._responder = boom
        response = await client.post("/v1/decisions", json=input_body(), headers=seeded.auth)
        assert response.status_code == 502
        assert (await latest_record(session)).cost == 0


# -- access and quota -----------------------------------------------------------


class TestAccess:
    async def test_an_ungranted_model_is_not_served(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await make_cortecs(session, seeded)
        await add_decision_model(session, seeded, name="secret", grant=False)
        response = await client.post(
            "/v1/decisions", json=input_body("secret"), headers=seeded.auth
        )
        assert response.status_code in (403, 404)
        assert fake_upstream.bodies == []

    async def test_no_credential_is_a_401(
        self, client: httpx.AsyncClient, decider: ModelDef
    ) -> None:
        for route, body in (("decisions", input_body()), ("systemone", state_body())):
            assert (await client.post(f"/v1/{route}", json=body)).status_code == 401

    async def test_a_decision_model_is_refused_on_the_chat_route_naming_the_right_one(
        self, client: httpx.AsyncClient, seeded: Seeded, decider: ModelDef
    ) -> None:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "decider", "messages": [{"role": "user", "content": "hi"}]},
            headers=seeded.auth,
        )
        assert response.status_code == 400
        assert "/v1/decisions" in response.json()["error"]["message"]

    async def test_a_chat_model_is_refused_on_the_decision_routes(
        self, client: httpx.AsyncClient, seeded: Seeded, decider: ModelDef
    ) -> None:
        for route, body in (
            ("decisions", input_body(seeded.model.name)),
            ("systemone", state_body(seeded.model.name)),
        ):
            response = await client.post(f"/v1/{route}", json=body, headers=seeded.auth)
            assert response.status_code == 400, route

    async def test_a_provider_without_decision_support_cannot_serve_one(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        # The seeded provider is the generic OpenAI-compatible plugin.
        await add_decision_model(session, seeded)
        response = await client.post("/v1/decisions", json=input_body(), headers=seeded.auth)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "provider_no_decision_support"
        assert fake_upstream.bodies == []
        assert await all_records(session) == []

    async def test_the_model_is_listed_with_its_kind(
        self, client: httpx.AsyncClient, seeded: Seeded, decider: ModelDef
    ) -> None:
        response = await client.get("/v1/models", headers=seeded.auth)
        cards = {card["id"]: card for card in response.json()["data"]}
        assert cards["decider"]["kind"] == "decision"
        assert cards[seeded.model.name]["kind"] == "chat"


class TestQuota:
    async def test_a_cost_ceiling_refuses_before_the_call_and_settles_after(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        async with session_factory() as rules:
            rules.add(
                LimitRule(
                    name="two requests",
                    scope=LimitScope.GROUP,
                    scope_id=seeded.group.id,
                    metric=LimitMetric.REQUESTS,
                    window_seconds=3600,
                    limit_value=Decimal(2),
                )
            )
            await rules.commit()
        fake_upstream.set_json(INPUT_ANSWER)

        statuses = [
            (await client.post("/v1/decisions", json=input_body(), headers=seeded.auth)).status_code
            for _ in range(3)
        ]
        assert statuses == [200, 200, 429]
        # The refused one never reached the provider.
        assert len(fake_upstream.bodies) == 2

    async def test_tokens_settle_into_the_ledger_from_the_reported_count(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        """A 250-token ceiling is under two requests' settled 286 tokens but over
        any one request's estimate, so a refusal can only come from the settled
        counts accumulating."""
        async with session_factory() as rules:
            rules.add(
                LimitRule(
                    name="tokens",
                    scope=LimitScope.GROUP,
                    scope_id=seeded.group.id,
                    metric=LimitMetric.TOKENS,
                    window_seconds=3600,
                    limit_value=Decimal(250),
                )
            )
            await rules.commit()
        fake_upstream.set_json(INPUT_ANSWER)
        statuses = [
            (await client.post("/v1/decisions", json=input_body(), headers=seeded.auth)).status_code
            for _ in range(3)
        ]
        assert statuses[0] == 200
        assert statuses[-1] == 429
        assert len(fake_upstream.bodies) < 3


# -- redaction ------------------------------------------------------------------


class TestRedaction:
    async def test_input_text_is_substituted_before_it_leaves(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        from test_ocr_surface import install_detector

        install_detector(app, [("IBAN_CODE", IBAN)])
        fake_upstream.set_json(INPUT_ANSWER)
        body = input_body()
        body["input"][0]["content"][0]["text"] = f"Refund to {IBAN} please."
        response = await client.post("/v1/decisions", json=body, headers=seeded.auth)

        assert response.status_code == 200
        sent = fake_upstream.last_body["input"][0]["content"][0]["text"]
        assert IBAN not in sent
        assert "<IBAN_CODE_" in sent

    async def test_every_string_leaf_of_a_state_object_is_substituted_and_keys_are_not(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        from test_ocr_surface import install_detector

        install_detector(app, [("IBAN_CODE", IBAN)])
        fake_upstream.set_json(STATE_ANSWER)
        body = state_body()
        body["state"] = {
            "ticket": f"Refund to {IBAN}",
            "history": [{"note": f"earlier {IBAN}"}, "plain"],
            "count": 3,
        }
        response = await client.post("/v1/systemone", json=body, headers=seeded.auth)

        assert response.status_code == 200
        sent = fake_upstream.last_body["state"]
        assert IBAN not in str(sent)
        assert set(sent) == {"ticket", "history", "count"}
        assert sent["count"] == 3
        assert sent["history"][1] == "plain"
        assert "<IBAN_CODE_" in sent["ticket"]

    async def test_a_blocking_policy_refuses_and_records_without_calling(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        import httpx as _httpx
        import orjson
        from gateway.config import EntityMode, RedactionPolicy, RedactionSettings
        from gateway.redaction.http import HttpDetectionRedactor
        from pydantic import SecretStr

        def handler(request: _httpx.Request) -> _httpx.Response:
            texts = orjson.loads(request.content)["texts"]
            findings = [
                {
                    "index": i,
                    "spans": [
                        {
                            "start": t.find(IBAN),
                            "end": t.find(IBAN) + len(IBAN),
                            "entity_type": "IBAN_CODE",
                            "score": 0.99,
                        }
                    ]
                    if IBAN in t
                    else [],
                }
                for i, t in enumerate(texts)
            ]
            return _httpx.Response(200, json={"findings": findings, "engine": "fake"})

        app.state.redaction = None
        app.state.redactor = HttpDetectionRedactor(
            RedactionSettings(
                engine="http",
                endpoint="http://detector:8080",
                placeholder_key=SecretStr("decisions-test-key"),
                policy=RedactionPolicy(default_mode=EntityMode.BLOCK),
            ),
            client=_httpx.AsyncClient(transport=_httpx.MockTransport(handler)),
        )
        body = state_body()
        body["state"] = f"pay {IBAN}"
        response = await client.post("/v1/systemone", json=body, headers=seeded.auth)

        assert response.status_code == 403
        assert fake_upstream.bodies == []
        record = await latest_record(session)
        assert record.status is UsageStatus.BLOCKED
        assert record.api_surface is ApiSurface.DECISIONS

    def test_strings_and_rebuild_agree_on_order_and_skip_keys_and_empties(self) -> None:
        value = {"a": "one", "b": ["", "two", {"c": "three", "d": 4}], "e": None, "f": ""}
        assert list(_strings(value)) == ["one", "two", "three"]
        rebuilt = _rebuild(value, iter(["1", "2", "3"]))
        assert rebuilt == {"a": "1", "b": ["", "2", {"c": "3", "d": 4}], "e": None, "f": ""}


# -- aliases --------------------------------------------------------------------


class TestAlias:
    async def test_an_alias_of_a_decision_model_serves_and_records_both_names(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        alias = ModelDef(
            name="triage",
            provider_id=None,
            upstream_model=None,
            is_alias=True,
            kind=ModelKind.DECISION,
        )
        session.add(alias)
        await session.flush()
        session.add(ModelRouteTarget(alias_id=alias.id, target_model_id=decider.id, position=0))
        session.add(GroupModelAccess(group_id=seeded.group.id, model_id=alias.id))
        await session.commit()
        fake_upstream.set_json(INPUT_ANSWER)

        response = await client.post(
            "/v1/decisions", json=input_body("triage"), headers=seeded.auth
        )

        assert response.status_code == 200, response.text
        assert response.json()["model"] == "triage"
        assert fake_upstream.last_body["model"] == "laya"
        record = await latest_record(session)
        assert record.model_name == "decider"


# -- the catalogue and the console's API ----------------------------------------


class TestCatalogue:
    @pytest.fixture
    async def admin(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> httpx.AsyncClient:
        as_user(app, await make_admin(session_factory, seeded))
        return client

    async def test_a_decision_model_is_created_on_a_provider_that_supports_it(
        self, admin: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        await make_cortecs(session, seeded)
        response = await admin.post(
            "/api/admin/models",
            json={
                "name": f"d-{uuid.uuid4().hex[:6]}",
                "upstream_model": "laya",
                "provider_id": str(seeded.provider.id),
                "kind": "decision",
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["kind"] == "decision"

    async def test_creating_one_on_a_provider_without_support_says_so(
        self, admin: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        response = await admin.post(
            "/api/admin/models",
            json={
                "name": "d-generic",
                "upstream_model": "laya",
                "provider_id": str(seeded.provider.id),
                "kind": "decision",
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "provider_no_decision_support"

    async def test_changing_a_chat_model_to_decision_on_such_a_provider_is_refused(
        self, admin: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        response = await admin.patch(
            f"/api/admin/models/{seeded.model.id}", json={"kind": "decision"}
        )
        assert response.status_code == 400

    async def test_the_provider_types_say_which_support_decisions(
        self, admin: httpx.AsyncClient
    ) -> None:
        response = await admin.get("/api/admin/provider-plugins")
        assert response.status_code == 200, response.text
        support = {entry["name"]: entry["supports_decisions"] for entry in response.json()}
        assert support["cortecs"] is True
        assert support["generic"] is False

    def test_the_kind_is_detected_from_a_catalogue_tag(self) -> None:
        from gateway.pricing import _kind_of

        kind = _kind_of({"id": "laya", "tags": ["Decision"], "output_modalities": ["text"]})
        assert kind is ModelKind.DECISION


class TestSchema:
    def test_format_is_decided_by_presence_not_truthiness(self) -> None:
        assert DecisionsRequest.model_validate({"model": "m", "state": ""}).format() == "state"
        assert DecisionsRequest.model_validate({"model": "m", "input": ""}).format() == "input"
        assert DecisionsRequest.model_validate({"model": "m", "input": None}).format() is None
        both = DecisionsRequest.model_validate({"model": "m", "input": "a", "state": "b"})
        assert both.format() is None
