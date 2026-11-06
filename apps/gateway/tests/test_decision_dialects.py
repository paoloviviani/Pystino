"""Decision dialects: ``decisions`` and ``systemone``, translated both ways.

The unit tests pin ``gateway.translate.decisions`` (every question type, the
edge cases, what has no counterpart); the router tests pin the four
combinations of caller form and provider dialect, and that metering reads each
provider's own usage. Answers are the recorded shapes of the two providers.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
from conftest import FakeUpstream, Seeded
from gateway.errors import BadRequestError
from gateway.models import BillingMode, CostSource, ModelDef, Provider
from gateway.plugins.registry import resolve
from gateway.translate.decisions import translate_request, translate_response
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from test_decisions import (
    INPUT_ANSWER,
    INPUT_TEXT,
    STATE_ANSWER,
    STATE_QUESTIONS,
    add_decision_model,
    input_body,
    latest_record,
    make_cortecs,
    state_body,
    upstream_path,
)

D, S = "decisions", "systemone"

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


def to_s1(body: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    return translate_request(body, source=D, target=S)


def to_dec(body: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    return translate_request(body, source=S, target=D)


class TestDialectsAreDeclared:
    def test_each_plugin_declares_its_native_dialect_as_data(self) -> None:
        cortecs, melious = resolve("cortecs").decisions, resolve("melious").decisions  # type: ignore[attr-defined]
        assert (cortecs.dialect, cortecs.path) == (D, "/decisions")
        assert (melious.dialect, melious.path) == (S, "/systemone")

    def test_matching_dialects_are_forwarded_untouched(self) -> None:
        body = {"model": "m", "input": "x", "questions": QUESTIONS, "preference": "cost"}
        wire, plan = translate_request(body, source=D, target=D)
        assert wire is body and plan is None
        answer = {"answers": []}
        assert translate_response(answer, plan) is answer


class TestDecisionsToSystemOne:
    def test_questions_become_system_one_questions(self) -> None:
        body = {"model": "m", "input": "hello", "questions": QUESTIONS}
        wire, _ = to_s1(body)
        assert wire == {
            "model": "m",
            "state": "hello",
            "questions": {
                "route": {
                    "type": "choice",
                    "instructions": "Which team?",
                    "criteria": {"facilities": "building", "billing": "billing"},
                },
                "urgency": {
                    "type": "score",
                    "instructions": "How urgent?",
                    "criteria": ["low", "medium", "high"],
                },
                "inspect": {"type": "noul", "instructions": "Needs inspection?"},
            },
        }

    def test_messages_become_one_text(self) -> None:
        q = [{"type": "predicate", "name": "p", "instructions": "x?"}]
        one = [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}]
        two = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        assert to_s1({"model": "m", "input": one, "questions": q})[0]["state"] == "hi"
        assert to_s1({"model": "m", "input": two, "questions": q})[0]["state"] == (
            "user: a\nassistant: b"
        )

    def test_an_answer_comes_back_in_the_decisions_shape(self) -> None:
        _, plan = to_s1({"model": "m", "input": "x", "questions": QUESTIONS})
        out = translate_response(copy.deepcopy(SYSTEM_ONE_ANSWER), plan)
        assert out["usage"] == {"input_tokens": 124, "output_tokens": 0}
        route, urgency, inspect = out["answers"]
        assert route == {
            "type": "choice",
            "name": "route",
            "choice": "facilities",
            "probabilities": [
                {"value": "facilities", "probability": 0.75},
                {"value": "billing", "probability": 0.25},
            ],
            "confidence": 0.5,
        }
        assert urgency["score"] == 0.9 and urgency["confidence"] == 0.6
        assert urgency["probabilities"][1] == {"value": 1, "label": "medium", "probability": 0.7}
        assert inspect == {"type": "predicate", "name": "inspect", "probability": 0.0915}

    def test_non_string_choice_values_come_back_as_sent(self) -> None:
        q = [{"type": "choice", "name": "n", "choices": [{"value": 1}, {"value": 2}]}]
        wire, plan = to_s1({"model": "m", "input": "x", "questions": q})
        assert wire["questions"]["n"]["criteria"] == {"1": "1", "2": "2"}
        answer = {"answers": {"n": {"type": "choice", "choice": "2", "probabilities": {"2": 1.0}}}}
        out = translate_response(answer, plan)["answers"][0]
        assert out["choice"] == 2 and out["probabilities"] == [{"value": 2, "probability": 1.0}]

    def test_unnamed_and_unsafe_and_repeated_names_get_keys_and_come_back_as_sent(self) -> None:
        q = [
            {"type": "predicate", "instructions": "a?"},
            {"type": "predicate", "name": "has space", "instructions": "b?"},
            {"type": "predicate", "name": "dup", "instructions": "c?"},
            {"type": "predicate", "name": "dup", "instructions": "d?"},
        ]
        wire, plan = to_s1({"model": "m", "input": "x", "questions": q})
        assert list(wire["questions"]) == ["q0", "q1", "dup", "q3"]
        answer = {"answers": {k: {"type": "noul", "noul": 0.5} for k in wire["questions"]}}
        names = [a["name"] for a in translate_response(answer, plan)["answers"]]
        assert names == [None, "has space", "dup", "dup"]

    def test_a_refusal_keeps_its_fields_and_gets_the_name(self) -> None:
        q = [{"type": "predicate", "name": "p", "instructions": "x?"}]
        _, plan = to_s1({"model": "m", "input": "x", "questions": q})
        answer = {"answers": {"p": {"type": "refusal", "refusal": "no"}}}
        assert translate_response(answer, plan)["answers"] == [
            {"type": "refusal", "refusal": "no", "name": "p"}
        ]

    def test_preference_has_no_counterpart(self) -> None:
        q = [{"type": "predicate", "name": "p", "instructions": "x?"}]
        with pytest.raises(BadRequestError, match="'preference'"):
            to_s1({"model": "m", "input": "x", "questions": q, "preference": "cost"})
        wire, _ = to_s1({"model": "m", "input": "x", "questions": q, "preference": None})
        assert "preference" not in wire

    def test_a_question_with_no_counterpart_is_refused_by_type(self) -> None:
        q = [{"type": "box", "name": "b", "instructions": "?"}]
        with pytest.raises(BadRequestError, match="'box'"):
            to_s1({"model": "m", "input": "x", "questions": q})

    @pytest.mark.parametrize(
        "question",
        [
            {"type": "choice", "name": "c", "choices": [{"value": "only"}]},
            {"type": "score", "name": "s", "levels": [{"label": "only"}]},
            "not an object",
        ],
    )
    def test_malformed_questions_are_a_400(self, question: Any) -> None:
        with pytest.raises(BadRequestError):
            to_s1({"model": "m", "input": "x", "questions": [question]})


class TestSystemOneToDecisions:
    def test_every_type_maps(self) -> None:
        body = {
            "model": "m",
            "state": "text",
            "questions": {
                "inspect": {"type": "noul", "instructions": "Needs inspection?"},
                "team": {
                    "type": "choice",
                    "instructions": "Which team?",
                    "criteria": {"facilities": "equipment", "billing": "billing"},
                },
                "urg": {"type": "score", "criteria": ["low", "medium", "high"]},
            },
        }
        wire, _ = to_dec(body)
        assert wire == {
            "model": "m",
            "input": "text",
            "questions": [
                {"type": "predicate", "name": "inspect", "instructions": "Needs inspection?"},
                {
                    "type": "choice",
                    "name": "team",
                    "instructions": "Which team?",
                    "choices": [
                        {"value": "facilities", "description": "equipment"},
                        {"value": "billing"},
                    ],
                },
                {
                    "type": "score",
                    "name": "urg",
                    "levels": [{"label": "low"}, {"label": "medium"}, {"label": "high"}],
                },
            ],
        }

    def test_choice_criteria_may_be_a_list_of_values(self) -> None:
        wire, _ = to_dec(
            {
                "model": "m",
                "state": "x",
                "questions": {"c": {"type": "choice", "criteria": ["a", "b"]}},
            }
        )
        assert wire["questions"][0]["choices"] == [{"value": "a"}, {"value": "b"}]

    def test_object_and_array_state_become_json_text(self) -> None:
        q = {"p": {"type": "noul"}}
        state = {"ticket": "r\u00e9sum\u00e9", "n": [1, 2]}
        wire, _ = to_dec({"model": "m", "state": state, "questions": q})
        assert json.loads(wire["input"]) == state and "r\u00e9sum\u00e9" in wire["input"]
        assert to_dec({"model": "m", "state": [1, "a"], "questions": q})[0]["input"] == '[1, "a"]'
        assert to_dec({"model": "m", "state": "", "questions": q})[0]["input"] == ""

    def test_images_pass_through(self) -> None:
        q = {"p": {"type": "noul"}}
        wire, _ = to_dec({"model": "m", "state": "x", "questions": q, "images": ["u"]})
        assert wire["images"] == ["u"]

    def test_the_answer_comes_back_keyed_by_question_id(self) -> None:
        body = {"model": "m", "state": "x", "questions": copy.deepcopy(STATE_QUESTIONS)}
        _, plan = to_dec(body)
        out = translate_response(copy.deepcopy(INPUT_ANSWER) | {}, plan)
        # INPUT_ANSWER is named route/urgency/inspect: only `inspect` matches by name,
        # the rest fall back to position, which is the order Cortecs answers in.
        assert set(out["answers"]) == {"inspect", "team", "urg"}
        assert out["usage"] == INPUT_ANSWER["usage"]

    def test_answers_translate_back_by_name(self) -> None:
        body = {"model": "m", "state": "x", "questions": copy.deepcopy(STATE_QUESTIONS)}
        _, plan = to_dec(body)
        answer = {
            "model": "laya",
            "answers": [
                {"type": "predicate", "name": "inspect", "probability": 0.03},
                {
                    "type": "choice",
                    "name": "team",
                    "choice": "facilities",
                    "probabilities": [
                        {"value": "facilities", "probability": 0.9},
                        {"value": "billing", "probability": 0.1},
                    ],
                    "confidence": 0.8,
                },
                {
                    "type": "score",
                    "name": "urg",
                    "score": 1.3,
                    "probabilities": [
                        {"value": 0, "label": "low", "probability": 0.1},
                        {"value": 1, "label": "medium", "probability": 0.6},
                        {"value": 2, "label": "high", "probability": 0.3},
                    ],
                    "confidence": 0.3,
                },
            ],
            "usage": {"input_tokens": 5},
        }
        assert translate_response(answer, plan) == {
            "model": "laya",
            "answers": {
                "inspect": {"type": "noul", "noul": 0.03},
                "team": {
                    "type": "choice",
                    "choice": "facilities",
                    "probabilities": {"facilities": 0.9, "billing": 0.1},
                    "confidence": 0.8,
                },
                "urg": {
                    "type": "score",
                    "score": 1.3,
                    "probabilities": {"0": 0.1, "1": 0.6, "2": 0.3},
                    "confidence": 0.3,
                    "legend": {"0": "low", "1": "medium", "2": "high"},
                },
            },
            "usage": {"input_tokens": 5},
        }

    def test_a_refusal_loses_its_name(self) -> None:
        _, plan = to_dec({"model": "m", "state": "x", "questions": {"p": {"type": "noul"}}})
        answer = {"answers": [{"type": "refusal", "name": "p", "refusal": "no"}]}
        assert translate_response(answer, plan)["answers"] == {
            "p": {"type": "refusal", "refusal": "no"}
        }

    def test_a_type_with_no_counterpart_is_refused_by_type(self) -> None:
        q = {"b": {"type": "box", "instructions": "?"}}
        with pytest.raises(BadRequestError, match="'box'"):
            to_dec({"model": "m", "state": "x", "questions": q})

    @pytest.mark.parametrize(
        "question",
        [
            {"type": "choice", "criteria": {"only": "one"}},
            {"type": "score", "criteria": {"a": "x", "b": "y"}},
            {"type": "score", "criteria": ["only"]},
            {"type": "noul"} and "not an object",
        ],
    )
    def test_malformed_questions_are_a_400(self, question: Any) -> None:
        with pytest.raises(BadRequestError):
            to_dec({"model": "m", "state": "x", "questions": {"q": question}})


# -- the four combinations -------------------------------------------------------

DECISIONS_ANSWER_FOR_STATE: dict[str, Any] = {
    "model": "laya",
    "answers": [
        {"type": "predicate", "name": "inspect", "probability": 0.0323},
        {
            "type": "choice",
            "name": "team",
            "choice": "facilities",
            "probabilities": [
                {"value": "facilities", "probability": 0.9905},
                {"value": "billing", "probability": 0.0095},
            ],
            "confidence": 0.981,
        },
        {
            "type": "score",
            "name": "urg",
            "score": 1.3,
            "probabilities": [
                {"value": 0, "label": "low", "probability": 0.0679},
                {"value": 1, "label": "medium", "probability": 0.5594},
                {"value": 2, "label": "high", "probability": 0.3727},
            ],
            "confidence": 0.34,
        },
    ],
    "usage": {"input_tokens": 124, "output_tokens": 0, "cost": 6},
}


@pytest.fixture
async def cortecs_decider(session: AsyncSession, seeded: Seeded) -> ModelDef:
    await make_cortecs(session, seeded)
    return await add_decision_model(session, seeded)


@pytest.fixture
async def melious_decider(session: AsyncSession, seeded: Seeded) -> ModelDef:
    await session.execute(
        update(Provider).where(Provider.id == seeded.provider.id).values(plugin="melious")
    )
    await session.commit()
    return await add_decision_model(session, seeded, upstream="clef-flash", input_per_mtok="0.03")


class TestTheFourCombinations:
    async def test_decisions_to_cortecs_is_a_passthrough(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        cortecs_decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        fake_upstream.set_json(INPUT_ANSWER)
        response = await client.post(
            "/v1/decisions", json=input_body(preference="cost"), headers=seeded.auth
        )
        assert response.status_code == 200, response.text
        assert upstream_path(fake_upstream).endswith("/decisions")
        assert fake_upstream.last_body["preference"] == "cost"
        assert response.json()["answers"] == INPUT_ANSWER["answers"]

    async def test_decisions_to_melious_is_translated(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        melious_decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        fake_upstream.set_json(copy.deepcopy(SYSTEM_ONE_ANSWER))
        response = await client.post("/v1/decisions", json=input_body(), headers=seeded.auth)
        assert response.status_code == 200, response.text
        assert upstream_path(fake_upstream).endswith("/systemone")
        assert isinstance(fake_upstream.last_body["questions"], dict)
        assert fake_upstream.last_body["state"] == INPUT_TEXT
        body = response.json()
        assert body["model"] == "decider"
        assert [a["type"] for a in body["answers"]] == ["choice", "score", "predicate"]
        assert (await latest_record(session)).prompt_tokens == 124

    async def test_systemone_to_melious_is_a_passthrough(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        melious_decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        fake_upstream.set_json(copy.deepcopy(STATE_ANSWER))
        response = await client.post("/v1/systemone", json=state_body(), headers=seeded.auth)
        assert response.status_code == 200, response.text
        assert upstream_path(fake_upstream).endswith("/systemone")
        assert fake_upstream.last_body["questions"] == STATE_QUESTIONS
        assert response.json()["answers"] == STATE_ANSWER["answers"]

    async def test_systemone_to_cortecs_is_translated(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        cortecs_decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        fake_upstream.set_json(copy.deepcopy(DECISIONS_ANSWER_FOR_STATE))
        response = await client.post("/v1/systemone", json=state_body(), headers=seeded.auth)
        assert response.status_code == 200, response.text
        assert upstream_path(fake_upstream).endswith("/decisions")
        sent = fake_upstream.last_body
        assert isinstance(sent["questions"], list) and "state" not in sent
        assert sent["input"] == state_body()["state"]
        body = response.json()
        assert body["model"] == "decider"
        assert body["answers"]["inspect"] == {"type": "noul", "noul": 0.0323}
        assert body["answers"]["team"]["probabilities"] == {"facilities": 0.9905, "billing": 0.0095}
        assert body["answers"]["urg"]["legend"] == {"0": "low", "1": "medium", "2": "high"}

    async def test_the_state_form_on_the_decisions_route_is_translated_too(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        cortecs_decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        fake_upstream.set_json(copy.deepcopy(DECISIONS_ANSWER_FOR_STATE))
        response = await client.post("/v1/decisions", json=state_body(), headers=seeded.auth)
        assert response.status_code == 200, response.text
        assert set(response.json()["answers"]) == {"inspect", "team", "urg"}

    async def test_a_question_with_no_counterpart_is_a_400_and_nothing_is_called(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        cortecs_decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        body = state_body()
        body["questions"] = {"b": {"type": "box", "instructions": "?"}}
        response = await client.post("/v1/systemone", json=body, headers=seeded.auth)
        assert response.status_code == 400
        assert "'box'" in response.json()["error"]["message"]
        assert not fake_upstream.urls

    async def test_preference_to_melious_is_a_400_and_nothing_is_called(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        melious_decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        response = await client.post(
            "/v1/decisions", json=input_body(preference="speed"), headers=seeded.auth
        )
        assert response.status_code == 400
        assert "'preference'" in response.json()["error"]["message"]
        assert not fake_upstream.urls


class TestMeteringPerProvider:
    async def test_a_translated_request_is_metered_from_the_providers_own_usage(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await make_cortecs(session, seeded, mode=BillingMode.PROVIDER_REPORTED)
        await add_decision_model(session, seeded)
        fake_upstream.set_json(copy.deepcopy(DECISIONS_ANSWER_FOR_STATE))
        response = await client.post("/v1/systemone", json=state_body(), headers=seeded.auth)
        assert response.status_code == 200, response.text
        record = await latest_record(session)
        # Cortecs reports micro-EUR in usage.cost; the caller's dialect does not matter.
        assert record.prompt_tokens == 124 and record.completion_tokens == 0
        assert record.cost == Decimal("0.000006")
        assert record.cost_source is CostSource.PROVIDER_REPORTED
        # The caller's answer keeps the provider's usage block.
        assert response.json()["usage"]["cost"] == 6

    async def test_melious_is_billed_from_the_table_on_either_form(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        melious_decider: ModelDef,
        fake_upstream: FakeUpstream,
    ) -> None:
        for route, body in (("/v1/decisions", input_body()), ("/v1/systemone", state_body())):
            fake_upstream.set_json(copy.deepcopy(SYSTEM_ONE_ANSWER))
            assert (await client.post(route, json=body, headers=seeded.auth)).status_code == 200
            record = await latest_record(session)
            assert record.prompt_tokens == 124
            assert record.cost_source is not CostSource.PROVIDER_REPORTED
            assert record.cost == Decimal("0.03") * 124 / 1_000_000


class TestModelsAdvertiseTheForms:
    async def test_a_decision_model_lists_both_forms_and_others_none(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        cortecs_decider: ModelDef,
    ) -> None:
        cards = {
            c["id"]: c for c in (await client.get("/v1/models", headers=seeded.auth)).json()["data"]
        }
        assert cards["decider"]["decision_forms"] == ["decisions", "systemone"]
        assert cards[seeded.model.name]["decision_forms"] == []
