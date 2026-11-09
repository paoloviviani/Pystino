# ruff: noqa: S608, E501
"""Fixes from the review of ADR 0113, section 0c: unsupported types, wire limits, the fold,
the 0083 downgrade, and the engine labels."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import Seeded
from gateway.config import (
    CustomEntity,
    EntityMode,
    EntityPolicy,
    KeywordList,
    NerAsk,
    RedactionPolicy,
)
from gateway.privacy.finds import find_in
from gateway.redaction.http import EngineUnavailableError
from pydantic import ValidationError
from test_admin import as_user, make_admin
from test_migration_0067 import _alembic
from test_privacy_ner_engines import FakeService

M = EntityMode
E = EntityPolicy


class Unsupported(FakeService):
    """A service that answers 200 but names the requested types its engine cannot serve."""

    def __init__(self, unsupported: list[str]) -> None:
        super().__init__()
        self.unsupported = unsupported

    def handle(self, request: httpx.Request) -> httpx.Response:
        response = super().handle(request)
        if request.url.path == "/detect" and response.status_code == 200:
            body = response.json()
            body["unsupported_types"] = self.unsupported
            return httpx.Response(200, json=body)
        return response


class TestFindsFailsClosedOnUnsupportedTypes:
    async def test_a_type_the_engine_cannot_serve_makes_the_count_unavailable(self) -> None:
        service = Unsupported(["NRP"])
        policy = RedactionPolicy(entities={"NRP": E(mode=M.DETECT_ONLY)})
        result = await find_in(
            policy, ["Some text"], redactor=service.redactor(), default_threshold=0.5
        )
        assert result.items == ()
        assert result.unavailable and "NRP" in result.unavailable

    async def test_a_custom_label_the_engine_ignores_is_unavailable_too(self) -> None:
        service = Unsupported(["CODENAME"])
        policy = RedactionPolicy(
            custom_entities=[
                CustomEntity(name="CODENAME", label="project code name", mode=M.DETECT_ONLY)
            ]
        )
        result = await find_in(policy, ["x"], redactor=service.redactor(), default_threshold=0.5)
        assert result.unavailable and "CODENAME" in result.unavailable

    async def test_redaction_is_not_refused_for_it_and_the_answer_is_not_cached(self) -> None:
        service = Unsupported(["NRP"])
        redactor = service.redactor()
        policy = RedactionPolicy(entities={"PERSON": E(mode=M.REDACT)})
        # Not strict: the detector's historical behaviour (a loud log, no refusal).
        await redactor.detect(["Mario Rossi"], policy=policy)
        await redactor.detect(["Mario Rossi"], policy=policy)
        assert len(service.requests) == 2  # a partial answer is never cached
        with pytest.raises(EngineUnavailableError):
            await redactor.detect(["Mario Rossi"], policy=policy, strict=True)

    async def test_a_default_switch_from_spacy_to_gliner_with_a_missing_type(self) -> None:
        service = FakeService()
        # The default is now GLiNER, which has no NRP: it reports it as unsupported.
        service.health["ner_engines"][0]["default"] = False
        service.health["ner_engines"][1]["default"] = True
        service.finds["gliner"] = {}
        fake = Unsupported(["NRP"])
        fake.health = service.health
        policy = RedactionPolicy(entities={"NRP": E(mode=M.DETECT_ONLY)})
        result = await find_in(policy, ["x"], redactor=fake.redactor(), default_threshold=0.5)
        assert result.unavailable


class TestSaveRefusesEntitiesTheEngineCannotFind:
    async def install(self, app: Any, service: FakeService) -> None:
        app.state.settings.redaction.engine = "http"
        app.state.settings.redaction.endpoint = "http://detector:8080"
        app.state.control_http = service.client()
        app.state.redactor = service.redactor()

    async def test_an_engine_without_ner_models_refuses_entity_items(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        service = FakeService()
        service.health["ner_engines"][0]["entities"] = []  # the pattern-only image
        await self.install(app, service)
        refused = await client.post(
            "/api/admin/privacy/policies",
            json={"name": "Names", "policy": {"entities": {"PERSON": {"mode": "redact"}}}},
        )
        assert refused.status_code == 422 and "no NER model" in refused.text
        ok = await client.post(
            "/api/admin/privacy/policies",
            json={"name": "Formats", "policy": {"formats": {"IBAN_CODE": {"mode": "redact"}}}},
        )
        assert ok.status_code == 201, ok.text


class TestWireLimits:
    def test_a_label_over_80_characters_is_refused_when_saved(self) -> None:
        CustomEntity(name="A", label="x" * 80)
        with pytest.raises(ValidationError):
            CustomEntity(name="A", label="x" * 81)

    def test_more_than_30_custom_entities_in_a_policy_is_refused(self) -> None:
        many = [CustomEntity(name=f"T{n}", label=f"thing {n}") for n in range(31)]
        with pytest.raises(ValidationError):
            RedactionPolicy(custom_entities=many)
        RedactionPolicy(custom_entities=many[:30])

    async def test_policies_that_together_ask_for_too_many_labels_are_a_named_503_not_a_500(
        self,
    ) -> None:
        service = FakeService()
        redactor = service.redactor()
        labels = tuple((f"T{n}", f"thing {n}") for n in range(31))
        with pytest.raises(EngineUnavailableError) as raised:
            await redactor.detect(["x"], asks=[NerAsk("gliner", None, labels)])
        assert "31 custom entity types" in str(raised.value)
        assert service.requests == []

    async def test_the_request_path_answers_with_a_503_naming_it(self) -> None:
        from gateway.config import Contributor, EffectivePolicy
        from gateway.errors import ServiceUnavailableError

        service = FakeService()
        policies = [
            RedactionPolicy(
                engine="gliner",
                custom_entities=[
                    CustomEntity(name=f"T{p}_{n}", label=f"thing {p} {n}") for n in range(16)
                ],
            )
            for p in range(2)
        ]
        eff = EffectivePolicy(
            policy=RedactionPolicy.combine(policies),
            contributors=tuple(
                Contributor(id=uuid.uuid4(), name=f"P{i}", owner="admin", policy=p)
                for i, p in enumerate(policies)
            ),
        )
        with pytest.raises(ServiceUnavailableError) as raised:
            await service.redactor().redact_request([{"role": "user", "content": "x"}], policy=eff)
        assert raised.value.status_code == 503 and "32" in raised.value.message

    async def test_saving_a_policy_that_could_exceed_the_budget_is_refused(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        service = FakeService()
        await TestSaveRefusesEntitiesTheEngineCannotFind().install(app, service)

        def body(prefix: str, count: int) -> dict[str, Any]:
            return {
                "name": f"Labels {prefix}",
                "policy": {
                    "engine": "gliner",
                    "custom_entities": [
                        {"name": f"{prefix}{n}", "label": f"{prefix} thing {n}"}
                        for n in range(count)
                    ],
                },
            }

        assert (
            await client.post("/api/admin/privacy/policies", json=body("A", 20))
        ).status_code == 201
        refused = await client.post("/api/admin/privacy/policies", json=body("B", 11))
        assert refused.status_code == 422 and "31" in refused.text
        assert (
            await client.post("/api/admin/privacy/policies", json=body("C", 10))
        ).status_code == 201

    async def test_a_validation_error_building_the_request_is_not_a_500(self) -> None:
        # The label is over the wire limit even though the gateway let it through.
        service = FakeService()
        with pytest.raises(EngineUnavailableError):
            await service.redactor().detect(
                ["x"], asks=[NerAsk("gliner", None, (("T", "x" * 81),))]
            )


class TestTheFoldNeverLowersAnotherTablesMode:
    def test_detect_only_in_one_policy_does_not_lower_a_keyword_or_pattern_of_another(self) -> None:
        loud = RedactionPolicy(
            keywords=[KeywordList(name="LOCATION", words="Rome", mode=M.REDACT)],
            patterns=[{"name": "PERSON", "regex": "Mr [A-Z]", "mode": "redact"}],  # type: ignore[list-item]
        )
        quiet = RedactionPolicy(
            entities={"LOCATION": E(mode=M.DETECT_ONLY), "PERSON": E(mode=M.OFF)}
        )
        for order in ((loud, quiet), (quiet, loud)):
            combined = RedactionPolicy.combine(list(order))
            assert combined.mode_for("LOCATION") is M.REDACT
            assert combined.mode_for("PERSON") is M.REDACT

    def test_one_policy_naming_a_type_in_two_tables_takes_the_strictest(self) -> None:
        policy = RedactionPolicy(
            entities={"LOCATION": E(mode=M.DETECT_ONLY)},
            patterns=[{"name": "LOCATION", "regex": "Rome", "mode": "block"}],  # type: ignore[list-item]
        )
        assert policy.mode_for("LOCATION") is M.BLOCK


class TestFormatsGoWithEveryEngineCall:
    def test_formats_are_asked_of_each_engine(self) -> None:
        from gateway.config import Contributor, EffectivePolicy

        a = RedactionPolicy(
            engine="spacy",
            formats={"DATE_TIME": E(mode=M.REDACT)},
            entities={"PERSON": E(mode=M.REDACT)},
        )
        b = RedactionPolicy(engine="gliner", entities={"LOCATION": E(mode=M.REDACT)})
        eff = EffectivePolicy(
            policy=RedactionPolicy.combine([a, b]),
            contributors=tuple(
                Contributor(id=uuid.uuid4(), name=n, owner="admin", policy=p)
                for n, p in (("A", a), ("B", b))
            ),
        )
        by_engine = {ask.engine: ask.types for ask in eff.asks()}
        assert by_engine["gliner"] == ("DATE_TIME", "LOCATION")
        assert by_engine["spacy"] == ("DATE_TIME", "PERSON")


class TestEngineLabels:
    async def test_a_custom_label_equal_to_a_default_label_is_refused_at_save(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        service = FakeService()
        service.health["ner_engines"][1]["labels"] = ["person", "date of birth"]
        await TestSaveRefusesEntitiesTheEngineCannotFind().install(app, service)
        refused = await client.post(
            "/api/admin/privacy/policies",
            json={
                "name": "Clash",
                "policy": {
                    "engine": "gliner",
                    "custom_entities": [{"name": "WHO", "label": "Person"}],
                },
            },
        )
        assert refused.status_code == 422 and "already uses" in refused.text
        ok = await client.post(
            "/api/admin/privacy/policies",
            json={
                "name": "Fine",
                "policy": {
                    "engine": "gliner",
                    "custom_entities": [{"name": "WHO", "label": "project owner"}],
                },
            },
        )
        assert ok.status_code == 201, ok.text


class TestTryEndpoint:
    async def test_an_engine_that_goes_away_between_the_calls_is_a_503(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        service = FakeService()
        await TestSaveRefusesEntitiesTheEngineCannotFind().install(app, service)
        redactor = app.state.redactor
        original = redactor.detect
        calls = {"n": 0}

        async def detect(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] > 1:
                raise EngineUnavailableError("gliner")
            return await original(*args, **kwargs)

        redactor.detect = detect
        policy = RedactionPolicy(engine="gliner", entities={"PERSON": E(mode=M.REDACT)})
        response = await client.post(
            "/api/admin/privacy/policies/try",
            json={"policy": policy.model_dump(mode="json"), "text": "Mario Rossi"},
        )
        assert response.status_code == 503, response.text

    async def test_the_placeholders_it_returns_use_a_throwaway_key(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        service = FakeService()
        await TestSaveRefusesEntitiesTheEngineCannotFind().install(app, service)
        policy = RedactionPolicy(entities={"PERSON": E(mode=M.ANONYMISE)})
        texts = []
        for suffix in ("", " "):
            response = await client.post(
                "/api/admin/privacy/policies/try",
                json={"policy": policy.model_dump(mode="json"), "text": "Mario Rossi" + suffix},
            )
            assert response.status_code == 200, response.text
            texts.append(response.json()["redacted_text"])
        assert texts[0].strip() != texts[1].strip()  # a fresh key every run


# -- the 0083 downgrade --------------------------------------------------------------


T0 = "2026-11-09 12:00:00.000000"


def _db(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "d.db"
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", "0082").returncode == 0
    return path, url


def _insert_policy(conn: sqlite3.Connection, name: str, policy: dict[str, Any]) -> str:
    ident = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO redaction_policies (id, name, policy, is_active, reason, created_at, updated_at) "
        "VALUES (?, ?, ?, 1, '', ?, ?)",
        (ident, name, json.dumps(policy), T0, T0),
    )
    return ident


def _insert_pipeline(conn: sqlite3.Connection, rules: list[dict[str, Any]]) -> str:
    ident = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO privacy_pipelines (id, owner_id, kind, name, enabled, position, who, target, rules, otherwise, created_at, updated_at) "
        "VALUES (?, NULL, 'embed', 'Remote', 1, 0, ?, ?, ?, ?, ?, ?)",
        (
            ident,
            json.dumps({"mode": "all", "ids": []}),
            json.dumps({"mode": "all", "providers": [], "models": []}),
            json.dumps(rules),
            json.dumps({"level": "pass"}),
            T0,
            T0,
        ),
    )
    return ident


def _rule(name: str, conditions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "enabled": True,
        "observe_only": False,
        "conditions": conditions,
        "action": {"level": "refuse"},
    }


def _rows(path: Path, table: str) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]


def test_the_downgrade_round_trips_policies_and_the_ids_condition(tmp_path: Path) -> None:
    path, url = _db(tmp_path)
    old = {
        "default_mode": "off",
        "entities": {
            "PERSON": {"mode": "redact", "placeholder": "tagged", "threshold": 0.7},
            "IBAN_CODE": {"mode": "block", "placeholder": "tagged", "threshold": None},
        },
        "patterns": [],
        "allow_list": ["acme.test"],
    }
    with closing(sqlite3.connect(path)) as conn:
        pid = _insert_policy(conn, "Mixed", old)
        _insert_pipeline(
            conn,
            [_rule("No IDs", [{"key": "ids", "values": [], "negate": False}])],
        )
        conn.commit()
    assert _alembic(url, "upgrade", "0083").returncode == 0
    names = {r["name"] for r in _rows(path, "redaction_policies")}
    assert names == {"Mixed", "Identifiers (migrated)"}

    down = _alembic(url, "downgrade", "0082")
    assert down.returncode == 0, down.stderr[-1500:]
    policies = {r["name"]: r for r in _rows(path, "redaction_policies")}
    # The migrated identifiers policy only existed to be counted, and is gone again.
    assert set(policies) == {"Mixed"}
    restored = json.loads(policies["Mixed"]["policy"])
    assert set(restored) == {"default_mode", "entities", "patterns", "allow_list"}
    assert restored["entities"]["PERSON"]["mode"] == "redact"
    assert restored["entities"]["PERSON"]["threshold"] == 0.7
    assert restored["entities"]["IBAN_CODE"]["mode"] == "block"
    assert policies["Mixed"]["id"] == pid
    (pipeline,) = _rows(path, "privacy_pipelines")
    (rule,) = json.loads(pipeline["rules"])
    assert rule["conditions"] == [{"key": "ids", "values": [], "negate": False}]
    with closing(sqlite3.connect(path)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "detector_profiles" in tables and "privacy_classifiers" not in tables
        columns = {r[1] for r in conn.execute("PRAGMA table_info(redaction_policies)")}
        assert "profile_id" in columns and "shared_with_users" not in columns
    # And it goes forward again.
    assert _alembic(url, "upgrade", "0083").returncode == 0


def test_detect_only_becomes_off_on_the_way_down(tmp_path: Path) -> None:
    path, url = _db(tmp_path)
    assert _alembic(url, "upgrade", "0083").returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        _insert_policy(
            conn,
            "Counting",
            {
                "engine": None,
                "default_mode": "off",
                "formats": {"IBAN_CODE": {"mode": "detect_only", "placeholder": "tagged"}},
                "entities": {"PERSON": {"mode": "redact", "placeholder": "tagged"}},
                "custom_entities": [],
                "patterns": [],
                "keywords": [{"name": "K", "words": ["x"], "mode": "detect_only"}],
                "allow_list": [],
            },
        )
        conn.commit()
    down = _alembic(url, "downgrade", "0082")
    assert down.returncode == 0, down.stderr[-1500:]
    (row,) = _rows(path, "redaction_policies")
    entities = json.loads(row["policy"])["entities"]
    assert entities["IBAN_CODE"]["mode"] == "off" and entities["PERSON"]["mode"] == "redact"


def test_the_downgrade_refuses_what_the_older_shape_cannot_say(tmp_path: Path) -> None:
    path, url = _db(tmp_path)
    assert _alembic(url, "upgrade", "0083").returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        pid = _insert_policy(
            conn,
            "Codenames",
            {
                "engine": "gliner",
                "default_mode": "off",
                "formats": {},
                "entities": {},
                "custom_entities": [],
                "patterns": [],
                "keywords": [{"name": "K", "words": ["x"], "mode": "redact"}],
                "allow_list": [],
            },
        )
        _insert_pipeline(
            conn,
            [
                _rule(
                    "Two",
                    [
                        {
                            "key": "finds",
                            "values": [str(uuid.UUID(pid))],
                            "negate": False,
                            "count": 2,
                        }
                    ],
                )
            ],
        )
        conn.commit()
    refused = _alembic(url, "downgrade", "0082")
    assert refused.returncode != 0
    assert "keyword lists" in refused.stderr and "finds at least N" in refused.stderr
    assert "PYSTINO_ALLOW_LOSSY_DOWNGRADE" in refused.stderr
    # Nothing was changed by the refusal.
    assert json.loads(_rows(path, "redaction_policies")[0]["policy"])["keywords"]

    import os
    import sys

    import test_migration_0067 as helper

    env = {**os.environ, "PYSTINO_ALLOW_LOSSY_DOWNGRADE": "1"}
    done = helper.subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(helper._INI),
            "-x",
            f"url={url}",
            "downgrade",
            "0082",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert done.returncode == 0, done.stderr[-1500:]
    (rule,) = json.loads(_rows(path, "privacy_pipelines")[0]["rules"])
    assert rule["conditions"] == []


class TestReadingDecidesFromTheFile:
    async def test_the_api_refuses_content_conditions_on_a_reading_rule_with_a_400(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        for key in ("finds", "classified"):
            response = await client.post(
                "/api/admin/privacy/pipelines",
                json={
                    "kind": "read",
                    "name": f"Reads {key}",
                    "rules": [
                        {
                            "name": "R",
                            "conditions": [{"key": key, "values": [str(uuid.uuid4())]}],
                            "action": {"level": "refuse"},
                        }
                    ],
                },
            )
            assert response.status_code == 400, response.text
            assert "decide from the file, not its content" in response.text
            assert "embedded or sent to chat" in response.text
        ok = await client.post(
            "/api/admin/privacy/pipelines",
            json={
                "kind": "read",
                "name": "Scans",
                "rules": [
                    {
                        "name": "R",
                        "conditions": [{"key": "scan"}],
                        "action": {"level": "refuse"},
                    }
                ],
            },
        )
        assert ok.status_code == 201, ok.text

    async def test_a_user_gets_the_same_400(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        as_user(app, seeded.user)
        response = await client.post(
            "/api/me/privacy/pipelines",
            json={
                "kind": "read",
                "name": "Mine",
                "rules": [
                    {
                        "name": "R",
                        "conditions": [{"key": "finds", "values": [str(uuid.uuid4())]}],
                        "action": {"level": "refuse"},
                    }
                ],
            },
        )
        assert response.status_code == 400, response.text

    async def test_a_read_probe_reads_the_file_for_scan_only(self) -> None:
        from gateway.privacy.rules import CONDITION_KEYS

        assert CONDITION_KEYS["read"] == ("filetype", "scan", "filename")
        assert "finds" in CONDITION_KEYS["embed"] and "classified" in CONDITION_KEYS["chat"]
