"""The v2 privacy API and request path, end to end (ADR 0114).

Validation of the new rule shape (policies, match, mode, overrides, model types), the removed
fields being refused, a block naming its rule on a real request, the model type filter, the
Test tab's claims, and a user's pipeline never loosening an administrator's.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from conftest import FakeUpstream, Seeded
from gateway.config import EntityMode, EntityPolicy
from gateway.models import UsageRecord, UsageStatus
from privacy_helpers import add_pipeline, add_policy
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin
from test_embeddings import add_embedding_model, embedding_body
from test_redaction_http import FakeDetector

BASE = "/api/admin/privacy"


@pytest.fixture
async def admin(
    app: Any, seeded: Seeded, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    as_user(app, await make_admin(session_factory, seeded))


async def policy(app: Any, session_factory: Any, name: str = "Names", **kw: Any) -> uuid.UUID:
    return await add_policy(
        app,
        session_factory,
        name,
        entities=kw.pop("entities", {"PERSON": EntityPolicy(mode=EntityMode.REDACT)}),
        **kw,
    )


def body(pid: uuid.UUID, **over: Any) -> dict[str, Any]:
    rule = {"name": "Names stay home", "policies": [str(pid)], "mode": "block"}
    return {"kind": "content", "name": "Remote providers", "rules": [rule], **over}


class TestPipelineApi:
    async def test_a_content_pipeline_round_trips_with_its_rule_sentence_inputs(
        self, app: Any, admin: None, client: httpx.AsyncClient, session_factory: Any
    ) -> None:
        pid = await policy(app, session_factory)
        rule = {
            "name": "Hide",
            "policies": [str(pid)],
            "match": "any",
            "mode": "anonymise",
            "overrides": {"person": "redact"},
            "conditions": [{"key": "finds", "count": 2}],
        }
        created = await client.post(
            f"{BASE}/pipelines",
            json=body(
                pid, rules=[rule], target={"mode": "all", "model_types": ["embedding", "rerank"]}
            ),
        )
        assert created.status_code == 201, created.text
        out = created.json()
        assert out["kind"] == "content" and out["target"]["model_types"] == ["embedding", "rerank"]
        assert "otherwise" not in out
        r = out["rules"][0]
        assert (r["policies"], r["match"], r["mode"], r["overrides"]) == (
            [str(pid)],
            "any",
            "anonymise",
            {"PERSON": "redact"},
        )
        assert r["conditions"][0]["count"] == 2 and r["conditions"][0]["values"] == []
        assert "observe_only" not in r

    @pytest.mark.parametrize(
        ("patch", "status"),
        [
            ({"otherwise": {"level": "pass"}}, 400),
            ({"kind": "chat"}, 400),
            ({"rules": [{"name": "r", "mode": "anonymise"}]}, 422),  # hides, so needs a policy
            ({"rules": [{"name": "r", "mode": "detect_only"}]}, 422),  # records, needs a condition
            ({"rules": [{"name": "r", "policies": [str(uuid.uuid4())], "mode": "block"}]}, 422),
            (
                {
                    "rules": [
                        {"name": "r", "policies": ["x"], "mode": "block", "observe_only": True}
                    ]
                },
                400,
            ),
        ],
    )
    async def test_what_cannot_be_saved(
        self,
        app: Any,
        admin: None,
        client: httpx.AsyncClient,
        session_factory: Any,
        patch: dict[str, Any],
        status: int,
    ) -> None:
        pid = await policy(app, session_factory)
        sent = body(pid)
        sent.update(patch)
        resp = await client.post(f"{BASE}/pipelines", json=sent)
        assert resp.status_code == status, resp.text

    async def test_finds_on_an_extract_rule_is_a_400_and_a_model_type_needs_content(
        self, app: Any, admin: None, client: httpx.AsyncClient, session_factory: Any
    ) -> None:
        extract = {
            "kind": "extract",
            "name": "Files",
            "rules": [
                {"name": "r", "conditions": [{"key": "finds"}], "action": {"level": "block"}}
            ],
        }
        assert (await client.post(f"{BASE}/pipelines", json=extract)).status_code == 400
        typed = {
            "kind": "extract",
            "name": "Files",
            "target": {"mode": "all", "model_types": ["chat"]},
        }
        assert (await client.post(f"{BASE}/pipelines", json=typed)).status_code == 422

    async def test_an_extract_pipeline_has_an_action_and_optional_hiding(
        self, app: Any, admin: None, client: httpx.AsyncClient, session_factory: Any
    ) -> None:
        pid = await policy(app, session_factory)
        rule = {
            "name": "Scans",
            "conditions": [{"key": "scan"}],
            "action": {"level": "default"},
            "policies": [str(pid)],
            "mode": "redact",
        }
        resp = await client.post(
            f"{BASE}/pipelines", json={"kind": "extract", "name": "Files", "rules": [rule]}
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["rules"][0]["action"] == {"level": "default", "reader": None}

    async def test_a_policy_is_what_to_look_for_and_carries_no_mode(
        self, app: Any, admin: None, client: httpx.AsyncClient, session_factory: Any
    ) -> None:
        good = await client.post(
            f"{BASE}/policies",
            json={"name": "Tax", "policy": {"patterns": [{"name": "ACME", "regex": "acme+"}]}},
        )
        assert good.status_code == 201, good.text
        assert "mode" not in good.text and "default_mode" not in good.text
        bad = await client.post(
            f"{BASE}/policies",
            json={
                "name": "Old",
                "policy": {"patterns": [{"name": "ACME", "regex": "acme+", "mode": "redact"}]},
            },
        )
        assert bad.status_code in (400, 422)


class TestRequestPath:
    async def test_a_block_refuses_and_names_the_rule_and_the_pipeline(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_embedding_model(session, seeded)
        app.state.redactor = FakeDetector({"Mario Rossi": "PERSON"}).redactor()
        pid = await policy(app, session_factory)
        await add_pipeline(
            app,
            session_factory,
            "embed",
            "Remote providers",
            rules=[
                {
                    "id": str(uuid.uuid4()),
                    "name": "Names stay",
                    "enabled": True,
                    "observe_only": False,
                    "conditions": [{"key": "finds", "values": [str(pid)], "count": 1}],
                    "action": {"level": "refuse"},
                }
            ],
        )
        fake_upstream.set_json(embedding_body())
        refused = await client.post(
            "/v1/embeddings",
            json={"model": "embed-1", "input": ["Mario Rossi"]},
            headers=seeded.auth,
        )
        assert refused.status_code == 403, refused.text
        error = refused.json()["error"]
        assert error["code"] == "routing_refused"
        assert "'Names stay'" in error["message"] and "'Remote providers'" in error["message"]
        assert fake_upstream.bodies == []
        async with session_factory() as db:
            row = (
                (await db.execute(select(UsageRecord).order_by(UsageRecord.created_at.desc())))
                .scalars()
                .first()
            )
        assert row is not None and row.status is UsageStatus.BLOCKED
        ok = await client.post(
            "/v1/embeddings",
            json={"model": "embed-1", "input": ["nothing here"]},
            headers=seeded.auth,
        )
        assert ok.status_code == 200, ok.text

    async def test_the_model_type_filter_is_part_of_the_scope(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_embedding_model(session, seeded)
        app.state.redactor = FakeDetector({"Mario Rossi": "PERSON"}).redactor()
        pid = await policy(app, session_factory)
        rules = [
            {
                "id": str(uuid.uuid4()),
                "name": "r",
                "enabled": True,
                "observe_only": False,
                "conditions": [{"key": "finds", "values": [str(pid)], "count": 1}],
                "action": {"level": "refuse"},
            }
        ]
        # A chat-only pipeline does not cover an embedding model.
        await add_pipeline(app, session_factory, "chat", "Chat only", rules=rules)
        fake_upstream.set_json(embedding_body())
        send = lambda: client.post(  # noqa: E731
            "/v1/embeddings",
            json={"model": "embed-1", "input": ["Mario Rossi"]},
            headers=seeded.auth,
        )
        assert (await send()).status_code == 200
        await add_pipeline(app, session_factory, "embed", "Embeddings", rules=rules)
        assert (await send()).status_code == 403

    async def test_the_test_tab_lists_every_rule_and_which_rule_claimed_the_span(
        self,
        app: Any,
        admin: None,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
    ) -> None:
        model = await add_embedding_model(session, seeded)
        app.state.redactor = FakeDetector({"Mario Rossi": "PERSON"}).redactor()
        soft = await policy(
            app,
            session_factory,
            "Soft",
            entities={"PERSON": EntityPolicy(mode=EntityMode.ANONYMISE_RESTORE)},
        )
        hard = await policy(
            app, session_factory, "Hard", entities={"PERSON": EntityPolicy(mode=EntityMode.REDACT)}
        )
        await add_pipeline(
            app,
            session_factory,
            "embed",
            "A",
            otherwise={"level": "pass", "redact_with": str(soft)},
        )
        await add_pipeline(
            app,
            session_factory,
            "embed",
            "B",
            otherwise={"level": "pass", "redact_with": str(hard)},
            position=1,
        )
        result = (
            await client.post(
                f"{BASE}/test",
                json={"kind": "content", "model_id": str(model.id), "text": "Ciao Mario Rossi"},
            )
        ).json()
        assert result["outcome"] == "hide" and result["level"] == "pass"
        assert [p["name"] for p in result["pipelines"]] == ["A", "B"]
        assert all(p["steps"] and p["steps"][0]["result"] == "matched" for p in result["pipelines"])
        claims = result["claims"]
        # The first rule to hide the name decides how; the second never sees it.
        assert [(c["pipeline"], c["mode"], c["action"]) for c in claims] == [
            ("A", "anonymise_restore", "hidden")
        ]
        assert "ladder" not in result and "policy_types" not in result
