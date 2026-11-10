"""NER engines per policy, and the groups of items (ADR 0113, section 0c).

Against a fake of the redaction service's contract: ``/healthz`` lists ``ner_engines``,
``/detect`` takes ``ner_engine`` and ``custom_labels`` and answers 409
``engine_unavailable`` for an engine it has not got. The real service is built on its own
branch; this is the gateway's half.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from conftest import FakeUpstream, Seeded
from gateway.config import (
    Contributor,
    CustomEntity,
    EffectivePolicy,
    EntityMode,
    EntityPolicy,
    KeywordList,
    NerAsk,
    PolicyDefinition,
    RedactionPolicy,
)
from gateway.errors import ServiceUnavailableError
from gateway.privacy.finds import find_in
from gateway.redaction.http import EngineUnavailableError, HttpDetectionRedactor
from llmp_shared import EntitySpan
from privacy_helpers import add_pipeline, add_policy, rule
from test_admin import as_user, make_admin
from test_embeddings import add_embedding_model, embedding_body
from test_redaction_http import settings

M = EntityMode
E = EntityPolicy

HEALTHZ = {
    "status": "ok",
    "engine": "presidio",
    "pattern_entities": ["EMAIL_ADDRESS", "IBAN_CODE", "CREDIT_CARD"],
    "ner_engines": [
        {
            "name": "spacy",
            "available": True,
            "default": True,
            "entities": ["PERSON", "LOCATION", "ORGANIZATION"],
            "custom_labels": False,
            "ms_per_token": 0.4,
            "languages": ["en", "it"],
        },
        {
            "name": "gliner",
            "available": True,
            "default": False,
            "entities": ["PERSON", "LOCATION", "ORGANIZATION", "DATE"],
            "custom_labels": True,
            "ms_per_token": 6.0,
            "languages": ["en", "it"],
        },
    ],
}


class FakeService:
    """The redaction service as the gateway sees it: healthz and detect, per engine."""

    def __init__(self) -> None:
        self.health = json.loads(json.dumps(HEALTHZ))
        self.requests: list[dict[str, Any]] = []
        #: engine -> {needle: entity type}. ``pattern`` is what the recognisers find.
        self.finds: dict[str, dict[str, str]] = {
            "pattern": {"IT60X0542811101000000123456": "IBAN_CODE"},
            "spacy": {"Mario Rossi": "PERSON"},
            "gliner": {"Mario Rossi": "PERSON", "Falcon": "CODENAME"},
        }
        self.down: set[str] = set()

    def set_available(self, name: str, available: bool) -> None:
        for engine in self.health["ner_engines"]:
            if engine["name"] == name:
                engine["available"] = available

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json=self.health)
        payload = json.loads(request.content)
        self.requests.append(payload)
        default = next(e["name"] for e in self.health["ner_engines"] if e["default"])
        engine = payload.get("ner_engine") or default
        state = next((e for e in self.health["ner_engines"] if e["name"] == engine), None)
        if state is None or not state["available"] or engine in self.down:
            return httpx.Response(409, json={"detail": {"code": "engine_unavailable"}})
        wanted = payload.get("entity_types")
        ner_off = payload.get("presidio_ner") is False
        patterns_off = payload.get("presidio_pattern_matching") is False
        labels = {c["entity"] for c in payload.get("custom_labels") or []}
        unsupported = sorted(
            {
                e
                for e in (wanted or [])
                if (ner_off and e in ("PERSON", "LOCATION", "ORGANIZATION", "AGE"))
                or (patterns_off and e in self.health["pattern_entities"])
            }
            | (labels if ner_off else set())
        )
        findings = []
        for index, text in enumerate(payload["texts"]):
            spans = []
            for source, needles in (
                (("pattern"), self.finds["pattern"]),
                (engine, self.finds[engine]),
            ):
                for needle, entity in needles.items():
                    if wanted is not None and entity not in wanted:
                        continue
                    if entity in unsupported or (ner_off and source != "pattern"):
                        continue
                    if patterns_off and source == "pattern":
                        continue
                    if source == "gliner" and entity == "CODENAME" and entity not in labels:
                        continue
                    start = text.find(needle)
                    while start != -1:
                        spans.append(
                            {
                                "start": start,
                                "end": start + len(needle),
                                "entity_type": entity,
                                "score": 1.0 if source == "pattern" else 0.8,
                                "engine": source,
                            }
                        )
                        start = text.find(needle, start + 1)
            findings.append({"index": index, "spans": spans})
        return httpx.Response(
            200,
            json={"findings": findings, "engine": "fake", "unsupported_types": unsupported},
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def redactor(self, **overrides: Any) -> HttpDetectionRedactor:
        return HttpDetectionRedactor(settings(**overrides), client=self.client())


TEXT = "Mario Rossi, IT60X0542811101000000123456, Falcon."


def contributor(name: str, policy: RedactionPolicy) -> Contributor:
    return Contributor(id=uuid.uuid4(), name=name, owner="admin", policy=policy)


def effective(*named: tuple[str, RedactionPolicy]) -> EffectivePolicy:
    return EffectivePolicy(
        policy=RedactionPolicy.combine([p for _, p in named]),
        contributors=tuple(contributor(n, p) for n, p in named),
    )


class TestAsks:
    def test_a_single_policy_asks_for_its_own_engine_and_types(self) -> None:
        policy = RedactionPolicy(
            engine="gliner",
            formats={"IBAN_CODE": E(mode=M.REDACT)},
            entities={"PERSON": E(mode=M.REDACT)},
            custom_entities=[CustomEntity(name="CODENAME", label="project code name")],
        )
        assert policy.ner_asks() == [
            NerAsk(
                "gliner", ("CODENAME", "IBAN_CODE", "PERSON"), (("CODENAME", "project code name"),)
            )
        ]

    def test_policies_of_different_engines_ask_once_per_engine_and_formats_with_each(self) -> None:
        a = RedactionPolicy(
            engine="spacy",
            formats={"IBAN_CODE": E(mode=M.REDACT)},
            entities={"PERSON": E(mode=M.REDACT)},
        )
        b = RedactionPolicy(
            engine="gliner",
            formats={"EMAIL_ADDRESS": E(mode=M.REDACT)},
            custom_entities=[CustomEntity(name="CODENAME", label="project code name")],
        )
        c = RedactionPolicy(engine="spacy", entities={"LOCATION": E(mode=M.ANONYMISE)})
        asks = effective(("A", a), ("B", b), ("C", c)).asks()
        assert asks == [
            NerAsk(
                "gliner",
                ("CODENAME", "EMAIL_ADDRESS", "IBAN_CODE"),
                (("CODENAME", "project code name"),),
            ),
            # Formats go with every call: which engine finds one can depend on the model.
            NerAsk("spacy", ("EMAIL_ADDRESS", "IBAN_CODE", "LOCATION", "PERSON"), ()),
        ]

    def test_a_policy_with_only_patterns_makes_one_ask_with_the_default_engine(self) -> None:
        a = RedactionPolicy(formats={"IBAN_CODE": E(mode=M.REDACT)})
        b = RedactionPolicy(engine="gliner", formats={"EMAIL_ADDRESS": E(mode=M.REDACT)})
        assert effective(("A", a), ("B", b)).asks() == [
            NerAsk(None, ("EMAIL_ADDRESS", "IBAN_CODE"), ())
        ]

    def test_detect_only_items_are_not_asked_for_by_redaction_but_are_by_a_condition(self) -> None:
        policy = RedactionPolicy(entities={"PERSON": E(mode=M.DETECT_ONLY)})
        assert policy.ner_asks()[0].types == ()
        assert policy.ner_asks(acting=False)[0].types == ("PERSON",)

    def test_the_default_mode_asks_for_everything_the_engine_knows(self) -> None:
        a = RedactionPolicy(engine="gliner", default_mode=M.REDACT)
        assert effective(("A", a)).asks() == [NerAsk("gliner", None, ())]


class TestDetectAcrossEngines:
    async def test_one_call_per_engine_with_engine_and_labels_and_spans_merged(self) -> None:
        service = FakeService()
        redactor = service.redactor()
        a = RedactionPolicy(engine="spacy", entities={"PERSON": E(mode=M.REDACT)})
        b = RedactionPolicy(
            engine="gliner",
            entities={"PERSON": E(mode=M.BLOCK)},
            custom_entities=[CustomEntity(name="CODENAME", label="project code name")],
        )
        eff = effective(("A", a), ("B", b))
        spans = (await redactor.detect([TEXT], policy=eff.policy, asks=eff.asks()))[0]
        assert [(r["ner_engine"], r["entity_types"]) for r in service.requests] == [
            ("gliner", ["CODENAME", "PERSON"]),
            ("spacy", ["PERSON"]),
        ]
        assert service.requests[0]["custom_labels"] == [
            {"entity": "CODENAME", "label": "project code name"}
        ]
        assert service.requests[1]["custom_labels"] is None
        # Mario Rossi came from both engines: one span, and the folded policy's strictest mode.
        assert sorted((s.entity_type, s.engine) for s in spans) == [
            ("CODENAME", "gliner"),
            ("PERSON", "gliner"),
        ] or sorted(s.entity_type for s in spans) == ["CODENAME", "PERSON"]
        assert len([s for s in spans if s.entity_type == "PERSON"]) == 1
        assert eff.policy.mode_for("PERSON") is M.BLOCK

    async def test_a_redaction_request_uses_the_strictest_mode_of_the_span(self) -> None:
        from gateway.errors import ContentBlockedError

        service = FakeService()
        redactor = service.redactor()
        a = RedactionPolicy(engine="spacy", entities={"PERSON": E(mode=M.REDACT)})
        b = RedactionPolicy(engine="gliner", entities={"PERSON": E(mode=M.BLOCK)})
        with pytest.raises(ContentBlockedError):
            await redactor.redact_request(
                [{"role": "user", "content": TEXT}], policy=effective(("A", a), ("B", b))
            )

    async def test_the_cache_keeps_engines_apart(self) -> None:
        service = FakeService()
        redactor = service.redactor()
        spacy = RedactionPolicy(engine="spacy", entities={"PERSON": E(mode=M.REDACT)})
        gliner = RedactionPolicy(engine="gliner", entities={"PERSON": E(mode=M.REDACT)})
        await redactor.detect([TEXT], policy=spacy)
        await redactor.detect([TEXT], policy=gliner)
        await redactor.detect([TEXT], policy=spacy)
        assert [r["ner_engine"] for r in service.requests] == ["spacy", "gliner"]


class TestAnUnavailableEngineFailsClosed:
    async def test_a_409_is_an_unavailable_engine_not_a_clean_text(self) -> None:
        service = FakeService()
        service.set_available("gliner", False)
        redactor = service.redactor()
        policy = RedactionPolicy(engine="gliner", entities={"PERSON": E(mode=M.REDACT)})
        with pytest.raises(EngineUnavailableError) as raised:
            await redactor.detect([TEXT], policy=policy)
        assert raised.value.engine == "gliner"

    async def test_a_switched_off_engine_a_policy_chose_fails_closed(self) -> None:
        service = FakeService()
        redactor = service.redactor(gliner_enabled=False)
        policy = RedactionPolicy(engine="gliner", entities={"PERSON": E(mode=M.REDACT)})
        with pytest.raises(EngineUnavailableError) as raised:
            await redactor.detect([TEXT], policy=policy)
        assert raised.value.engine == "gliner" and "switched off" in str(raised.value)
        # And the request is the 503 that names the policy, whatever fail_open says.
        failing = service.redactor(gliner_enabled=False, fail_open=True)
        with pytest.raises(ServiceUnavailableError) as again:
            await failing.redact_request(
                [{"role": "user", "content": TEXT}], policy=effective(("Names", policy))
            )
        assert "'Names'" in again.value.message

    async def test_a_switched_off_default_engine_refuses_what_needs_it_only(self) -> None:
        service = FakeService()
        redactor = service.redactor(spacy_enabled=False, gliner_enabled=True)
        needs = RedactionPolicy(entities={"PERSON": E(mode=M.REDACT)})
        with pytest.raises(EngineUnavailableError):
            await redactor.detect([TEXT], policy=needs)
        # A policy of formats alone does not need the engine, so it still works.
        formats = RedactionPolicy(formats={"IBAN_CODE": E(mode=M.REDACT)})
        spans = await redactor.detect([TEXT], policy=formats)
        assert [s.entity_type for s in spans[0]] == ["IBAN_CODE"]

    async def test_a_legacy_type_no_family_lists_does_not_turn_a_switch_into_an_outage(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        service = FakeService()
        redactor = service.redactor(spacy_enabled=False, gliner_enabled=False, presidio_ner=False)
        policy = RedactionPolicy(
            formats={"AGE": E(mode=M.REDACT), "IBAN_CODE": E(mode=M.REDACT)},
        )
        with caplog.at_level("WARNING"):
            spans = await redactor.detect([TEXT], policy=policy)
        assert [s.entity_type for s in spans[0]] == ["IBAN_CODE"]
        assert "AGE" in caplog.text and "no family" in caplog.text
        # A type a switched-off family does list is still refused.
        with pytest.raises(EngineUnavailableError):
            await redactor.detect(
                [TEXT], policy=RedactionPolicy(entities={"PERSON": E(mode=M.REDACT)})
            )

    async def test_a_custom_entity_is_refused_not_ignored_when_ner_is_off(self) -> None:
        # Custom GLiNER names are in no /healthz list either: only the legacy AGE/ID/EMAIL
        # set may be ignored, so NER off must still refuse a policy that blocks CODENAME.
        service = FakeService()
        redactor = service.redactor(presidio_ner=False)
        policy = RedactionPolicy(
            engine="gliner",
            custom_entities=[
                CustomEntity(name="CODENAME", label="project code name", mode=M.BLOCK)
            ],
        )
        with pytest.raises(EngineUnavailableError):
            await redactor.detect([TEXT], policy=policy)

    async def test_a_type_of_a_switched_off_pattern_family_is_refused_not_ignored(self) -> None:
        service = FakeService()
        redactor = service.redactor(presidio_pattern_matching=False)
        policy = RedactionPolicy(formats={"IBAN_CODE": E(mode=M.REDACT)})
        with pytest.raises(EngineUnavailableError) as raised:
            await redactor.detect([TEXT], policy=policy)
        assert "switched off" in str(raised.value)

    async def test_the_request_is_a_503_naming_the_policy(self) -> None:
        service = FakeService()
        service.set_available("gliner", False)
        redactor = service.redactor(fail_open=True)  # fail_open is about outages, not this
        policy = RedactionPolicy(engine="gliner", entities={"PERSON": E(mode=M.REDACT)})
        with pytest.raises(ServiceUnavailableError) as raised:
            await redactor.redact_request(
                [{"role": "user", "content": TEXT}], policy=effective(("Names", policy))
            )
        assert "'Names'" in raised.value.message and "gliner" in raised.value.message
        assert TEXT not in raised.value.message
        assert raised.value.code == "redaction_engine_unavailable"

    async def test_a_condition_reports_it_and_ignores_fail_open(self) -> None:
        service = FakeService()
        service.set_available("gliner", False)
        redactor = service.redactor(fail_open=True)
        policy = RedactionPolicy(engine="gliner", entities={"PERSON": E(mode=M.DETECT_ONLY)})
        result = await find_in(policy, [TEXT], redactor=redactor, default_threshold=0.5)
        assert result.items == () and result.unavailable and "gliner" in result.unavailable

    async def test_an_outage_is_unavailable_for_a_condition_even_when_redaction_fails_open(
        self,
    ) -> None:
        class Down(FakeService):
            def handle(self, request: httpx.Request) -> httpx.Response:
                raise httpx.ConnectError("down")

        redactor = Down().redactor(fail_open=True)
        policy = RedactionPolicy(entities={"PERSON": E(mode=M.DETECT_ONLY)})
        result = await find_in(policy, [TEXT], redactor=redactor, default_threshold=0.5)
        assert result.unavailable and "could not be reached" in result.unavailable

    async def test_the_patterns_part_never_fails(self) -> None:
        service = FakeService()
        service.set_available("gliner", False)
        redactor = service.redactor()
        policy = RedactionPolicy(
            engine="gliner",
            keywords=[KeywordList(name="K", words="Falcon", mode=M.DETECT_ONLY)],
            patterns=[],
        )
        result = await find_in(policy, [TEXT], redactor=redactor, default_threshold=0.5)
        assert [i.type for i in result.items] == ["K"] and result.unavailable is None
        assert service.requests == []


class TestFinds:
    async def test_items_are_distinct_and_carry_group_engine_and_score(self) -> None:
        service = FakeService()
        policy = RedactionPolicy(
            engine="gliner",
            formats={"IBAN_CODE": E(mode=M.DETECT_ONLY)},
            entities={"PERSON": E(mode=M.DETECT_ONLY)},
            custom_entities=[
                CustomEntity(name="CODENAME", label="project code name", mode=M.DETECT_ONLY)
            ],
            keywords=[KeywordList(name="K", words="Falcon, Mario Rossi", mode=M.DETECT_ONLY)],
        )
        text = TEXT + " Mario Rossi again."
        result = await find_in(policy, [text], redactor=service.redactor(), default_threshold=0.5)
        found = sorted((i.group, i.type, i.engine, i.score is not None) for i in result.items)
        assert found == [
            ("entity", "CODENAME", "gliner", True),
            ("entity", "PERSON", "gliner", True),
            ("pattern", "IBAN_CODE", "pattern", False),
            ("pattern", "K", "pattern", False),
            ("pattern", "K", "pattern", False),
        ]
        # Mario Rossi twice is one item.
        assert len([i for i in result.items if i.type == "PERSON"]) == 1

    async def test_the_threshold_of_each_type_applies(self) -> None:
        service = FakeService()
        policy = RedactionPolicy(entities={"PERSON": E(mode=M.DETECT_ONLY, threshold=0.9)})
        result = await find_in(policy, [TEXT], redactor=service.redactor(), default_threshold=0.5)
        assert result.items == ()  # the fake scores 0.8

    async def test_off_is_not_counted_and_detect_only_is(self) -> None:
        service = FakeService()
        off = RedactionPolicy(entities={"PERSON": E(mode=M.OFF)})
        on = RedactionPolicy(entities={"PERSON": E(mode=M.DETECT_ONLY)})
        assert (
            await find_in(off, [TEXT], redactor=service.redactor(), default_threshold=0.5)
        ).items == ()
        assert (
            len(
                (
                    await find_in(on, [TEXT], redactor=service.redactor(), default_threshold=0.5)
                ).items
            )
            == 1
        )

    async def test_detect_only_never_changes_the_text(self) -> None:
        service = FakeService()
        policy = RedactionPolicy(
            entities={"PERSON": E(mode=M.DETECT_ONLY)},
            keywords=[KeywordList(name="K", words="Falcon", mode=M.DETECT_ONLY)],
        )
        out = await service.redactor().redact_request(
            [{"role": "user", "content": TEXT}], policy=EffectivePolicy(policy=policy)
        )
        assert out.messages[0]["content"] == TEXT and out.entity_count == 0
        assert out.active is False

    async def test_the_text_too_large_is_incomplete_not_clean(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("gateway.privacy.finds.MAX_TEXT_CHARS", 5)
        policy = RedactionPolicy(
            keywords=[KeywordList(name="K", words="Falcon", mode=M.DETECT_ONLY)]
        )
        result = await find_in(policy, [TEXT], redactor=None, default_threshold=0.5)
        assert result.incomplete and result.items == ()


class TestTheModeOrder:
    def test_detect_only_is_just_above_off(self) -> None:
        order = list(EntityMode)
        assert order[:3] == [M.OFF, M.DETECT_ONLY, M.ANONYMISE_RESTORE]

    def test_the_fold_takes_the_strictest_and_detect_only_never_lowers(self) -> None:
        loud = RedactionPolicy(entities={"PERSON": E(mode=M.REDACT)})
        quiet = RedactionPolicy(entities={"PERSON": E(mode=M.DETECT_ONLY)})
        assert RedactionPolicy.combine([loud, quiet]).mode_for("PERSON") is M.REDACT
        assert RedactionPolicy.combine([quiet, loud]).mode_for("PERSON") is M.REDACT
        assert RedactionPolicy.combine([quiet, quiet]).mode_for("PERSON") is M.DETECT_ONLY

    def test_a_user_cannot_use_detect_only_to_lower_an_administrators_mode(self) -> None:
        from gateway.redaction.user_rules import UserRule, fold

        base = RedactionPolicy(entities={"PERSON": E(mode=M.REDACT)})
        rule_ = UserRule(uuid.uuid4(), "mode", "PERSON", None, M.DETECT_ONLY)
        assert fold(base, [rule_]).mode_for("PERSON") is M.REDACT
        # And where the administrators say nothing it adds a thing to count and hides nothing.
        assert fold(RedactionPolicy(), [rule_]).mode_for("PERSON") is M.DETECT_ONLY

    def test_a_detect_only_policy_changes_nothing_in_force(self) -> None:
        policy = RedactionPolicy(entities={"PERSON": E(mode=M.DETECT_ONLY)})
        assert policy.is_active() is False and policy.finds_anything() is True
        assert policy.protects() == set()

    def test_moving_to_detect_only_is_a_weakening(self) -> None:
        hide = RedactionPolicy(entities={"PERSON": E(mode=M.REDACT)})
        count = RedactionPolicy(entities={"PERSON": E(mode=M.DETECT_ONLY)})
        assert count.weakens(hide) and not hide.weakens(count)


class TestLegacyShape:
    def test_the_new_shape_is_left_alone(self) -> None:
        raw = RedactionPolicy(
            formats={"IBAN_CODE": E(mode=M.BLOCK)}, entities={"PERSON": E(mode=M.REDACT)}
        ).model_dump(mode="json")
        again = RedactionPolicy.model_validate(raw)
        assert set(again.formats) == {"IBAN_CODE"} and set(again.entities) == {"PERSON"}

    def test_spans_carry_the_engine(self) -> None:
        assert EntitySpan(start=0, end=1, entity_type="X").engine is None


class TestAdminApi:
    async def install(self, app: Any, service: FakeService) -> None:
        app.state.settings.redaction.engine = "http"
        app.state.settings.redaction.endpoint = "http://detector:8080"
        app.state.control_http = service.client()
        app.state.redactor = service.redactor()
        app.state.redaction = None

    async def test_the_engines_are_what_the_service_reports(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        service = FakeService()
        await self.install(app, service)
        body = (await client.get("/api/admin/privacy/engines")).json()
        assert body["reachable"] is True
        assert body["pattern_entities"] == ["EMAIL_ADDRESS", "IBAN_CODE", "CREDIT_CARD"]
        assert [
            (e["name"], e["available"], e["default"], e["custom_labels"]) for e in body["engines"]
        ] == [
            ("spacy", True, True, False),
            ("gliner", True, False, True),
        ]
        service.set_available("gliner", False)
        again = (await client.get("/api/admin/privacy/engines")).json()
        assert [e["available"] for e in again["engines"]] == [True, False]

    async def test_only_an_available_engine_that_fits_the_items_can_be_chosen(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        service = FakeService()
        await self.install(app, service)

        def body(**policy: Any) -> dict[str, Any]:
            return {"name": f"P{uuid.uuid4().hex[:4]}", "policy": policy}

        ok = await client.post("/api/admin/privacy/policies", json=body(engine="gliner"))
        assert ok.status_code == 201 and ok.json()["engine_available"] is True
        custom = {"custom_entities": [{"name": "CODENAME", "label": "project code name"}]}
        assert (
            await client.post("/api/admin/privacy/policies", json=body(engine="gliner", **custom))
        ).status_code == 201
        # spaCy takes no labels; the default engine (spaCy) neither.
        refused = await client.post(
            "/api/admin/privacy/policies", json=body(engine="spacy", **custom)
        )
        assert refused.status_code == 422 and "does not take custom entity types" in refused.text
        assert (
            await client.post("/api/admin/privacy/policies", json=body(**custom))
        ).status_code == 422
        # A type the engine does not expose, and a format the service does not have.
        unknown = await client.post(
            "/api/admin/privacy/policies",
            json=body(formats={}, entities={"DRUG": {}}),
        )
        assert unknown.status_code == 422 and "does not find: DRUG" in unknown.text
        noformat = await client.post("/api/admin/privacy/policies", json=body(formats={"NOPE": {}}))
        assert noformat.status_code == 422 and "no built-in format" in noformat.text
        # An engine that is down cannot be chosen.
        service.set_available("gliner", False)
        down = await client.post("/api/admin/privacy/policies", json=body(engine="gliner"))
        assert down.status_code == 422 and "not available" in down.text
        # The default engine is never refused.
        assert (await client.post("/api/admin/privacy/policies", json=body())).status_code == 201

    async def test_a_saved_policy_whose_engine_goes_away_says_so(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        service = FakeService()
        await self.install(app, service)
        await add_policy(app, session_factory, "Names", engine="gliner")
        service.set_available("gliner", False)
        listed = (await client.get("/api/admin/privacy/policies")).json()["items"]
        assert [(p["name"], p["engine_available"]) for p in listed] == [("Names", False)]

    async def test_the_try_it_box_runs_the_real_detection_and_changes_nothing(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        service = FakeService()
        await self.install(app, service)
        policy = RedactionPolicy(
            engine="gliner",
            formats={"IBAN_CODE": E(mode=M.REDACT)},
            entities={"PERSON": E(mode=M.DETECT_ONLY)},
            custom_entities=[CustomEntity(name="CODENAME", label="project code name")],
            keywords=[KeywordList(name="WORDS", words="Falcon", mode=M.DETECT_ONLY)],
        )
        response = await client.post(
            "/api/admin/privacy/policies/try",
            json={
                "policy": PolicyDefinition.from_stored(policy.model_dump(mode="json")).model_dump(
                    mode="json"
                ),
                "text": TEXT,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        rows = sorted((i["group"], i["type"], i["engine"], i["text"]) for i in body["items"])
        assert rows == [
            ("entity", "CODENAME", "gliner", "Falcon"),
            ("entity", "PERSON", "gliner", "Mario Rossi"),
            ("pattern", "IBAN_CODE", "pattern", "IT60X0542811101000000123456"),
            ("pattern", "WORDS", "pattern", "Falcon"),
        ]
        scores = {i["type"]: i["score"] for i in body["items"]}
        assert scores["PERSON"] == 0.8 and scores["IBAN_CODE"] is None and scores["WORDS"] is None
        # What the redaction would do: detect-only items are left, the rest is hidden.
        # A policy has no mode: the preview shows everything it finds as removed.
        assert "Mario Rossi" not in body["redacted_text"] and "IT60X" not in body["redacted_text"]
        assert body["unavailable"] is None and body["blocked"] is False
        # With the engine down the answer says it cannot look; it is not an empty result.
        service.set_available("gliner", False)
        # (A new text: the first answer is cached by what it was asked.)
        down = (
            await client.post(
                "/api/admin/privacy/policies/try",
                json={"policy": policy.model_dump(mode="json"), "text": TEXT + " Again."},
            )
        ).json()
        assert down["unavailable"] and "gliner" in down["unavailable"]
        assert [i["type"] for i in down["items"]] == ["IBAN_CODE", "WORDS"] or {
            i["group"] for i in down["items"]
        } <= {"pattern"}

    async def test_a_try_it_needs_an_administrator(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        as_user(app, seeded.user)
        response = await client.post(
            "/api/admin/privacy/policies/try", json={"policy": {}, "text": "x"}
        )
        assert response.status_code in (401, 403)


class TestOnTheRequestPath:
    async def test_a_policy_with_an_unavailable_engine_is_a_503_not_a_pass(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: Any,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        service = FakeService()
        service.set_available("gliner", False)
        app.state.redactor = service.redactor()
        await add_embedding_model(session, seeded)
        pid = await add_policy(
            app, session_factory, "Names", engine="gliner", entities={"PERSON": E(mode=M.REDACT)}
        )
        await add_pipeline(
            app, session_factory, "embed", "P", otherwise={"level": "pass", "redact_with": str(pid)}
        )
        fake_upstream.set_json(embedding_body())
        response = await client.post(
            "/v1/embeddings",
            json={"model": "embed-1", "input": [TEXT]},
            headers=seeded.auth,
        )
        assert response.status_code == 503, response.text
        assert "'Names'" in response.text and "Mario Rossi" not in response.text
        assert fake_upstream.bodies == []

    async def test_finds_counts_with_the_policys_engine_and_a_rule_can_refuse_on_it(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: Any,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        service = FakeService()
        app.state.redactor = service.redactor()
        await add_embedding_model(session, seeded)
        pid = await add_policy(
            app,
            session_factory,
            "Names",
            engine="gliner",
            entities={"PERSON": E(mode=M.DETECT_ONLY)},
            keywords=[KeywordList(name="K", words="Falcon", mode=M.DETECT_ONLY)],
        )
        await add_pipeline(
            app,
            session_factory,
            "embed",
            "P",
            rules=[rule("Two items", [{"key": "finds", "values": [str(pid)], "count": 2}])],
        )
        fake_upstream.set_json(embedding_body())
        refused = await client.post(
            "/v1/embeddings", json={"model": "embed-1", "input": [TEXT]}, headers=seeded.auth
        )
        assert refused.status_code == 403 and "'Two items'" in refused.text
        assert "Mario" not in refused.text
        assert service.requests and service.requests[0]["ner_engine"] == "gliner"
        # One item is not two; and hiding nothing, the text goes on unchanged.
        ok = await client.post(
            "/v1/embeddings",
            json={"model": "embed-1", "input": ["Only Falcon here."]},
            headers=seeded.auth,
        )
        assert ok.status_code == 200, ok.text
        assert fake_upstream.bodies[-1]["input"] == ["Only Falcon here."]


def pattern_only_service() -> FakeService:
    """A service whose image has no NER model: engines are listed but expose no entities."""
    service = FakeService()
    service.health["pattern_entities"] = ["EMAIL_ADDRESS", "IBAN_CODE", "CREDIT_CARD"]
    for engine in service.health["ner_engines"]:
        engine["entities"] = []
        engine["available"] = engine["name"] == "spacy"
        engine["custom_labels"] = False
    return service


class TestPatternOnlyService:
    async def install(self, app: Any, service: FakeService) -> None:
        app.state.settings.redaction.engine = "http"
        app.state.settings.redaction.endpoint = "http://detector:8080"
        app.state.control_http = service.client()
        app.state.redactor = service.redactor()
        app.state.redaction = None

    async def test_an_entity_item_is_refused_with_the_no_ner_message(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        await self.install(app, pattern_only_service())
        refused = await client.post(
            "/api/admin/privacy/policies",
            json={"name": "N", "policy": {"entities": {"PERSON": {}}}},
        )
        assert refused.status_code == 422
        assert "No NER engine is installed in the redaction service" in refused.text
        assert "entities need one" in refused.text
        # Patterns alone are fine.
        ok = await client.post(
            "/api/admin/privacy/policies",
            json={"name": "P", "policy": {"formats": {"IBAN_CODE": {}}}},
        )
        assert ok.status_code == 201

    async def test_the_placeholder_style_round_trips_on_every_kind_of_item(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        await self.install(app, FakeService())
        policy = {
            "formats": {"IBAN_CODE": {"placeholder": "same_length"}},
            "entities": {"PERSON": {"placeholder": "same_length"}},
            "patterns": [
                {
                    "name": "BADGE",
                    "regex": "B-[0-9]+",
                    "placeholder": "same_length",
                }
            ],
            "keywords": [
                {
                    "name": "CODES",
                    "words": ["falcon"],
                    "placeholder": "same_length",
                }
            ],
            "engine": "gliner",
            "custom_entities": [
                {
                    "name": "CODENAME",
                    "label": "project code name",
                    "placeholder": "same_length",
                }
            ],
        }
        created = await client.post(
            "/api/admin/privacy/policies", json={"name": "Round", "policy": policy}
        )
        assert created.status_code == 201, created.text
        got = (await client.get("/api/admin/privacy/policies")).json()["items"][0]["policy"]
        assert got["formats"]["IBAN_CODE"]["placeholder"] == "same_length"
        assert got["entities"]["PERSON"]["placeholder"] == "same_length"
        assert got["patterns"][0]["placeholder"] == "same_length"
        assert got["keywords"][0]["placeholder"] == "same_length"
        assert got["custom_entities"][0]["placeholder"] == "same_length"

    async def test_my_privacy_offers_no_model_types_and_refuses_them(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        from gateway.models import PrivacySettings

        as_user(app, seeded.user)
        async with session_factory() as db:
            db.add(PrivacySettings(id=1, user_entity_types=["PERSON", "IBAN_CODE"]))
            await db.commit()
        await self.install(app, pattern_only_service())
        options = (await client.get("/api/me/privacy/options")).json()
        assert options["ner_available"] is False
        assert options["entity_types"] == ["IBAN_CODE"]
        assert "No NER engine is installed" in options["ner_note"]
        refused = await client.post(
            "/api/me/privacy/policies",
            json={"name": "N", "entries": [{"kind": "mode", "name": "PERSON"}]},
        )
        assert refused.status_code == 422 and "No NER engine is installed" in refused.text
        ok = await client.post(
            "/api/me/privacy/policies",
            json={
                "name": "I",
                "entries": [{"kind": "mode", "name": "IBAN_CODE"}],
            },
        )
        assert ok.status_code == 201


class TestSwitchedOffDetectors:
    async def test_the_report_carries_the_switches_and_policies_follow_them(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        service = FakeService()
        app.state.settings.redaction.engine = "http"
        app.state.settings.redaction.endpoint = "http://detector:8080"
        app.state.control_http = service.client()
        app.state.redactor = service.redactor()
        app.state.redaction = None
        off = await client.put(
            "/api/admin/redaction/engine",
            json={
                "engine": "noop",
                "presidio_pattern_matching": False,
                "spacy_enabled": True,
                "gliner_enabled": False,
            },
        )
        assert off.status_code == 200, off.text
        body = (await client.get("/api/admin/privacy/engines")).json()
        assert body["patterns_enabled"] is False
        assert {e["name"]: e["enabled"] for e in body["engines"]} == {
            "spacy": True,
            "gliner": False,
        }
        # The raw capabilities are still reported, for the Detectors cards.
        assert body["pattern_entities"] == ["EMAIL_ADDRESS", "IBAN_CODE", "CREDIT_CARD"]
        refused = await client.post(
            "/api/admin/privacy/policies", json={"name": "G", "policy": {"engine": "gliner"}}
        )
        assert refused.status_code == 422 and "switched off" in refused.text
        no_format = await client.post(
            "/api/admin/privacy/policies",
            json={"name": "F", "policy": {"formats": {"IBAN_CODE": {}}}},
        )
        assert no_format.status_code == 422 and "no built-in format" in no_format.text


class TestUserPatternNames:
    async def test_a_user_pattern_cannot_take_the_name_of_an_administrator_item(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        as_user(app, seeded.user)
        await add_policy(
            app,
            session_factory,
            "Admin",
            patterns=[{"name": "BADGE", "regex": "B-[0-9]+"}],
            keywords=[{"name": "CODES", "words": ["falcon"]}],
            custom_entities=[{"name": "PROJECT", "label": "project code name"}],
            engine="gliner",
        )
        for name in ("BADGE", "CODES", "PROJECT"):
            refused = await client.post(
                "/api/me/privacy/policies",
                json={
                    "name": f"U{name}",
                    "entries": [{"kind": "pattern", "name": name, "regex": "x[0-9]+"}],
                },
            )
            assert refused.status_code == 422, name
            assert "is the name of an entity type" in refused.text
        fresh = await client.post(
            "/api/me/privacy/policies",
            json={
                "name": "ok",
                "entries": [{"kind": "pattern", "name": "MINE", "regex": "x[0-9]+"}],
            },
        )
        assert fresh.status_code == 201


class TestSwitchedOffSaveTimeGaps:
    async def install(self, app: Any, service: FakeService) -> None:
        app.state.settings.redaction.engine = "http"
        app.state.settings.redaction.endpoint = "http://detector:8080"
        app.state.control_http = service.client()
        app.state.redactor = service.redactor()
        app.state.redaction = None

    async def test_the_default_engine_switched_off_refuses_entities_with_no_engine_named(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        await self.install(app, FakeService())
        off = await client.put(
            "/api/admin/redaction/engine",
            json={"engine": "noop", "spacy_enabled": False, "gliner_enabled": True},
        )
        assert off.status_code == 200
        refused = await client.post(
            "/api/admin/privacy/policies",
            json={"name": "N", "policy": {"entities": {"PERSON": {}}}},
        )
        assert refused.status_code == 422 and "switched off" in refused.text
        # Not "not installed": the engines are there.
        assert "not installed" not in refused.text

    async def test_every_engine_off_says_off_not_missing(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        await self.install(app, FakeService())
        await client.put(
            "/api/admin/redaction/engine",
            json={"engine": "noop", "spacy_enabled": False, "gliner_enabled": False},
        )
        refused = await client.post(
            "/api/admin/privacy/policies",
            json={
                "name": "N",
                "policy": {"engine": "gliner", "entities": {"PERSON": {}}},
            },
        )
        assert refused.status_code == 422 and "switched off" in refused.text

    async def test_activating_a_policy_checks_it_against_what_is_on_now(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        from privacy_helpers import add_policy

        as_user(app, await make_admin(session_factory, seeded))
        await self.install(app, FakeService())
        policy_id = await add_policy(
            app,
            session_factory,
            "Names",
            active=False,
            engine="gliner",
            entities={"PERSON": {}},
        )
        await client.put(
            "/api/admin/redaction/engine", json={"engine": "noop", "gliner_enabled": False}
        )
        refused = await client.patch(
            f"/api/admin/privacy/policies/{policy_id}", json={"is_active": True}
        )
        assert refused.status_code == 422 and "switched off" in refused.text

    async def test_an_inconsistent_family_switch_is_rejected(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        await self.install(app, FakeService())
        bad = await client.put(
            "/api/admin/redaction/engine",
            json={
                "engine": "noop",
                "presidio_ner": True,
                "spacy_enabled": False,
                "gliner_enabled": False,
            },
        )
        assert bad.status_code == 400 and "contradicts" in bad.text

    async def test_a_users_policy_counts_as_using_a_detector(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        from gateway.models import UserPolicy

        as_user(app, await make_admin(session_factory, seeded))
        await self.install(app, FakeService())
        async with session_factory() as db:
            db.add(
                UserPolicy(
                    user_id=seeded.user.id,
                    name="Mine",
                    rules=[{"kind": "mode", "name": "PERSON", "regex": None}],
                )
            )
            await db.commit()
        refused = await client.put(
            "/api/admin/redaction/engine", json={"engine": "noop", "spacy_enabled": False}
        )
        assert refused.status_code == 409 and "Mine (user policy)" in refused.text
        formats = await client.put(
            "/api/admin/redaction/engine", json={"engine": "noop", "gliner_enabled": False}
        )
        assert formats.status_code == 200

    async def test_the_default_follows_the_service_not_a_constant(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        from privacy_helpers import add_policy

        as_user(app, await make_admin(session_factory, seeded))
        service = FakeService()
        for engine in service.health["ner_engines"]:
            engine["default"] = engine["name"] == "gliner"
        await self.install(app, service)
        await add_policy(app, session_factory, "Names", entities={"PERSON": {}})
        # No engine named: it reads with the service's default, which is GLiNER here.
        assert (
            await client.put(
                "/api/admin/redaction/engine", json={"engine": "noop", "spacy_enabled": False}
            )
        ).status_code == 200
        refused = await client.put(
            "/api/admin/redaction/engine",
            json={"engine": "noop", "spacy_enabled": True, "gliner_enabled": False},
        )
        assert refused.status_code == 409 and "GLiNER" in refused.text

    async def test_a_change_is_audited_with_a_reason_and_restores_the_running_engine(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        from gateway.models import AdminAuditEvent as AuditLog
        from sqlalchemy import select

        as_user(app, await make_admin(session_factory, seeded))
        await self.install(app, FakeService())
        off = await client.put("/api/admin/redaction/engine", json={"engine": "noop"})
        assert off.status_code == 200
        body = off.json()
        assert body["configured"]["reason"] == "Changed from the console"
        # The engine that redacts and was in force is what "on" brings back.
        assert body["restore_engine"] == "http"
        async with session_factory() as db:
            actions = (await db.execute(select(AuditLog.action))).scalars().all()
        assert "redaction_engine.set" in actions


class TestUserFormatsFollowTheSwitches:
    async def test_a_format_the_service_does_not_offer_now_is_refused_and_not_offered(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        as_user(app, seeded.user)
        service = FakeService()
        app.state.settings.redaction.engine = "http"
        app.state.settings.redaction.endpoint = "http://detector:8080"
        app.state.control_http = service.client()
        app.state.redaction = None
        async with session_factory() as db:
            from gateway.models import RedactionConfig

            db.add(
                RedactionConfig(
                    engine="http",
                    presidio_pattern_matching=False,
                    presidio_ner=True,
                    reason="x",
                )
            )
            await db.commit()
        options = (await client.get("/api/me/privacy/options")).json()
        assert "IBAN_CODE" not in options["offered_types"]
        assert "PERSON" in options["offered_types"]
        refused = await client.post(
            "/api/me/privacy/policies",
            json={
                "name": "I",
                "entries": [{"kind": "mode", "name": "IBAN_CODE"}],
            },
        )
        assert (
            refused.status_code == 422
            and "not something the redaction service finds" in refused.text
        )
        ok = await client.post(
            "/api/me/privacy/policies",
            json={"name": "P", "entries": [{"kind": "mode", "name": "PERSON"}]},
        )
        assert ok.status_code == 201


class TestSwitchingOffNeedsTheService:
    async def test_it_is_refused_while_the_service_cannot_be_asked(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))

        class Down(FakeService):
            def handle(self, request: httpx.Request) -> httpx.Response:
                raise httpx.ConnectError("down")

        app.state.settings.redaction.engine = "http"
        app.state.settings.redaction.endpoint = "http://detector:8080"
        app.state.control_http = Down().client()
        refused = await client.put(
            "/api/admin/redaction/engine", json={"engine": "noop", "spacy_enabled": False}
        )
        assert refused.status_code == 409 and "can't be checked" in refused.text
        # Switching one on, or leaving them as they are, never needs the service.
        assert (
            await client.put("/api/admin/redaction/engine", json={"engine": "noop"})
        ).status_code == 200

    async def test_the_preview_of_a_policy_that_needs_a_switched_off_engine_is_a_named_503(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        from privacy_helpers import add_policy

        as_user(app, await make_admin(session_factory, seeded))
        service = FakeService()
        app.state.settings.redaction.engine = "http"
        app.state.settings.redaction.endpoint = "http://detector:8080"
        app.state.control_http = service.client()
        app.state.redactor = service.redactor(gliner_enabled=False)
        app.state.redaction = None
        policy_id = await add_policy(
            app, session_factory, "Names", engine="gliner", entities={"PERSON": {}}
        )
        response = await client.post(
            "/api/admin/redaction/preview",
            json={"text": TEXT, "policy_id": str(policy_id)},
        )
        assert response.status_code == 503, response.text
        assert "switched off" in response.text
