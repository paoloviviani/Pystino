"""Privacy routing end to end (ADR 0113): the admin API, enforcement on reading
and embedding, and the attribution in the log and the per-response summary."""

from __future__ import annotations

import base64
import uuid
from typing import Any

import httpx
import pytest
from conftest import FakeUpstream, Seeded
from gateway.models import (
    DetectorProfile,
    GroupModelAccess,
    ModelDef,
    ModelKind,
    ModelPrice,
    PrivacyRule,
    Provider,
    UsageRecord,
    UsageStatus,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin
from test_embeddings import add_embedding_model, embedding_body
from test_ocr_surface import (
    INLINE_DOCX,
    FakeExtractor,
    add_local_model,
    add_ocr_model,
    ocr_response,
)

SUMMARY = {"x-pystino-redaction-summary": "1"}
PDF = "data:application/pdf;base64," + base64.b64encode(b"%PDF-1.4 fake").decode()
HEALTH_TEXT = "Il paziente presenta una diagnosi di diabete di tipo 2."
PLAIN_TEXT = "La riunione di lunedi parlera del budget trimestrale."


@pytest.fixture
async def admin(
    app: Any, seeded: Seeded, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    as_user(app, await make_admin(session_factory, seeded))


async def add_rule(
    app: Any,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    kind: str,
    name: str,
    conditions: list[dict[str, Any]] | None = None,
    action: dict[str, Any] | None = None,
    position: int = 0,
    enabled: bool = True,
    observe_only: bool = False,
) -> uuid.UUID:
    async with session_factory() as db:
        row = PrivacyRule(
            kind=kind,
            name=name,
            enabled=enabled,
            observe_only=observe_only,
            position=position,
            conditions=conditions or [],
            action=action or {"type": "refuse"},
        )
        db.add(row)
        await db.commit()
        rule_id = row.id
    await app.state.privacy.refresh()
    return rule_id


async def last_row(session_factory: async_sessionmaker[AsyncSession]) -> UsageRecord:
    async with session_factory() as db:
        return (
            (await db.execute(select(UsageRecord).order_by(UsageRecord.created_at.desc()).limit(1)))
            .scalars()
            .one()
        )


async def add_local_embedder(
    session: AsyncSession, seeded: Seeded, *, name: str = "embed-local", grant: bool = True
) -> ModelDef:
    provider = Provider(
        name="onsite",
        base_url=seeded.provider.base_url,
        plugin=seeded.provider.plugin,
        is_local=True,
    )
    session.add(provider)
    await session.flush()
    model = ModelDef(
        name=name, upstream_model="onsite/embed", provider_id=provider.id, kind=ModelKind.EMBEDDING
    )
    session.add(model)
    await session.flush()
    session.add(ModelPrice(model_id=model.id, input_per_mtok=0, output_per_mtok=0, currency="EUR"))
    if grant:
        session.add(GroupModelAccess(group_id=seeded.group.id, model_id=model.id))
    await session.commit()
    return model


# -- the admin API -----------------------------------------------------------------


class TestAdminApi:
    async def test_create_list_update_delete_with_audit(
        self, admin: None, client: httpx.AsyncClient
    ) -> None:
        created = await client.post(
            "/api/admin/privacy/rules",
            json={
                "kind": "read",
                "name": "Scans stay here",
                "conditions": [{"key": "filetype", "values": ["scan"]}],
                "action": {"type": "reader", "reader": "ocr"},
            },
        )
        assert created.status_code == 201, created.text
        rule = created.json()
        assert rule["position"] == 0 and rule["enabled"] is True

        listed = (await client.get("/api/admin/privacy/rules")).json()
        assert [r["name"] for r in listed["items"]] == ["Scans stay here"]

        patched = await client.patch(
            f"/api/admin/privacy/rules/{rule['id']}", json={"observe_only": True, "name": "Scans"}
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["observe_only"] is True

        assert (await client.delete(f"/api/admin/privacy/rules/{rule['id']}")).status_code == 204
        assert (await client.get("/api/admin/privacy/rules")).json()["total"] == 0

        audit = (
            await client.get("/api/admin/audit", params={"target_type": "privacy_rule"})
        ).json()
        assert [e["action"] for e in audit["items"]][::-1] == [
            "privacy_rule.create",
            "privacy_rule.update",
            "privacy_rule.delete",
        ]

    async def test_errors_are_422_and_say_what_is_wrong(
        self, admin: None, client: httpx.AsyncClient
    ) -> None:
        bad = await client.post(
            "/api/admin/privacy/rules",
            json={
                "kind": "read",
                "name": "Bad",
                "conditions": [{"key": "lang", "values": ["it"]}],
                "action": {"type": "refuse"},
            },
        )
        assert bad.status_code == 422
        assert "not a condition" in bad.json()["error"]["message"]
        chat = await client.post(
            "/api/admin/privacy/rules",
            json={"kind": "chat", "name": "Chat", "action": {"type": "refuse"}},
        )
        assert chat.status_code == 422

    async def test_duplicate_names_in_a_kind_conflict(
        self, admin: None, client: httpx.AsyncClient
    ) -> None:
        body = {"kind": "embed", "name": "Same", "action": {"type": "refuse"}}
        assert (await client.post("/api/admin/privacy/rules", json=body)).status_code == 201
        assert (await client.post("/api/admin/privacy/rules", json=body)).status_code == 409

    async def test_the_kind_cannot_change(self, admin: None, client: httpx.AsyncClient) -> None:
        rule = (
            await client.post(
                "/api/admin/privacy/rules",
                json={"kind": "embed", "name": "E", "action": {"type": "refuse"}},
            )
        ).json()
        response = await client.patch(
            f"/api/admin/privacy/rules/{rule['id']}", json={"kind": "read"}
        )
        # `kind` is not an updatable field: the request model refuses it.
        assert response.status_code in (400, 422)

    async def test_reorder_and_positions(self, admin: None, client: httpx.AsyncClient) -> None:
        ids = []
        for name in ("A", "B", "C"):
            created = await client.post(
                "/api/admin/privacy/rules",
                json={"kind": "embed", "name": name, "action": {"type": "refuse"}},
            )
            ids.append(created.json()["id"])
        inserted = await client.post(
            "/api/admin/privacy/rules",
            json={"kind": "embed", "name": "First", "action": {"type": "refuse"}, "position": 0},
        )
        assert inserted.json()["position"] == 0
        ordered = [ids[2], ids[0], ids[1], inserted.json()["id"]]
        response = await client.post(
            "/api/admin/privacy/rules/reorder", json={"kind": "embed", "order": ordered}
        )
        assert [r["name"] for r in response.json()["items"]] == ["C", "A", "B", "First"]
        short = await client.post(
            "/api/admin/privacy/rules/reorder", json={"kind": "embed", "order": ordered[:2]}
        )
        assert short.status_code == 422
        # A delete closes the gap.
        await client.delete(f"/api/admin/privacy/rules/{ids[2]}")
        positions = [
            r["position"] for r in (await client.get("/api/admin/privacy/rules")).json()["items"]
        ]
        assert positions == [0, 1, 2]

    async def test_warnings_come_back_with_the_rules_and_never_block(
        self, admin: None, client: httpx.AsyncClient
    ) -> None:
        await client.post(
            "/api/admin/privacy/rules",
            json={"kind": "read", "name": "Everything", "action": {"type": "refuse"}},
        )
        later = await client.post(
            "/api/admin/privacy/rules",
            json={
                "kind": "read",
                "name": "Later",
                "conditions": [{"key": "filetype", "values": ["pdf"]}],
                "action": {"type": "refuse"},
            },
        )
        assert later.status_code == 201
        codes = [i["code"] for i in later.json()["issues"]]
        assert "never_fires" in codes and "convenience_only" in codes

    async def test_the_draft_check_reports_shadowing_before_saving(
        self, admin: None, client: httpx.AsyncClient
    ) -> None:
        await client.post(
            "/api/admin/privacy/rules",
            json={"kind": "read", "name": "Everything", "action": {"type": "refuse"}},
        )
        checked = await client.post(
            "/api/admin/privacy/rules/check",
            json={
                "kind": "read",
                "conditions": [{"key": "filetype", "values": ["pdf"]}],
                "action": {"type": "refuse"},
            },
        )
        body = checked.json()
        assert body["error"] is None and body["soft_cap"] == 20
        assert "never_fires" in [i["code"] for i in body["issues"]]
        invalid = await client.post(
            "/api/admin/privacy/rules/check",
            json={
                "kind": "read",
                "conditions": [{"key": "filetype", "values": ["floppy"]}],
                "action": {"type": "refuse"},
            },
        )
        assert "File type" in invalid.json()["error"]

    async def test_the_soft_cap_warns_at_21(self, admin: None, client: httpx.AsyncClient) -> None:
        for n in range(21):
            created = await client.post(
                "/api/admin/privacy/rules",
                json={
                    "kind": "read",
                    "name": f"R{n}",
                    "conditions": [{"key": "filename", "values": [f"f{n}*"]}],
                    "action": {"type": "refuse"},
                },
            )
            assert created.status_code == 201
        issues = created.json()["issues"]
        assert "soft_cap" in [i["code"] for i in issues]

    async def test_a_non_admin_is_refused(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded, admin=False))
        assert (await client.get("/api/admin/privacy/rules")).status_code == 403
        assert (
            await client.post("/api/admin/privacy/test", json={"kind": "read"})
        ).status_code == 403

    async def test_the_test_endpoint_traces_which_rule_fires_and_why_not_the_earlier_ones(
        self, admin: None, client: httpx.AsyncClient
    ) -> None:
        await client.post(
            "/api/admin/privacy/rules",
            json={
                "kind": "read",
                "name": "Images",
                "conditions": [{"key": "filetype", "values": ["image"]}],
                "action": {"type": "refuse"},
            },
        )
        await client.post(
            "/api/admin/privacy/rules",
            json={
                "kind": "read",
                "name": "Medical PDFs",
                "conditions": [
                    {"key": "filetype", "values": ["pdf"]},
                    {"key": "filename", "values": ["referto*"]},
                ],
                "action": {"type": "reader", "reader": "docling"},
            },
        )
        result = (
            await client.post(
                "/api/admin/privacy/test",
                json={"kind": "read", "filename": "referto-rossi.pdf", "mime": "application/pdf"},
            )
        ).json()
        assert result["outcome"] == "reader" and result["fired"]["name"] == "Medical PDFs"
        first = result["steps"][0]
        assert first["name"] == "Images" and first["result"] == "not_matched"
        assert "is not image" in first["reasons"][0]
        assert result["facts"]["filetype"] == "pdf"

    async def test_the_test_endpoint_needs_a_file_for_a_scan_rule_and_never_echoes_text(
        self, admin: None, client: httpx.AsyncClient
    ) -> None:
        await client.post(
            "/api/admin/privacy/rules",
            json={
                "kind": "read",
                "name": "Scans",
                "conditions": [{"key": "filetype", "values": ["scan"]}],
                "action": {"type": "refuse"},
            },
        )
        pending = (
            await client.post("/api/admin/privacy/test", json={"kind": "read", "filename": "a.pdf"})
        ).json()
        assert pending["outcome"] == "pending" and pending["needs"] == "scan"
        decided = (
            await client.post(
                "/api/admin/privacy/test",
                json={"kind": "read", "filename": "a.pdf", "is_scan": True},
            )
        ).json()
        assert decided["outcome"] == "refuse"

        await client.post(
            "/api/admin/privacy/rules",
            json={
                "kind": "embed",
                "name": "Health",
                "conditions": [{"key": "verdict", "values": ["health"]}],
                "action": {"type": "refuse"},
            },
        )
        text = await client.post(
            "/api/admin/privacy/test", json={"kind": "embed", "text": HEALTH_TEXT}
        )
        assert text.json()["outcome"] == "refuse"
        assert text.json()["verdicts"]["health"] is True
        assert "diabete" not in text.text and "paziente" not in text.text

    async def test_the_test_box_reads_a_file_on_the_local_extractor(
        self, app: Any, admin: None, client: httpx.AsyncClient
    ) -> None:
        extractor = FakeExtractor()
        extractor.set(kind="no_text_layer", text="", extractor="markitdown", pages=2)
        app.state.control_http = extractor.client()
        await client.post(
            "/api/admin/privacy/rules",
            json={
                "kind": "read",
                "name": "Scans",
                "conditions": [{"key": "filetype", "values": ["scan"]}],
                "action": {"type": "refuse"},
            },
        )
        result = (
            await client.post(
                "/api/admin/privacy/test",
                json={
                    "kind": "read",
                    "filename": "a.pdf",
                    "mime": "application/pdf",
                    "file_base64": base64.b64encode(b"%PDF fake").decode(),
                },
            )
        ).json()
        assert result["outcome"] == "refuse" and result["facts"]["is_scan"] is True
        assert result["extractor"] == "markitdown"

    async def test_the_test_endpoint_is_rate_limited(
        self, app: Any, admin: None, client: httpx.AsyncClient
    ) -> None:
        app.state.redaction_test_limiter._max_calls = 2
        for _ in range(2):
            assert (
                await client.post("/api/admin/privacy/test", json={"kind": "read"})
            ).status_code == 200
        assert (
            await client.post("/api/admin/privacy/test", json={"kind": "read"})
        ).status_code == 400

    async def test_profiles_crud_fallbacks_and_guards(
        self,
        admin: None,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as db:
            fast = DetectorProfile(name="Fast", engine="presidio", labels=[], is_default=True)
            db.add(fast)
            await db.commit()
            fast_id = str(fast.id)
        listed = (await client.get("/api/admin/privacy/profiles")).json()["items"]
        assert listed[0]["name"] == "Fast" and listed[0]["status"] == "available"
        assert listed[0]["decides"] is True

        medical = await client.post(
            "/api/admin/privacy/profiles",
            json={
                "name": "Medical",
                "engine": "gliner",
                "labels": ["diagnosis"],
                "fallback": "fallback_to",
                "fallback_profile_id": fast_id,
            },
        )
        assert medical.status_code == 201, medical.text
        body = medical.json()
        assert body["status"] == "not_installed" and body["decides"] is False
        assert body["fallback_profile_name"] == "Fast" and body["labels"] == ["DIAGNOSIS"]

        loop = await client.patch(
            f"/api/admin/privacy/profiles/{fast_id}",
            json={"fallback": "fallback_to", "fallback_profile_id": body["id"]},
        )
        assert loop.status_code == 422
        assert (await client.delete(f"/api/admin/privacy/profiles/{fast_id}")).status_code == 409

        used = await client.post(
            "/api/admin/privacy/rules",
            json={
                "kind": "embed",
                "name": "Uses Medical",
                "conditions": [{"key": "verdict", "values": ["health"], "decide": body["id"]}],
                "action": {"type": "refuse"},
            },
        )
        assert used.status_code == 201
        assert {"unavailable_now", "weakens"} <= {i["code"] for i in used.json()["issues"]}
        assert (await client.delete(f"/api/admin/privacy/profiles/{body['id']}")).status_code == 409
        await client.delete(f"/api/admin/privacy/rules/{used.json()['id']}")
        assert (await client.delete(f"/api/admin/privacy/profiles/{body['id']}")).status_code == 204
        audit = (
            await client.get("/api/admin/audit", params={"target_type": "privacy_profile"})
        ).json()
        assert "privacy_profile.create" in [e["action"] for e in audit["items"]]

    async def test_is_local_is_an_administrators_statement(
        self, admin: None, client: httpx.AsyncClient
    ) -> None:
        created = await client.post(
            "/api/admin/providers",
            json={"name": "onsite", "base_url": "http://vllm:8000/v1", "is_local": True},
        )
        assert created.status_code == 201, created.text
        assert created.json()["is_local"] is True
        updated = await client.patch(
            f"/api/admin/providers/{created.json()['id']}", json={"is_local": False}
        )
        assert updated.json()["is_local"] is False


# -- embedding ---------------------------------------------------------------------


class TestEmbeddingRules:
    async def test_no_rules_changes_nothing_and_a_summary_is_not_sent_unless_asked(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_embedding_model(session, seeded)
        fake_upstream.set_json(embedding_body())
        plain = await client.post(
            "/v1/embeddings", json={"model": "embed-1", "input": "x"}, headers=seeded.auth
        )
        assert plain.status_code == 200 and "pystino_redaction" not in plain.json()
        asked = await client.post(
            "/v1/embeddings",
            json={"model": "embed-1", "input": "x"},
            headers={**seeded.auth, **SUMMARY},
        )
        # Redaction is off in the tests and no rule is configured: still absent.
        assert "pystino_redaction" not in asked.json()

    async def test_a_health_rule_refuses_with_a_403_naming_the_rule_and_never_the_text(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_embedding_model(session, seeded)
        await add_rule(
            app,
            session_factory,
            kind="embed",
            name="Health never leaves",
            conditions=[
                {"key": "verdict", "values": ["health"], "negate": False, "decide": "patterns"},
                {"key": "target", "values": ["cloud"], "negate": False},
            ],
        )
        fake_upstream.set_json(embedding_body())
        response = await client.post(
            "/v1/embeddings",
            json={"model": "embed-1", "input": [HEALTH_TEXT]},
            headers={**seeded.auth, **SUMMARY},
        )
        assert response.status_code == 403
        body = response.json()
        assert body["error"]["code"] == "routing_refused"
        assert "Health never leaves" in body["error"]["message"]
        assert "diabete" not in response.text and "paziente" not in response.text
        assert not fake_upstream.bodies, "nothing may reach the provider"
        assert body["pystino_redaction"]["blocked"] is True
        assert body["pystino_redaction"]["routing"]["handled"]["routing_rule"]["name"] == (
            "Health never leaves"
        )
        row = await last_row(session_factory)
        assert row.status is UsageStatus.BLOCKED and row.error_code == "routing_refused"
        assert row.redaction_detail["routing"]["outcome"] == "refused"
        assert HEALTH_TEXT not in str(row.redaction_detail)

        fine = await client.post(
            "/v1/embeddings", json={"model": "embed-1", "input": [PLAIN_TEXT]}, headers=seeded.auth
        )
        assert fine.status_code == 200

    async def test_local_reroutes_to_a_local_model_the_caller_may_use(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_embedding_model(session, seeded)
        await add_local_embedder(session, seeded)
        await add_rule(
            app,
            session_factory,
            kind="embed",
            name="Keep health here",
            conditions=[{"key": "verdict", "values": ["health"], "decide": "patterns"}],
            action={"type": "local"},
        )
        fake_upstream.set_json(embedding_body(model="onsite/embed"))
        response = await client.post(
            "/v1/embeddings",
            json={"model": "embed-1", "input": HEALTH_TEXT},
            headers={**seeded.auth, **SUMMARY},
        )
        assert response.status_code == 200, response.text
        assert fake_upstream.bodies[-1]["model"] == "onsite/embed"
        assert response.json()["model"] == "embed-1"
        routing = response.json()["pystino_redaction"]["routing"]
        assert routing["outcome"] == "rerouted"
        assert routing["observed"]["target"]["local"] is True
        row = await last_row(session_factory)
        assert row.model_name == "embed-local" and row.requested_model == "embed-1"

    async def test_local_without_a_local_provider_fails_closed(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_embedding_model(session, seeded)
        await add_rule(
            app, session_factory, kind="embed", name="Keep here", action={"type": "local"}
        )
        response = await client.post(
            "/v1/embeddings", json={"model": "embed-1", "input": "x"}, headers=seeded.auth
        )
        assert response.status_code == 403
        assert "Keep here" in response.json()["error"]["message"]
        assert not fake_upstream.bodies

    async def test_local_without_access_to_the_local_model_fails_closed(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_embedding_model(session, seeded)
        await add_local_embedder(session, seeded, grant=False)
        await add_rule(
            app, session_factory, kind="embed", name="Keep here", action={"type": "local"}
        )
        response = await client.post(
            "/v1/embeddings", json={"model": "embed-1", "input": "x"}, headers=seeded.auth
        )
        assert response.status_code == 403 and not fake_upstream.bodies

    async def test_a_request_already_on_a_local_model_is_just_served(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_local_embedder(session, seeded)
        await add_rule(
            app, session_factory, kind="embed", name="Keep here", action={"type": "local"}
        )
        fake_upstream.set_json(embedding_body(model="onsite/embed"))
        response = await client.post(
            "/v1/embeddings", json={"model": "embed-local", "input": "x"}, headers=seeded.auth
        )
        assert response.status_code == 200

    async def test_observe_only_logs_and_the_request_goes_through(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_embedding_model(session, seeded)
        await add_rule(
            app,
            session_factory,
            kind="embed",
            name="Would refuse",
            observe_only=True,
            conditions=[{"key": "verdict", "values": ["health"], "decide": "patterns"}],
        )
        fake_upstream.set_json(embedding_body())
        response = await client.post(
            "/v1/embeddings",
            json={"model": "embed-1", "input": HEALTH_TEXT},
            headers={**seeded.auth, **SUMMARY},
        )
        assert response.status_code == 200
        routing = response.json()["pystino_redaction"]["routing"]
        assert routing["observed_rules"][0]["name"] == "Would refuse"
        assert routing["handled"]["routing_rule"] is None
        assert routing["observed"]["verdicts"] == {
            "health": True,
            "ids": False,
            "lang": "it",
            "method": "patterns",
        }

    async def test_every_request_is_logged_once_routing_is_configured(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_embedding_model(session, seeded)
        fake_upstream.set_json(embedding_body())
        await client.post(
            "/v1/embeddings", json={"model": "embed-1", "input": "x"}, headers=seeded.auth
        )
        assert (await last_row(session_factory)).redaction_detail is None
        await add_rule(
            app,
            session_factory,
            kind="embed",
            name="Idle",
            conditions=[{"key": "who", "values": ["group:nobody"]}],
        )
        await client.post(
            "/v1/embeddings", json={"model": "embed-1", "input": "x"}, headers=seeded.auth
        )
        detail = (await last_row(session_factory)).redaction_detail
        assert detail["entities"] == [] and detail["routing"]["kind"] == "embed"
        assert detail["routing"]["handled"] == {
            "extractor": None,
            "fallback_reason": None,
            "reader": None,
            "profile": "default",
            "routing_rule": None,
        }
        assert detail["routing"]["observed"]["groups"] == ["research"]

    async def test_text_is_only_read_when_a_rule_needs_it(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_embedding_model(session, seeded)
        await add_rule(
            app,
            session_factory,
            kind="embed",
            name="Group",
            action={"type": "refuse"},
            conditions=[{"key": "who", "values": ["group:nobody"]}],
        )
        fake_upstream.set_json(embedding_body())
        before = app.state.privacy.cache.misses
        await client.post(
            "/v1/embeddings", json={"model": "embed-1", "input": HEALTH_TEXT}, headers=seeded.auth
        )
        assert app.state.privacy.cache.misses == before


# -- reading -----------------------------------------------------------------------


class TestReadingRules:
    async def test_a_file_name_rule_refuses_before_anything_is_read(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        extractor = FakeExtractor()
        app.state.control_http = extractor.client()
        model = await add_local_model(session, seeded)
        await add_rule(
            app,
            session_factory,
            kind="read",
            name="Medical files stay",
            conditions=[{"key": "filename", "values": ["referto*"]}],
        )
        response = await client.post(
            "/v1/ocr",
            json={
                "model": model.name,
                "document": {
                    "type": "document_url",
                    "document_url": PDF,
                    "document_name": "referto-rossi.pdf",
                },
            },
            headers={**seeded.auth, **SUMMARY},
        )
        assert response.status_code == 403
        body = response.json()
        assert body["error"]["code"] == "routing_refused"
        assert "Medical files stay" in body["error"]["message"]
        assert "referto-rossi" not in body["error"]["message"]
        assert not extractor.bodies
        row = await last_row(session_factory)
        assert row.status is UsageStatus.BLOCKED
        observed = row.redaction_detail["routing"]["observed"]
        assert "filename" not in observed
        assert observed["filename_match"]["pattern_index"] == 0

        other = await client.post(
            "/v1/ocr",
            json={
                "model": model.name,
                "document": {
                    "type": "document_url",
                    "document_url": PDF,
                    "document_name": "fattura.pdf",
                },
            },
            headers=seeded.auth,
        )
        assert other.status_code == 200

    async def test_the_uploaded_file_name_is_never_persisted(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """File names carry personal data; only the ephemeral summary may show one."""
        app.state.control_http = FakeExtractor().client()
        model = await add_local_model(session, seeded)
        rule_id = await add_rule(
            app,
            session_factory,
            kind="read",
            name="Observe names",
            observe_only=True,
            conditions=[{"key": "filename", "values": ["x*", "*mario-rossi*"]}],
        )
        name = "referto-mario-rossi.pdf"
        response = await client.post(
            "/v1/ocr",
            json={
                "model": model.name,
                "document": {"type": "document_url", "document_url": PDF, "document_name": name},
            },
            headers={**seeded.auth, **SUMMARY},
        )
        assert response.status_code == 200
        # The summary of this one response may name the file...
        assert response.json()["pystino_redaction"]["routing"]["observed"]["filename"] == name
        # ...nothing persisted may.
        async with session_factory() as db:
            rows = (await db.execute(select(UsageRecord))).scalars().all()
        dumped = " ".join(
            str({c.name: getattr(r, c.name) for c in UsageRecord.__table__.columns}) for r in rows
        )
        assert "mario" not in dumped and "referto-" not in dumped
        match = rows[-1].redaction_detail["routing"]["observed"]["filename_match"]
        assert match == {"rule_id": str(rule_id), "pattern_index": 1}

    async def test_a_reader_rule_passes_the_engine_to_the_extractor(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        extractor = FakeExtractor()
        extractor.set(
            kind="text", text="Hello", extractor="docling", pages=1, fallback_reason="docling busy"
        )
        app.state.control_http = extractor.client()
        model = await add_local_model(session, seeded)
        await add_rule(
            app,
            session_factory,
            kind="read",
            name="PDFs with Docling",
            conditions=[{"key": "filetype", "values": ["pdf"]}],
            action={"type": "reader", "reader": "docling"},
        )
        response = await client.post(
            "/v1/ocr",
            json={
                "model": model.name,
                "document": {"type": "document_url", "document_url": PDF, "document_name": "a.pdf"},
            },
            headers={**seeded.auth, **SUMMARY},
        )
        assert response.status_code == 200, response.text
        assert extractor.headers[0]["x-extractor-engine"] == "docling"
        assert extractor.headers[0]["x-filename"] == "a.pdf"
        routing = response.json()["pystino_redaction"]["routing"]
        assert routing["handled"] == {
            "extractor": "docling",
            "fallback_reason": "docling busy",
            "reader": "docling",
            "profile": "default",
            "routing_rule": {
                "id": routing["handled"]["routing_rule"]["id"],
                "name": "PDFs with Docling",
            },
        }
        assert (
            routing["observed"]["filetype"] == "pdf"
            and routing["observed"]["mime"] == "application/pdf"
        )
        row = await last_row(session_factory)
        assert row.redaction_detail["routing"]["handled"]["fallback_reason"] == "docling busy"

    async def test_without_rules_no_engine_header_is_sent(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        extractor = FakeExtractor()
        app.state.control_http = extractor.client()
        model = await add_local_model(session, seeded)
        response = await client.post(
            "/v1/ocr",
            json={
                "model": model.name,
                "document": {"type": "document_url", "document_url": INLINE_DOCX},
            },
            headers=seeded.auth,
        )
        assert response.status_code == 200
        assert "x-extractor-engine" not in extractor.headers[0]

    async def test_a_scan_rule_reads_first_then_refuses_and_nothing_leaves(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        fake_upstream: FakeUpstream,
    ) -> None:
        extractor = FakeExtractor()
        extractor.set(kind="no_text_layer", text="", extractor="markitdown", pages=3)
        app.state.control_http = extractor.client()
        local = await add_local_model(session, seeded)
        await add_ocr_model(session, seeded, name="cloud-reader")
        await add_rule(
            app,
            session_factory,
            kind="read",
            name="No scans to the cloud",
            conditions=[{"key": "filetype", "values": ["scan"]}],
        )
        # The caller named a cloud model; the document is read here to find out.
        response = await client.post(
            "/v1/ocr",
            json={
                "model": "cloud-reader",
                "document": {"type": "document_url", "document_url": PDF},
            },
            headers={**seeded.auth, **SUMMARY},
        )
        assert response.status_code == 403
        assert "No scans to the cloud" in response.json()["error"]["message"]
        assert len(extractor.bodies) == 1 and not fake_upstream.bodies
        routing = response.json()["pystino_redaction"]["routing"]
        assert routing["observed"]["is_scan"] is True and routing["outcome"] == "refused"
        assert local.name == "local-reader"

    async def test_a_scan_rule_reads_a_text_pdf_and_lets_it_through(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        extractor = FakeExtractor()
        app.state.control_http = extractor.client()
        model = await add_local_model(session, seeded)
        await add_rule(
            app,
            session_factory,
            kind="read",
            name="Scans refused",
            conditions=[{"key": "filetype", "values": ["scan"]}],
        )
        response = await client.post(
            "/v1/ocr",
            json={"model": model.name, "document": {"type": "document_url", "document_url": PDF}},
            headers={**seeded.auth, **SUMMARY},
        )
        assert response.status_code == 200
        routing = response.json()["pystino_redaction"]["routing"]
        assert routing["observed"]["is_scan"] is False and routing["outcome"] == "default"

    async def test_a_scan_rule_can_choose_a_different_reader_with_a_second_read(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        extractor = FakeExtractor()
        extractor.set(kind="text", text="scanned text", extractor="pypdfium2+rapidocr", pages=1)
        app.state.control_http = extractor.client()
        model = await add_local_model(session, seeded)
        await add_rule(
            app,
            session_factory,
            kind="read",
            name="Scans with Docling",
            conditions=[{"key": "filetype", "values": ["scan"]}],
            action={"type": "reader", "reader": "docling"},
        )
        response = await client.post(
            "/v1/ocr",
            json={"model": model.name, "document": {"type": "document_url", "document_url": PDF}},
            headers=seeded.auth,
        )
        assert response.status_code == 200
        assert len(extractor.headers) == 2
        assert "x-extractor-engine" not in extractor.headers[0]
        assert extractor.headers[1]["x-extractor-engine"] == "docling"

    async def test_a_reader_rule_overrides_a_cloud_model_when_a_local_reader_exists(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        fake_upstream: FakeUpstream,
    ) -> None:
        extractor = FakeExtractor()
        app.state.control_http = extractor.client()
        await add_local_model(session, seeded)
        await add_ocr_model(session, seeded, name="cloud-reader")
        await add_rule(
            app,
            session_factory,
            kind="read",
            name="PDFs here",
            conditions=[{"key": "filetype", "values": ["pdf"]}],
            action={"type": "reader", "reader": "markitdown"},
        )
        fake_upstream.set_json(ocr_response())
        response = await client.post(
            "/v1/ocr",
            json={
                "model": "cloud-reader",
                "document": {"type": "document_url", "document_url": PDF},
            },
            headers=seeded.auth,
        )
        assert response.status_code == 200
        assert extractor.bodies and not fake_upstream.bodies
        assert response.json()["model"] == "cloud-reader"
        row = await last_row(session_factory)
        assert row.requested_model == "cloud-reader" and row.model_name == "local-reader"

    async def test_a_reader_rule_on_a_link_for_a_cloud_model_fails_closed(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_local_model(session, seeded)
        await add_ocr_model(session, seeded, name="cloud-reader")
        await add_rule(
            app,
            session_factory,
            kind="read",
            name="PDFs here",
            conditions=[{"key": "filename", "values": ["*.pdf"]}],
            action={"type": "reader", "reader": "markitdown"},
        )
        response = await client.post(
            "/v1/ocr",
            json={
                "model": "cloud-reader",
                "document": {
                    "type": "document_url",
                    "document_url": "https://example.org/a.pdf",
                    "document_name": "a.pdf",
                },
            },
            headers=seeded.auth,
        )
        assert response.status_code == 403
        assert "PDFs here" in response.json()["error"]["message"]
        assert not fake_upstream.bodies

    async def test_a_cloud_model_with_no_matching_rule_is_untouched(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_ocr_model(session, seeded, name="cloud-reader")
        await add_rule(
            app,
            session_factory,
            kind="read",
            name="Word here",
            conditions=[{"key": "filetype", "values": ["word"]}],
            action={"type": "reader", "reader": "markitdown"},
        )
        fake_upstream.set_json(ocr_response())
        response = await client.post(
            "/v1/ocr",
            json={
                "model": "cloud-reader",
                "document": {"type": "document_url", "document_url": PDF},
            },
            headers={**seeded.auth, **SUMMARY},
        )
        assert response.status_code == 200 and fake_upstream.bodies
        routing = response.json()["pystino_redaction"]["routing"]
        assert routing["observed"]["target"]["local"] is False
        assert routing["handled"]["extractor"] is None

    async def test_the_users_redaction_page_carries_the_routing_fields(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded, admin=False))
        extractor = FakeExtractor()
        app.state.control_http = extractor.client()
        model = await add_local_model(session, seeded)
        await add_rule(
            app,
            session_factory,
            kind="read",
            name="PDFs with Docling",
            conditions=[{"key": "filetype", "values": ["pdf"]}],
            action={"type": "reader", "reader": "docling"},
        )
        await client.post(
            "/v1/ocr",
            json={"model": model.name, "document": {"type": "document_url", "document_url": PDF}},
            headers=seeded.auth,
        )
        events = (await client.get("/api/me/redaction/events")).json()["items"]
        assert len(events) == 1
        routing = events[0]["routing"]
        assert routing["kind"] == "read"
        assert routing["handled"]["routing_rule"]["name"] == "PDFs with Docling"
        assert routing["observed"]["filetype"] == "pdf"


# -- chat: observation only (phase 0) -----------------------------------------------


class TestChatObservation:
    async def test_chat_records_what_was_observed_and_changes_nothing(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_rule(
            app,
            session_factory,
            kind="embed",
            name="Idle",
            action={"type": "refuse"},
            conditions=[{"key": "who", "values": ["group:nobody"]}],
        )
        fake_upstream.set_json(
            {
                "id": "c1",
                "object": "chat.completion",
                "created": 1,
                "model": "vendor/x",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hi"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            }
        )
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hello"}]},
            headers={**seeded.auth, **SUMMARY},
        )
        assert response.status_code == 200, response.text
        summary = response.json()["pystino_redaction"]
        assert summary["applied"] is False and summary["entities"] == []
        routing = summary["routing"]
        assert routing["kind"] == "chat" and routing["handled"]["profile"] == "default"
        assert routing["handled"]["routing_rule"] is None
        assert routing["observed"]["target"]["model"] == "test-model"
        assert routing["observed"]["target"]["local"] is False
        detail = (await last_row(session_factory)).redaction_detail
        assert detail["routing"]["kind"] == "chat"

    async def test_chat_without_routing_configured_is_byte_for_byte_unchanged(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        fake_upstream: FakeUpstream,
    ) -> None:
        fake_upstream.set_json(
            {
                "id": "c1",
                "object": "chat.completion",
                "created": 1,
                "model": "vendor/x",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hi"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            }
        )
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hello"}]},
            headers={**seeded.auth, **SUMMARY},
        )
        assert "pystino_redaction" not in response.json()
        assert (await last_row(session_factory)).redaction_detail is None
