"""Privacy pipelines end to end (ADR 0113, section 0): the admin and user APIs, enforcement on
reading, embedding and chat, and the attribution in the log and the per-response summary.

The unit-level properties (the ladder, scope, the fold) are in ``test_privacy_combine.py``; the
migration is proved byte for byte in ``test_privacy_migration_contract.py``. What is left here is
what only a running application shows: that every pipeline in scope runs on a real request, that a
reroute follows the reader it lands on, that a user's pipeline can never loosen anything, and that
nothing refuses without naming the rule or persists a file's name.
"""

from __future__ import annotations

import base64
import uuid
from typing import Any

import httpx
import pytest
from conftest import FakeUpstream, Seeded
from gateway.config import EntityMode, EntityPolicy
from gateway.models import (
    ApiKey,
    DetectorProfile,
    Group,
    GroupModelAccess,
    ModelDef,
    ModelKind,
    ModelPrice,
    PrivacyPipeline,
    Provider,
    UsageRecord,
    UsageStatus,
    User,
)
from gateway.rate_limit import SlidingWindowLimiter
from privacy_helpers import (
    add_pipeline,
    add_policy,
    add_user_policy,
    catch_all,
    rule,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin
from test_embeddings import add_embedding_model, embedding_body
from test_ocr_surface import (
    FakeExtractor,
    add_local_model,
    add_ocr_model,
    ocr_response,
)
from test_redaction_http import FakeDetector

SUMMARY = {"x-pystino-redaction-summary": "1"}
PDF = "data:application/pdf;base64," + base64.b64encode(b"%PDF-1.4 fake").decode()
HEALTH_TEXT = "Il paziente presenta una diagnosi di diabete di tipo 2."
PLAIN_TEXT = "La riunione di lunedi parlera del budget trimestrale."
HEALTH = [{"key": "health"}]


@pytest.fixture
async def admin(
    app: Any, seeded: Seeded, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    as_user(app, await make_admin(session_factory, seeded))


async def last_row(session_factory: async_sessionmaker[AsyncSession]) -> UsageRecord:
    async with session_factory() as db:
        return (
            (await db.execute(select(UsageRecord).order_by(UsageRecord.created_at.desc()).limit(1)))
            .scalars()
            .one()
        )


async def embed(client: httpx.AsyncClient, seeded: Seeded, *texts: str, **headers: str) -> Any:
    return await client.post(
        "/v1/embeddings",
        json={"model": "embed-1", "input": list(texts)},
        headers={**seeded.auth, **headers},
    )


def ocr_body(
    model: str, document: str = PDF, document_name: str | None = None, **extra: Any
) -> dict[str, Any]:
    doc: dict[str, Any] = {"type": "document_url", "document_url": document}
    if document_name:
        doc["document_name"] = document_name
    return {"model": model, "document": doc, **extra}


async def ocr(
    client: httpx.AsyncClient, seeded: Seeded, model: str, document: str = PDF, **headers: str
) -> Any:
    return await client.post(
        "/v1/ocr",
        json=ocr_body(model, document),
        headers={**seeded.auth, **headers},
    )


# -- the admin API -----------------------------------------------------------------


class TestAdminApi:
    async def test_create_list_get_update_delete_with_audit(
        self, admin: None, client: httpx.AsyncClient, session_factory: Any
    ) -> None:
        created = await client.post(
            "/api/admin/privacy/pipelines",
            json={
                "kind": "embed",
                "name": "Remote embeddings",
                "rules": [
                    {
                        "name": "Health stays",
                        "conditions": [{"key": "health"}],
                        "action": {"level": "refuse"},
                    }
                ],
                "otherwise": {"level": "pass"},
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["position"] == 0 and body["enabled"] is True
        assert body["who"] == {"mode": "all", "ids": []}
        assert body["summary"] == ["refuses health data"]
        assert body["otherwise"]["name"] == "Everything else"

        listed = (await client.get("/api/admin/privacy/pipelines")).json()
        assert [p["name"] for p in listed["items"]] == ["Remote embeddings"]
        one = await client.get(f"/api/admin/privacy/pipelines/{body['id']}")
        assert one.status_code == 200 and one.json()["id"] == body["id"]

        rules = body["rules"]
        patched = await client.patch(
            f"/api/admin/privacy/pipelines/{body['id']}",
            json={
                "enabled": False,
                "name": "Remote",
                "rules": [
                    {
                        "id": rules[0]["id"],
                        "name": rules[0]["name"],
                        "observe_only": True,
                        "conditions": rules[0]["conditions"],
                        "action": {"level": "refuse"},
                    }
                ],
            },
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["enabled"] is False
        # The rule keeps its id across saves, so the log's rule id stays meaningful.
        assert patched.json()["rules"][0]["id"] == rules[0]["id"]

        assert (
            await client.delete(f"/api/admin/privacy/pipelines/{body['id']}")
        ).status_code == 204
        async with session_factory() as db:
            from gateway.models import AdminAuditEvent

            actions = {r.action for r in (await db.execute(select(AdminAuditEvent))).scalars()}
        assert {
            "privacy_pipeline.create",
            "privacy_pipeline.update",
            "privacy_pipeline.delete",
        } <= actions

    async def test_names_are_unique_per_kind_and_a_new_one_goes_last(
        self, admin: None, client: httpx.AsyncClient
    ) -> None:
        for name in ("A", "B"):
            await client.post("/api/admin/privacy/pipelines", json={"kind": "chat", "name": name})
        again = await client.post(
            "/api/admin/privacy/pipelines", json={"kind": "chat", "name": "a"}
        )
        assert again.status_code == 409
        other_kind = await client.post(
            "/api/admin/privacy/pipelines", json={"kind": "embed", "name": "a"}
        )
        assert other_kind.status_code == 201
        listed = (await client.get("/api/admin/privacy/pipelines", params={"kind": "chat"})).json()
        assert [(p["name"], p["position"]) for p in listed["items"]] == [("A", 0), ("B", 1)]

    async def test_reorder_needs_every_pipeline_once(
        self, admin: None, client: httpx.AsyncClient
    ) -> None:
        a = (
            await client.post("/api/admin/privacy/pipelines", json={"kind": "chat", "name": "A"})
        ).json()
        b = (
            await client.post("/api/admin/privacy/pipelines", json={"kind": "chat", "name": "B"})
        ).json()
        bad = await client.post(
            "/api/admin/privacy/pipelines/reorder", json={"kind": "chat", "order": [a["id"]]}
        )
        assert bad.status_code == 422
        ok = await client.post(
            "/api/admin/privacy/pipelines/reorder",
            json={"kind": "chat", "order": [b["id"], a["id"]]},
        )
        assert [p["name"] for p in ok.json()["items"]] == ["B", "A"]

    async def test_a_scope_that_names_nothing_that_exists_is_refused(
        self, admin: None, client: httpx.AsyncClient
    ) -> None:
        for body in (
            {"who": {"mode": "users", "ids": [str(uuid.uuid4())]}},
            {"who": {"mode": "groups", "ids": [str(uuid.uuid4())]}},
            {"target": {"mode": "only", "providers": [str(uuid.uuid4())]}},
            {"target": {"mode": "except", "models": [str(uuid.uuid4())]}},
        ):
            response = await client.post(
                "/api/admin/privacy/pipelines", json={"kind": "chat", "name": "P", **body}
            )
            assert response.status_code == 422, response.text

    async def test_a_scope_over_real_subjects_saves(
        self, admin: None, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        response = await client.post(
            "/api/admin/privacy/pipelines",
            json={
                "kind": "embed",
                "name": "Clinic, remote embedders",
                "who": {"mode": "groups", "ids": [str(seeded.group.id)]},
                "target": {"mode": "only", "providers": [str(seeded.provider.id)]},
            },
        )
        assert response.status_code == 201, response.text
        labels = response.json()["labels"]
        assert labels[str(seeded.group.id)] == seeded.group.name
        assert labels[str(seeded.provider.id)] == seeded.provider.name

    async def test_a_reader_and_a_policy_must_exist(
        self, admin: None, client: httpx.AsyncClient
    ) -> None:
        ghost = await client.post(
            "/api/admin/privacy/pipelines",
            json={
                "kind": "read",
                "name": "P",
                "otherwise": {"level": "read_with", "reader": "no-such-reader"},
            },
        )
        assert ghost.status_code == 422 and "not a configured OCR model" in ghost.text
        nopolicy = await client.post(
            "/api/admin/privacy/pipelines",
            json={
                "kind": "chat",
                "name": "P",
                "otherwise": {"level": "pass", "redact_with": str(uuid.uuid4())},
            },
        )
        assert nopolicy.status_code == 422 and "does not exist" in nopolicy.text

    async def test_the_fixed_last_rule_cannot_be_deleted_only_changed(
        self, admin: None, client: httpx.AsyncClient
    ) -> None:
        response = await client.post(
            "/api/admin/privacy/pipelines",
            json={"kind": "embed", "name": "P", "otherwise": {"level": "refuse"}},
        )
        body = response.json()
        assert body["rules"] == [] and body["otherwise"]["action"]["level"] == "refuse"
        again = await client.patch(f"/api/admin/privacy/pipelines/{body['id']}", json={"rules": []})
        assert again.json()["otherwise"]["action"]["level"] == "refuse"

    async def test_check_warns_without_blocking_and_reports_errors(
        self, admin: None, client: httpx.AsyncClient
    ) -> None:
        rules = [
            {
                "name": "All pdf",
                "conditions": [{"key": "filetype", "values": ["pdf"]}],
                "action": {"level": "refuse"},
            },
            {
                "name": "A pdf",
                "conditions": [{"key": "filetype", "values": ["pdf"]}],
                "action": {"level": "refuse"},
            },
        ]
        checked = (
            await client.post(
                "/api/admin/privacy/pipelines/check",
                json={"kind": "read", "name": "P", "rules": rules},
            )
        ).json()
        codes = {i["code"] for i in checked["issues"]}
        assert {"never_fires", "convenience_only"} <= codes and checked["error"] is None
        broken = (
            await client.post(
                "/api/admin/privacy/pipelines/check",
                json={
                    "kind": "embed",
                    "name": "P",
                    "rules": [
                        {
                            "name": "x",
                            "conditions": [{"key": "filename", "values": ["a"]}],
                            "action": {"level": "refuse"},
                        }
                    ],
                },
            )
        ).json()
        assert "not a condition" in broken["error"]

    async def test_options_list_what_the_editors_may_pick(
        self, admin: None, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        await add_local_model(session, seeded)
        options = (await client.get("/api/admin/privacy/options")).json()
        assert seeded.group.name in [g["name"] for g in options["groups"]]
        readers = {r["id"]: r for r in options["readers"]}
        assert readers["builtin"]["builtin"] is True and "local-reader" in readers
        assert options["engines"] == ["markitdown", "ocr", "docling"]

    async def test_there_is_no_local_flag_on_a_provider(
        self, admin: None, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        response = await client.get("/api/admin/providers")
        assert response.status_code == 200
        assert all("is_local" not in p for p in response.json()["items"])

    async def test_the_test_endpoint_is_rate_limited(
        self, app: Any, admin: None, client: httpx.AsyncClient
    ) -> None:
        app.state.privacy_test_limiter = SlidingWindowLimiter(max_calls=2, window_seconds=60)
        for _ in range(2):
            assert (
                await client.post("/api/admin/privacy/test", json={"kind": "read"})
            ).status_code == 200
        assert (
            await client.post("/api/admin/privacy/test", json={"kind": "read"})
        ).status_code == 400

    async def test_profiles_can_be_offered_to_users_and_guard_their_use(
        self, admin: None, client: httpx.AsyncClient, session_factory: Any
    ) -> None:
        fast = (
            await client.post(
                "/api/admin/privacy/profiles", json={"name": "Fast", "engine": "presidio"}
            )
        ).json()
        assert fast["user_available"] is False
        patched = await client.patch(
            f"/api/admin/privacy/profiles/{fast['id']}", json={"user_available": True}
        )
        assert patched.json()["user_available"] is True
        made = await client.post(
            "/api/admin/privacy/profiles",
            json={
                "name": "Medical",
                "engine": "gliner",
                "fallback": "fallback_to",
                "fallback_profile_id": fast["id"],
            },
        )
        assert made.status_code == 201 and made.json()["status"] == "not_installed"
        loop = await client.patch(
            f"/api/admin/privacy/profiles/{fast['id']}",
            json={"fallback": "fallback_to", "fallback_profile_id": made.json()["id"]},
        )
        assert loop.status_code == 422
        # A profile a pipeline's condition decides with cannot be deleted.
        await client.post(
            "/api/admin/privacy/pipelines",
            json={
                "kind": "embed",
                "name": "P",
                "rules": [
                    {
                        "name": "R",
                        "conditions": [{"key": "health", "decide": made.json()["id"]}],
                        "action": {"level": "refuse"},
                    }
                ],
            },
        )
        in_use = await client.delete(f"/api/admin/privacy/profiles/{made.json()['id']}")
        assert in_use.status_code == 409

    async def test_user_entity_types_are_a_setting(
        self, admin: None, client: httpx.AsyncClient
    ) -> None:
        assert (await client.get("/api/admin/privacy/settings")).json() == {
            "user_entity_types": None
        }
        put = await client.put(
            "/api/admin/privacy/settings", json={"user_entity_types": ["person", "IBAN_CODE"]}
        )
        assert put.json()["user_entity_types"] == ["IBAN_CODE", "PERSON"]


# -- embedding and rerank ----------------------------------------------------------


class TestEmbedding:
    async def test_a_pipeline_refuses_health_text_and_names_the_rule(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_embedding_model(session, seeded)
        await add_pipeline(
            app,
            session_factory,
            "embed",
            "Remote embeddings",
            rules=[rule("Health text", HEALTH)],
        )
        fake_upstream.set_json(embedding_body())

        refused = await embed(client, seeded, HEALTH_TEXT, **SUMMARY)

        assert refused.status_code == 403, refused.text
        error = refused.json()["error"]
        assert error["code"] == "routing_refused"
        assert "'Health text'" in error["message"] and "'Remote embeddings'" in error["message"]
        # The rule is named; the content never is.
        assert "diabete" not in refused.text and "paziente" not in refused.text
        assert fake_upstream.bodies == []
        row = await last_row(session_factory)
        assert row.status is UsageStatus.BLOCKED and row.cost == 0
        routing = row.redaction_detail["routing"]
        assert routing["outcome"] == "refused"
        assert routing["handled"]["routing_rule"]["name"] == "Health text"
        assert routing["handled"]["routing_rule"]["pipeline"] == "Remote embeddings"
        assert routing["handled"]["routing_rule"]["owner"] == "admin"
        # The summary in the 403 says the same.
        assert refused.json()["pystino_redaction"]["routing"]["outcome"] == "refused"

        ok = await embed(client, seeded, PLAIN_TEXT, **SUMMARY)
        assert ok.status_code == 200, ok.text
        assert ok.json()["pystino_redaction"]["routing"]["outcome"] == "default"

    async def test_there_is_no_reroute_for_embedding(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Another model breaks the vector space, so the provider stays the caller's choice."""
        await add_embedding_model(session, seeded)
        await add_pipeline(app, session_factory, "embed", "P", rules=[rule("Health", HEALTH)])
        fake_upstream.set_json(embedding_body())
        assert (await embed(client, seeded, HEALTH_TEXT)).status_code == 403
        assert fake_upstream.bodies == []

    async def test_every_input_is_judged_in_full(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_embedding_model(session, seeded)
        await add_pipeline(app, session_factory, "embed", "P", rules=[rule("Health", HEALTH)])
        fake_upstream.set_json(embedding_body())
        response = await embed(client, seeded, PLAIN_TEXT, PLAIN_TEXT, HEALTH_TEXT)
        assert response.status_code == 403

    async def test_a_pipeline_scoped_to_another_provider_does_not_apply(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        """A facility's own embedder is left out of the scope of the pipelines that guard
        remote providers: scope is how an administrator treats one differently."""
        await add_embedding_model(session, seeded)
        await add_pipeline(
            app,
            session_factory,
            "embed",
            "Guards remote providers",
            rules=[rule("Health", HEALTH)],
            target={"mode": "except", "providers": [str(seeded.provider.id)], "models": []},
        )
        fake_upstream.set_json(embedding_body())
        assert (await embed(client, seeded, HEALTH_TEXT)).status_code == 200

    async def test_every_pipeline_in_scope_runs_and_policies_add_up(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_embedding_model(session, seeded)
        app.state.redactor = FakeDetector(
            {"Mario Rossi": "PERSON", "mario@example.com": "EMAIL_ADDRESS"}
        ).redactor()
        person = await add_policy(
            app, session_factory, "Names", entities={"PERSON": EntityPolicy(mode=EntityMode.REDACT)}
        )
        email = await add_policy(
            app,
            session_factory,
            "Emails",
            entities={"EMAIL_ADDRESS": EntityPolicy(mode=EntityMode.REDACT)},
        )
        await add_pipeline(
            app,
            session_factory,
            "embed",
            "Names",
            otherwise={"level": "pass", "redact_with": str(person)},
        )
        await add_pipeline(
            app,
            session_factory,
            "embed",
            "Emails",
            otherwise={"level": "pass", "redact_with": str(email)},
            position=1,
        )
        fake_upstream.set_json(embedding_body())

        response = await embed(client, seeded, "Mario Rossi mario@example.com", **SUMMARY)

        assert response.status_code == 200, response.text
        sent = fake_upstream.bodies[-1]["input"][0]
        assert "Mario" not in sent and "mario@example.com" not in sent
        routing = response.json()["pystino_redaction"]["routing"]
        assert sorted(routing["handled"]["policies"]) == ["Emails", "Names"]
        assert {p["name"] for p in routing["pipelines"]} == {"Names", "Emails"}

    async def test_the_strictest_mode_wins_across_pipelines(
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
        soft = await add_policy(
            app,
            session_factory,
            "Soft",
            entities={"PERSON": EntityPolicy(mode=EntityMode.ANONYMISE_RESTORE)},
        )
        hard = await add_policy(
            app, session_factory, "Hard", entities={"PERSON": EntityPolicy(mode=EntityMode.BLOCK)}
        )
        for position, (name, pid) in enumerate((("Soft", soft), ("Hard", hard))):
            await add_pipeline(
                app,
                session_factory,
                "embed",
                name,
                otherwise={"level": "pass", "redact_with": str(pid)},
                position=position,
            )
        fake_upstream.set_json(embedding_body())
        response = await embed(client, seeded, "Mario Rossi")
        assert response.status_code == 403 and response.json()["error"]["code"] == "content_blocked"

    async def test_rerank_is_governed_by_the_embed_pipelines(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        from test_rerank import add_rerank_model

        await add_rerank_model(session, seeded)
        await add_pipeline(app, session_factory, "embed", "P", rules=[rule("Health", HEALTH)])
        response = await client.post(
            "/v1/rerank",
            json={"model": "rank-1", "query": "q", "documents": [PLAIN_TEXT, HEALTH_TEXT]},
            headers=seeded.auth,
        )
        assert response.status_code == 403, response.text
        assert fake_upstream.bodies == []

    async def test_an_unavailable_detector_profile_fails_closed(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_embedding_model(session, seeded)
        async with session_factory() as db:
            profile = DetectorProfile(name="Medical", engine="gliner", labels=[], fallback="refuse")
            db.add(profile)
            await db.commit()
            profile_id = str(profile.id)
        await add_pipeline(
            app,
            session_factory,
            "embed",
            "P",
            rules=[rule("Health", [{"key": "health", "decide": profile_id}])],
        )
        fake_upstream.set_json(embedding_body())
        # No health data in the text, but the detector is not there: the rule acts.
        refused = await embed(client, seeded, PLAIN_TEXT)
        assert refused.status_code == 403 and fake_upstream.bodies == []

    async def test_a_worker_that_never_loaded_the_pipelines_refuses(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        await add_embedding_model(session, seeded)
        app.state.privacy.loaded = False
        refused = await embed(client, seeded, PLAIN_TEXT)
        assert refused.status_code == 503
        assert refused.json()["error"]["code"] == "privacy_rules_unavailable"


# -- chat --------------------------------------------------------------------------


class TestChat:
    async def test_a_chat_pipeline_refuses_and_redacts_messages(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        from helpers import completion_body

        app.state.redactor = FakeDetector({"Mario Rossi": "PERSON"}).redactor()
        pid = await add_policy(
            app, session_factory, "Names", entities={"PERSON": EntityPolicy(mode=EntityMode.REDACT)}
        )
        await add_pipeline(
            app,
            session_factory,
            "chat",
            "Chat",
            rules=[rule("Health", HEALTH)],
            otherwise={"level": "pass", "redact_with": str(pid)},
        )
        fake_upstream.set_json(completion_body())

        def body(text: str) -> dict[str, Any]:
            return {"model": seeded.model.name, "messages": [{"role": "user", "content": text}]}

        refused = await client.post(
            "/v1/chat/completions", json=body(HEALTH_TEXT), headers=seeded.auth
        )
        assert refused.status_code == 403
        assert refused.json()["error"]["code"] == "routing_refused"
        assert fake_upstream.bodies == []

        ok = await client.post(
            "/v1/chat/completions", json=body("scrivi a Mario Rossi"), headers=seeded.auth
        )
        assert ok.status_code == 200, ok.text
        assert "Mario Rossi" not in str(fake_upstream.bodies[-1])


# -- reading -----------------------------------------------------------------------


class TestReading:
    async def setup_readers(
        self, session: AsyncSession, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> FakeExtractor:
        await add_ocr_model(session, seeded, name="mistral", upstream="mistral-ocr")
        await add_local_model(session, seeded, name="local-reader")
        fake_upstream.set_json(ocr_response(pages=1))
        extractor = FakeExtractor()
        return extractor

    async def test_refuse_by_file_type_names_the_rule_and_reads_nothing(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        extractor = await self.setup_readers(session, seeded, fake_upstream)
        app.state.control_http = extractor.client()
        await add_pipeline(
            app,
            session_factory,
            "read",
            "Reading",
            rules=[rule("No PDFs", [{"key": "filetype", "values": ["pdf"]}])],
        )
        refused = await ocr(client, seeded, "mistral", **SUMMARY)
        assert refused.status_code == 403, refused.text
        assert "'No PDFs'" in refused.json()["error"]["message"]
        assert fake_upstream.bodies == [] and extractor.bodies == []

    async def test_read_with_the_built_in_extractor_overrides_the_named_model(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        extractor = await self.setup_readers(session, seeded, fake_upstream)
        app.state.control_http = extractor.client()
        await add_pipeline(
            app,
            session_factory,
            "read",
            "Reports stay here",
            rules=[
                rule(
                    "Referto",
                    [{"key": "filename", "values": ["*referto*"]}],
                    level="read_with",
                    reader="builtin",
                    engine="ocr",
                )
            ],
        )
        response = await client.post(
            "/v1/ocr",
            json=ocr_body("mistral", document_name="Referto_Rossi.pdf"),
            headers={**seeded.auth, **SUMMARY},
        )
        assert response.status_code == 200, response.text
        assert fake_upstream.bodies == [], "the file never left the stack"
        assert len(extractor.bodies) == 1
        assert extractor.headers[0]["x-extractor-engine"] == "ocr"
        routing = response.json()["pystino_redaction"]["routing"]
        assert routing["outcome"] == "rerouted"
        assert routing["handled"]["engine"] == "ocr"
        # The log never keeps a file's name, only which condition matched.
        row = await last_row(session_factory)
        detail = row.redaction_detail["routing"]
        assert "filename" not in detail["observed"]
        assert "Referto_Rossi" not in str(row.redaction_detail)
        assert detail["observed"]["filename_match"]["pattern_index"] == 0

    async def test_read_with_another_ocr_provider_sends_the_file_whole_there(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        await self.setup_readers(session, seeded, fake_upstream)
        await add_ocr_model(session, seeded, name="facility", upstream="docling-serve")
        await add_pipeline(
            app,
            session_factory,
            "read",
            "To the facility",
            otherwise={"level": "read_with", "reader": "facility"},
        )
        response = await ocr(client, seeded, "mistral", **SUMMARY)
        assert response.status_code == 200, response.text
        assert fake_upstream.bodies[-1]["model"] == "docling-serve"
        assert response.json()["model"] == "mistral"

    async def test_a_reroute_runs_the_pipelines_that_guard_the_new_reader(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        await self.setup_readers(session, seeded, fake_upstream)
        facility = await add_ocr_model(session, seeded, name="facility", upstream="docling-serve")
        await add_pipeline(
            app,
            session_factory,
            "read",
            "To the facility",
            otherwise={"level": "read_with", "reader": "facility"},
            target={
                "mode": "only",
                "providers": [],
                "models": [str(seeded.model.id), str(uuid.uuid4())],
            }
            if False
            else {"mode": "all", "providers": [], "models": []},
        )
        await add_pipeline(
            app,
            session_factory,
            "read",
            "Guards the facility reader",
            otherwise={"level": "refuse"},
            target={"mode": "only", "providers": [], "models": [str(facility.id)]},
            position=1,
        )
        response = await ocr(client, seeded, "mistral")
        assert response.status_code == 403, response.text
        assert "Guards the facility reader" in response.json()["error"]["message"]
        assert fake_upstream.bodies == []

    async def test_read_with_fails_closed_when_it_cannot_be_done(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        await self.setup_readers(session, seeded, fake_upstream)
        await add_pipeline(
            app,
            session_factory,
            "read",
            "Built in only",
            otherwise={"level": "read_with", "reader": "builtin"},
        )
        # A link: the built-in extractor reads only documents sent with the request.
        link = await ocr(client, seeded, "mistral", "https://files.example/referto.pdf")
        assert link.status_code == 403, link.text
        assert "link" in link.json()["error"]["message"]
        assert fake_upstream.bodies == []

    async def test_a_reader_the_caller_is_not_granted_fails_closed(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        await self.setup_readers(session, seeded, fake_upstream)
        async with session_factory() as db:
            ungranted = ModelDef(
                name="secret-reader",
                upstream_model="x",
                provider_id=seeded.provider.id,
                kind=ModelKind.OCR,
            )
            db.add(ungranted)
            await db.commit()
        await add_pipeline(
            app,
            session_factory,
            "read",
            "To the secret reader",
            otherwise={"level": "read_with", "reader": "secret-reader"},
        )
        refused = await ocr(client, seeded, "mistral")
        assert refused.status_code == 403, refused.text
        assert "not available to you" in refused.json()["error"]["message"]

    async def test_a_scan_is_found_by_reading_on_the_built_in_extractor_first(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        extractor = await self.setup_readers(session, seeded, fake_upstream)
        extractor.set(kind="no_text_layer", text="", extractor="markitdown", pages=2)
        app.state.control_http = extractor.client()
        await add_pipeline(
            app,
            session_factory,
            "read",
            "Scans",
            rules=[rule("Scans are refused", [{"key": "scan"}])],
        )
        refused = await ocr(client, seeded, "mistral")
        assert refused.status_code == 403, refused.text
        assert fake_upstream.bodies == [], "the probe read happened here, nothing left the stack"
        assert len(extractor.bodies) == 1

        extractor.set(kind="text", text="plain", extractor="markitdown", pages=0)
        app.state.control_http = extractor.client()
        ok = await ocr(client, seeded, "mistral")
        assert ok.status_code == 200, ok.text
        assert fake_upstream.bodies, "not a scan: the named reader reads it, whole"

    async def test_a_scan_rule_with_no_built_in_reader_available_fails_closed(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_ocr_model(session, seeded, name="mistral", upstream="mistral-ocr")
        fake_upstream.set_json(ocr_response(pages=1))
        await add_pipeline(
            app,
            session_factory,
            "read",
            "Scans",
            rules=[rule("Scans are refused", [{"key": "scan"}], level="pass" and "refuse")],
        )
        refused = await ocr(client, seeded, "mistral")
        assert refused.status_code == 403
        assert fake_upstream.bodies == []

    async def test_the_administrators_reader_holds_over_a_users(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        extractor = await self.setup_readers(session, seeded, fake_upstream)
        app.state.control_http = extractor.client()
        await add_ocr_model(session, seeded, name="facility", upstream="docling-serve")
        await add_pipeline(
            app,
            session_factory,
            "read",
            "Admin",
            otherwise={"level": "read_with", "reader": "facility"},
        )
        await add_pipeline(
            app,
            session_factory,
            "read",
            "Mine",
            owner=seeded.user.id,
            otherwise={"level": "read_with", "reader": "builtin", "engine": "ocr"},
        )
        response = await ocr(client, seeded, "mistral", **SUMMARY)
        assert response.status_code == 200, response.text
        assert fake_upstream.bodies[-1]["model"] == "docling-serve"
        assert extractor.bodies == []
        routing = response.json()["pystino_redaction"]["routing"]
        assert routing["handled"]["routing_rule"]["owner"] == "admin"

    async def test_a_user_reader_and_engine_apply_when_no_administrator_chose(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Mistral OCR is the default reader; the user's `*referto*` rule reads theirs here."""
        extractor = await self.setup_readers(session, seeded, fake_upstream)
        app.state.control_http = extractor.client()
        await add_pipeline(
            app,
            session_factory,
            "read",
            "Mine",
            owner=seeded.user.id,
            rules=[
                rule(
                    "My reports",
                    [{"key": "filename", "values": ["*referto*"]}],
                    level="read_with",
                    reader="builtin",
                    engine="docling",
                )
            ],
        )
        response = await client.post(
            "/v1/ocr",
            json=ocr_body("mistral", document_name="referto.pdf"),
            headers=seeded.auth,
        )
        assert response.status_code == 200, response.text
        assert extractor.headers[0]["x-extractor-engine"] == "docling"
        assert fake_upstream.bodies == []

    async def test_a_policy_screens_the_text_that_comes_back(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        await self.setup_readers(session, seeded, fake_upstream)
        app.state.redactor = FakeDetector({"Luca Bianchi": "PERSON"}).redactor()
        fake_upstream.set_json(
            {**ocr_response(pages=1), "pages": [{"index": 0, "markdown": "Fattura Luca Bianchi"}]}
        )
        await catch_all(
            app, session_factory, kinds=("read",), PERSON=EntityPolicy(mode=EntityMode.REDACT)
        )
        response = await ocr(client, seeded, "mistral")
        assert response.status_code == 200
        assert "Luca Bianchi" not in response.text
        assert "<PERSON>" in response.json()["pages"][0]["markdown"]

    async def test_the_403_never_names_the_file(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        await self.setup_readers(session, seeded, fake_upstream)
        await add_pipeline(
            app,
            session_factory,
            "read",
            "P",
            rules=[rule("By name", [{"key": "filename", "values": ["*rossi*"]}])],
        )
        response = await client.post(
            "/v1/ocr",
            json=ocr_body("mistral", document_name="Mario_Rossi_referto.pdf"),
            headers={**seeded.auth, **SUMMARY},
        )
        assert response.status_code == 403
        assert "Rossi" not in response.text
        row = await last_row(session_factory)
        assert "Rossi" not in str(row.redaction_detail) and "Rossi" not in (row.error_message or "")


# -- user pipelines ----------------------------------------------------------------


class TestUserPipelines:
    async def test_a_users_pipeline_is_theirs_alone_whatever_the_body_says(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        as_user(app, seeded.user)
        created = await client.post(
            "/api/me/privacy/pipelines",
            json={
                "kind": "embed",
                "name": "Mine",
                "who": {"mode": "all", "ids": []},
                "rules": [
                    {
                        "name": "Health",
                        "conditions": [{"key": "health"}],
                        "action": {"level": "refuse"},
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["who"] == {"mode": "users", "ids": [str(seeded.user.id)]}
        assert body["owner_id"] == str(seeded.user.id)

    async def test_nothing_that_loosens_can_be_written(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        as_user(app, seeded.user)
        for kind, action in (("embed", {"level": "pass"}), ("read", {"level": "default"})):
            response = await client.post(
                "/api/me/privacy/pipelines",
                json={
                    "kind": kind,
                    "name": "Nothing",
                    "rules": [{"name": "R", "conditions": [], "action": action}],
                },
            )
            assert response.status_code == 422, response.text
            assert "add protection" in response.text
        # "Everything else" may add nothing.
        ok = await client.post(
            "/api/me/privacy/pipelines",
            json={"kind": "embed", "name": "Empty", "otherwise": {"level": "pass"}},
        )
        assert ok.status_code == 201

    async def test_a_user_may_apply_an_administrators_policy_or_their_own_never_anothers(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: Any,
    ) -> None:
        as_user(app, seeded.user)
        admin_policy = await add_policy(
            app, session_factory, "Shared", entities={"PERSON": EntityPolicy()}
        )
        other = await _other_user(session_factory, seeded)
        theirs = await add_user_policy(
            app, session_factory, other.id, [{"kind": "mode", "name": "PERSON", "mode": "block"}]
        )

        def pipeline(policy_id: uuid.UUID) -> dict[str, Any]:
            return {
                "kind": "chat",
                "name": f"P{policy_id.hex[:4]}",
                "otherwise": {"level": "pass", "redact_with": str(policy_id)},
            }

        assert (
            await client.post("/api/me/privacy/pipelines", json=pipeline(admin_policy))
        ).status_code == 201
        mine = (
            await client.post(
                "/api/me/privacy/policies",
                json={
                    "name": "Mine",
                    "entries": [{"kind": "mode", "name": "IBAN_CODE", "mode": "redact"}],
                },
            )
        ).json()
        assert (
            await client.post("/api/me/privacy/pipelines", json=pipeline(uuid.UUID(mine["id"])))
        ).status_code == 201
        refused = await client.post("/api/me/privacy/pipelines", json=pipeline(theirs))
        assert refused.status_code == 422 and "does not exist" in refused.text

    async def test_a_user_may_name_only_readers_they_are_granted(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
    ) -> None:
        await add_ocr_model(session, seeded, name="mistral", upstream="m")
        async with session_factory() as db:
            db.add(
                ModelDef(
                    name="secret-reader",
                    upstream_model="x",
                    provider_id=seeded.provider.id,
                    kind=ModelKind.OCR,
                )
            )
            await db.commit()
        as_user(app, seeded.user)

        def read_with(reader: str) -> dict[str, Any]:
            return {
                "kind": "read",
                "name": f"R-{reader}",
                "otherwise": {"level": "read_with", "reader": reader},
            }

        assert (
            await client.post("/api/me/privacy/pipelines", json=read_with("mistral"))
        ).status_code == 201
        denied = await client.post("/api/me/privacy/pipelines", json=read_with("secret-reader"))
        assert denied.status_code == 422 and "not a reader you can use" in denied.text
        options = (await client.get("/api/me/privacy/options")).json()
        assert [r["id"] for r in options["readers"]] == ["mistral"]

    async def test_own_policies_use_only_what_the_administrator_made_available(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        as_user(app, seeded.user)
        async with session_factory() as db:
            from gateway.models import PrivacySettings

            db.add(PrivacySettings(id=1, user_entity_types=["PERSON"]))
            await db.commit()

        def policy(name: str, kind: str, type_: str, **extra: Any) -> dict[str, Any]:
            return {
                "name": name,
                "entries": [{"kind": kind, "name": type_, "mode": "redact", **extra}],
            }

        ok = await client.post("/api/me/privacy/policies", json=policy("A", "mode", "PERSON"))
        assert ok.status_code == 201, ok.text
        no = await client.post("/api/me/privacy/policies", json=policy("B", "mode", "IBAN_CODE"))
        assert no.status_code == 422 and "makes available" in no.text
        # Patterns are the user's own and are validated as the administrators' are.
        bad = await client.post(
            "/api/me/privacy/policies", json=policy("C", "pattern", "MINE", regex="(?=x)")
        )
        assert bad.status_code == 422
        good = await client.post(
            "/api/me/privacy/policies", json=policy("D", "pattern", "MINE", regex=r"PRJ-\d+")
        )
        assert good.status_code == 201
        off = await client.post(
            "/api/me/privacy/policies",
            json={"name": "E", "entries": [{"kind": "mode", "name": "PERSON", "mode": "off"}]},
        )
        assert off.status_code == 422
        # Profiles: only ones an administrator offered.
        async with session_factory() as db:
            profile = DetectorProfile(name="Fast", engine="presidio", labels=[], fallback="refuse")
            db.add(profile)
            await db.commit()
            pid = str(profile.id)
        denied = await client.post(
            "/api/me/privacy/policies", json={**policy("F", "mode", "PERSON"), "profile_id": pid}
        )
        assert denied.status_code == 422

    async def test_nobody_reads_or_changes_another_users_pipelines(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        other = await _other_user(session_factory, seeded)
        theirs = await add_pipeline(
            app,
            session_factory,
            "chat",
            "Theirs",
            owner=other.id,
            otherwise={"level": "refuse"},
        )
        as_user(app, seeded.user)
        assert (await client.get("/api/me/privacy/pipelines")).json()["items"] == []
        for method in ("get", "delete"):
            response = await getattr(client, method)(f"/api/me/privacy/pipelines/{theirs}")
            assert response.status_code == 404
        patched = await client.patch(f"/api/me/privacy/pipelines/{theirs}", json={"enabled": False})
        assert patched.status_code == 404

    async def test_a_policy_a_pipeline_applies_cannot_be_deleted(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        as_user(app, seeded.user)
        mine = (
            await client.post(
                "/api/me/privacy/policies",
                json={
                    "name": "Mine",
                    "entries": [{"kind": "mode", "name": "PERSON", "mode": "redact"}],
                },
            )
        ).json()
        await client.post(
            "/api/me/privacy/pipelines",
            json={
                "kind": "chat",
                "name": "P",
                "otherwise": {"level": "pass", "redact_with": mine["id"]},
            },
        )
        assert (await client.delete(f"/api/me/privacy/policies/{mine['id']}")).status_code == 409

    async def test_the_in_force_list_is_the_administrators_pipelines_that_cover_the_user(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        other = await _other_user(session_factory, seeded)
        await add_pipeline(app, session_factory, "chat", "For all")
        await add_pipeline(
            app,
            session_factory,
            "chat",
            "For me",
            who={"mode": "users", "ids": [str(seeded.user.id), str(other.id)]},
            position=1,
        )
        await add_pipeline(
            app,
            session_factory,
            "chat",
            "For someone else",
            who={"mode": "users", "ids": [str(other.id)]},
            position=2,
        )
        await add_pipeline(app, session_factory, "chat", "Off", enabled=False, position=3)
        await add_pipeline(
            app, session_factory, "chat", "Not an admin's", owner=other.id, position=4
        )
        as_user(app, seeded.user)
        items = (await client.get("/api/me/privacy/in-force")).json()["items"]
        assert [p["name"] for p in items] == ["For all", "For me"]
        # Who else is named is not the user's to read.
        assert str(other.id) not in items[1]["labels"]

    async def test_a_users_pipeline_raises_protection_and_never_lowers_an_administrators(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_embedding_model(session, seeded)
        fake_upstream.set_json(embedding_body())
        # Their own pipeline refuses health data: it applies to them.
        await add_pipeline(
            app,
            session_factory,
            "embed",
            "Mine",
            owner=seeded.user.id,
            rules=[rule("Health", HEALTH)],
        )
        assert (await embed(client, seeded, HEALTH_TEXT)).status_code == 403
        assert (await embed(client, seeded, PLAIN_TEXT)).status_code == 200
        # An administrator refuses everything; theirs, at the base level, cannot undo it.
        await add_pipeline(
            app, session_factory, "embed", "Admin", otherwise={"level": "refuse"}, position=5
        )
        response = await embed(client, seeded, PLAIN_TEXT, **SUMMARY)
        assert response.status_code == 403
        assert (
            response.json()["pystino_redaction"]["routing"]["handled"]["routing_rule"]["owner"]
            == "admin"
        )

    async def test_a_user_pipeline_that_loosens_in_the_database_is_not_served(
        self, app: Any, session_factory: Any, seeded: Seeded
    ) -> None:
        """Enforced when read as well as when saved: a hand-edited row is skipped, with a log."""
        async with session_factory() as db:
            db.add(
                PrivacyPipeline(
                    owner_id=seeded.user.id,
                    kind="embed",
                    name="Hand-edited",
                    position=0,
                    who={"mode": "all", "ids": []},
                    target={"mode": "all", "providers": [], "models": []},
                    rules=[],
                    otherwise={"level": "no-such-level"},
                )
            )
            await db.commit()
        await app.state.privacy.refresh()
        assert app.state.privacy.pipelines == ()
        assert app.state.privacy.bad_rows

    async def test_the_test_box_is_locked_to_the_user_and_leaks_nothing_of_others(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
    ) -> None:
        other = await _other_user(session_factory, seeded)
        secret_group = Group(name="secret-oncology")
        session.add(secret_group)
        await session.commit()
        await add_pipeline(
            app,
            session_factory,
            "chat",
            "Theirs by user",
            who={"mode": "users", "ids": [str(other.id)]},
            otherwise={"level": "refuse"},
        )
        await add_pipeline(
            app,
            session_factory,
            "chat",
            "Theirs by group",
            who={"mode": "groups", "ids": [str(secret_group.id)]},
            otherwise={"level": "refuse"},
            position=1,
        )
        await add_pipeline(
            app,
            session_factory,
            "chat",
            "Another user's own",
            owner=other.id,
            otherwise={"level": "refuse"},
            position=2,
        )
        await add_pipeline(app, session_factory, "chat", "For all", position=3)
        as_user(app, seeded.user)
        # Pretending to be the other user is not possible: user_id in the body is ignored.
        result = await client.post(
            "/api/me/privacy/test",
            json={"kind": "chat", "user_id": str(other.id), "text": "x"},
        )
        body = result.json()
        assert body["outcome"] == "pass"
        assert [p["name"] for p in body["pipelines"]] == ["For all"]
        # Neither run, nor "not in scope": other people's pipelines are not in the answer at all.
        assert body["skipped"] == []
        for hidden in (
            "Theirs by user",
            "Theirs by group",
            "Another user's own",
            "secret-oncology",
        ):
            assert hidden not in result.text

    async def test_the_administrators_test_box_still_shows_everything(
        self, app: Any, admin: None, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        other = await _other_user(session_factory, seeded)
        await add_pipeline(
            app, session_factory, "chat", "Not mine", who={"mode": "users", "ids": [str(other.id)]}
        )
        body = (
            await client.post(
                "/api/admin/privacy/test", json={"kind": "chat", "user_id": str(seeded.user.id)}
            )
        ).json()
        assert [s["name"] for s in body["skipped"]] == ["Not mine"]

    async def test_the_test_box_is_rate_limited(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        as_user(app, seeded.user)
        for _ in range(30):
            assert (
                await client.post("/api/me/privacy/test", json={"kind": "chat"})
            ).status_code == 200
        over = await client.post("/api/me/privacy/test", json={"kind": "chat"})
        assert over.status_code == 429


async def _other_user(session_factory: Any, seeded: Seeded) -> User:
    async with session_factory() as db:
        user = User(
            issuer="https://idp.test",
            subject=f"other-{uuid.uuid4().hex[:6]}",
            email=f"other-{uuid.uuid4().hex[:6]}@example.org",
            default_billing_group_id=seeded.group.id,
        )
        db.add(user)
        await db.commit()
        return user


# -- the test box ------------------------------------------------------------------


class TestTheTestBox:
    async def test_it_shows_pipelines_in_scope_the_ladder_and_the_policy_table(
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
        soft = await add_policy(
            app,
            session_factory,
            "Soft",
            entities={"PERSON": EntityPolicy(mode=EntityMode.ANONYMISE_RESTORE)},
        )
        hard = await add_policy(
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
            rules=[rule("Never", [{"key": "lang", "values": ["de"]}])],
            otherwise={"level": "pass", "redact_with": str(hard)},
            position=1,
        )
        await add_pipeline(
            app,
            session_factory,
            "embed",
            "Elsewhere",
            otherwise={"level": "refuse"},
            target={"mode": "only", "providers": [str(uuid.uuid4())], "models": []},
            position=2,
        )
        result = (
            await client.post(
                "/api/admin/privacy/test",
                json={
                    "kind": "embed",
                    "user_id": str(seeded.user.id),
                    "model_id": str(model.id),
                    "text": "Mario Rossi",
                },
            )
        ).json()
        assert result["outcome"] == "redact" and result["level"] == "pass"
        assert [p["name"] for p in result["pipelines"]] == ["A", "B"]
        assert result["pipelines"][1]["fired_otherwise"] is True
        assert [s["name"] for s in result["skipped"]] == ["Elsewhere"]
        assert [lv["level"] for lv in result["ladder"]] == ["pass", "refuse"]
        types = {t["entity_type"]: t for t in result["policy_types"]}
        assert types["PERSON"]["mode"] == "redact" and types["PERSON"]["source"] == "Hard"
        assert types["PERSON"]["found"] is True
        assert sorted(result["policies"]) == ["Hard", "Soft"]

    async def test_it_names_the_refusing_rule_and_never_echoes_the_text(
        self,
        app: Any,
        admin: None,
        client: httpx.AsyncClient,
        session_factory: Any,
    ) -> None:
        await add_pipeline(app, session_factory, "embed", "Remote", rules=[rule("Health", HEALTH)])
        result = await client.post(
            "/api/admin/privacy/test", json={"kind": "embed", "text": HEALTH_TEXT}
        )
        body = result.json()
        assert body["outcome"] == "refuse"
        assert body["winner_rule"] == "Health" and body["winner_pipeline"] == "Remote"
        assert body["verdicts"]["health"] is True
        assert "diabete" not in result.text and "paziente" not in result.text

    async def test_it_follows_a_reroute_and_names_who_chose_the_reader(
        self,
        app: Any,
        admin: None,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
    ) -> None:
        await add_local_model(session, seeded, name="local-reader")
        mistral = await add_ocr_model(session, seeded, name="mistral", upstream="m")
        await add_pipeline(
            app,
            session_factory,
            "read",
            "Admin",
            otherwise={"level": "read_with", "reader": "builtin", "engine": "docling"},
        )
        await add_pipeline(
            app,
            session_factory,
            "read",
            "Mine",
            owner=seeded.user.id,
            otherwise={"level": "read_with", "reader": "builtin", "engine": "ocr"},
        )
        result = (
            await client.post(
                "/api/admin/privacy/test",
                json={
                    "kind": "read",
                    "user_id": str(seeded.user.id),
                    "model_id": str(mistral.id),
                    "filename": "a.pdf",
                    "mime": "application/pdf",
                },
            )
        ).json()
        assert result["outcome"] == "read_with"
        assert result["engine"] == "docling" and "administrator" in result["engine_by"]
        assert [h["reader"] for h in result["hops"]] == ["builtin"]

    async def test_it_reads_a_file_on_the_built_in_extractor(
        self,
        app: Any,
        admin: None,
        client: httpx.AsyncClient,
        session_factory: Any,
    ) -> None:
        extractor = FakeExtractor()
        extractor.set(kind="no_text_layer", text="", extractor="markitdown", pages=2)
        app.state.control_http = extractor.client()
        await add_pipeline(
            app, session_factory, "read", "Scans", rules=[rule("Scans", [{"key": "scan"}])]
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
        waiting = (
            await client.post("/api/admin/privacy/test", json={"kind": "read", "filename": "a.pdf"})
        ).json()
        assert waiting["outcome"] == "pending" and waiting["needs"] == "scan"


# -- the up-front summary ------------------------------------------------------------


class TestSummary:
    async def test_it_lists_what_applies_per_embedding_and_chat_model(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
    ) -> None:
        await add_embedding_model(session, seeded)
        other = Provider(
            name="own-tei", base_url=seeded.provider.base_url, plugin=seeded.provider.plugin
        )
        session.add(other)
        await session.flush()
        own = ModelDef(
            name="bge-own", upstream_model="bge", provider_id=other.id, kind=ModelKind.EMBEDDING
        )
        session.add(own)
        await session.flush()
        session.add(
            ModelPrice(model_id=own.id, input_per_mtok=0, output_per_mtok=0, currency="EUR")
        )
        session.add(GroupModelAccess(group_id=seeded.group.id, model_id=own.id))
        await session.commit()
        policy = await add_policy(
            app, session_factory, "Default", entities={"PERSON": EntityPolicy()}
        )
        await add_pipeline(
            app,
            session_factory,
            "embed",
            "Remote embeddings",
            rules=[rule("Health", HEALTH)],
            otherwise={"level": "pass", "redact_with": str(policy)},
            target={"mode": "only", "providers": [str(seeded.provider.id)], "models": []},
        )
        await add_pipeline(
            app,
            session_factory,
            "embed",
            "Someone else's",
            otherwise={"level": "refuse"},
            who={"mode": "users", "ids": [str(uuid.uuid4())]},
            position=1,
        )
        await add_pipeline(
            app,
            session_factory,
            "embed",
            "Mine",
            owner=seeded.user.id,
            rules=[rule("My IDs", [{"key": "ids"}])],
            position=2,
        )

        body = (await client.get("/v1/pystino/privacy", headers=seeded.auth)).json()

        by_model = {m["model"]: m for m in body["embedding"]}
        assert by_model["embed-1"]["summary"] == [
            "refuses health data",
            "redacts with Default",
            "refuses IDs",
        ]
        assert by_model["embed-1"]["pipelines"] == ["Remote embeddings", "Mine"]
        # The own embedder is outside the remote pipeline's target: only the user's own applies.
        assert by_model["bge-own"]["summary"] == ["refuses IDs"]
        assert [m["model"] for m in body["chat"]] == [seeded.model.name]
        assert body["chat"][0]["summary"] == []

    async def test_it_needs_a_credential(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/v1/pystino/privacy")).status_code == 401


# -- review fixes ------------------------------------------------------------------------


class TestUndecidableOnTheRequestPath:
    async def pipeline_with_a_down_detector(
        self, app: Any, session_factory: Any, kind: str
    ) -> None:
        async with session_factory() as db:
            profile = DetectorProfile(name="Medical", engine="gliner", labels=[], fallback="refuse")
            db.add(profile)
            await db.commit()
            profile_id = str(profile.id)
        await add_pipeline(
            app,
            session_factory,
            kind,
            "Remote",
            rules=[
                rule(
                    "No health, pass",
                    [{"key": "health", "negate": True, "decide": profile_id}],
                    level="pass",
                )
            ],
            otherwise={"level": "refuse"},
        )

    async def test_embedding_with_the_detector_down_is_a_503_naming_the_rule(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_embedding_model(session, seeded)
        await self.pipeline_with_a_down_detector(app, session_factory, "embed")
        fake_upstream.set_json(embedding_body())
        response = await embed(client, seeded, HEALTH_TEXT)
        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == "privacy_undecidable"
        assert "No health, pass" in response.text and "diabete" not in response.text
        assert fake_upstream.bodies == []

    async def test_chat_with_the_detector_down_is_a_503_not_a_pass(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        await self.pipeline_with_a_down_detector(app, session_factory, "chat")
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": seeded.model.name,
                "messages": [{"role": "user", "content": HEALTH_TEXT}],
            },
            headers=seeded.auth,
        )
        assert response.status_code == 503, response.text
        assert fake_upstream.bodies == []

    async def test_oversized_input_on_a_negated_condition_is_a_503_for_embed_and_chat(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
        fake_upstream: FakeUpstream,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("gateway.privacy.evaluate.MAX_REQUEST_CHARS", 10)
        await add_embedding_model(session, seeded)
        for kind in ("embed", "chat"):
            await add_pipeline(
                app,
                session_factory,
                kind,
                f"Guard {kind}",
                rules=[rule("No health, pass", [{"key": "health", "negate": True}], level="pass")],
                otherwise={"level": "refuse"},
            )
        assert (await embed(client, seeded, HEALTH_TEXT * 2)).status_code == 503
        chat = await client.post(
            "/v1/chat/completions",
            json={
                "model": seeded.model.name,
                "messages": [{"role": "user", "content": HEALTH_TEXT * 2}],
            },
            headers=seeded.auth,
        )
        assert chat.status_code == 503
        assert fake_upstream.bodies == []


class TestUserPickingAnAdministratorsPolicy:
    async def test_chat_still_hides_what_the_environment_hides(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: Any,
        fake_upstream: FakeUpstream,
    ) -> None:
        from helpers import completion_body

        app.state.redactor = FakeDetector(
            {"Mario Rossi": "PERSON", "a@b.test": "EMAIL_ADDRESS"}
        ).redactor()
        from gateway.config import RedactionPolicy

        app.state.redaction._policy = RedactionPolicy(
            entities={"PERSON": EntityPolicy(mode=EntityMode.REDACT)}
        )
        picked = await add_policy(
            app,
            session_factory,
            "Emails",
            entities={"EMAIL_ADDRESS": EntityPolicy(mode=EntityMode.REDACT)},
            allow_list=["Mario Rossi"],
        )
        await add_pipeline(
            app,
            session_factory,
            "chat",
            "Mine",
            owner=seeded.user.id,
            otherwise={"level": "pass", "redact_with": str(picked)},
        )
        fake_upstream.set_json(completion_body())
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": seeded.model.name,
                "messages": [{"role": "user", "content": "Mario Rossi a@b.test"}],
            },
            headers=seeded.auth,
        )
        assert response.status_code == 200, response.text
        sent = str(fake_upstream.bodies[-1])
        assert "Mario Rossi" not in sent and "a@b.test" not in sent


class TestPoliciesAndProfiles:
    async def test_a_policy_cannot_name_a_non_default_profile_yet(
        self, admin: None, client: httpx.AsyncClient
    ) -> None:
        default = (
            await client.post(
                "/api/admin/privacy/profiles", json={"name": "Fast", "engine": "presidio"}
            )
        ).json()
        other = (
            await client.post(
                "/api/admin/privacy/profiles", json={"name": "Medical", "engine": "gliner"}
            )
        ).json()
        # Not flagged default by the API: the seeded one is; make it so directly below.
        assert default["is_default"] is False
        refused = await client.post(
            "/api/admin/privacy/policies",
            json={"name": "P", "policy": {}, "profile_id": other["id"]},
        )
        assert refused.status_code == 422
        assert "take effect in a later release" in refused.text

    async def test_the_default_profile_is_fine(
        self, admin: None, client: httpx.AsyncClient, session_factory: Any
    ) -> None:
        async with session_factory() as db:
            fast = DetectorProfile(
                name="Fast", engine="presidio", labels=[], fallback="refuse", is_default=True
            )
            db.add(fast)
            await db.commit()
            fid = str(fast.id)
        ok = await client.post(
            "/api/admin/privacy/policies", json={"name": "P", "policy": {}, "profile_id": fid}
        )
        assert ok.status_code == 201, ok.text

    async def test_a_users_policy_cannot_either(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
    ) -> None:
        async with session_factory() as db:
            medical = DetectorProfile(
                name="Medical", engine="gliner", labels=[], fallback="refuse", user_available=True
            )
            db.add(medical)
            await db.commit()
            mid = str(medical.id)
        as_user(app, seeded.user)
        refused = await client.post(
            "/api/me/privacy/policies", json={"name": "Mine", "entries": [], "profile_id": mid}
        )
        assert refused.status_code == 422 and "later release" in refused.text

    async def test_a_profile_must_name_a_known_engine(
        self, admin: None, client: httpx.AsyncClient
    ) -> None:
        bad = await client.post(
            "/api/admin/privacy/profiles", json={"name": "X", "engine": "nonsense"}
        )
        assert bad.status_code in (400, 422)
        assert "Unknown engine" in bad.text

    async def test_a_policy_in_use_cannot_be_deactivated(
        self, app: Any, admin: None, client: httpx.AsyncClient, session_factory: Any
    ) -> None:
        pid = await add_policy(app, session_factory, "Default", entities={"PERSON": EntityPolicy()})
        await add_pipeline(
            app,
            session_factory,
            "chat",
            "Chat",
            otherwise={"level": "pass", "redact_with": str(pid)},
        )
        refused = await client.patch(
            f"/api/admin/privacy/policies/{pid}", json={"is_active": False}
        )
        assert refused.status_code == 409 and "Chat" in refused.text
        assert str(pid) in app.state.privacy.policies
        free = await add_policy(app, session_factory, "Spare")
        assert (
            await client.patch(f"/api/admin/privacy/policies/{free}", json={"is_active": False})
        ).status_code == 200


class TestSummaryAndInForceLeaks:
    async def test_the_summary_needs_loaded_pipelines_and_counts_the_callers_own_key(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
    ) -> None:
        await add_embedding_model(session, seeded)
        await add_pipeline(
            app,
            session_factory,
            "embed",
            "Key only",
            rules=[rule("Health", HEALTH)],
            who={"mode": "keys", "ids": [str(seeded.api_key.id)]},
        )
        body = (await client.get("/v1/pystino/privacy", headers=seeded.auth)).json()
        assert {m["model"]: m["summary"] for m in body["embedding"]}["embed-1"] == [
            "refuses health data"
        ]
        app.state.privacy.loaded = False
        refused = await client.get("/v1/pystino/privacy", headers=seeded.auth)
        assert refused.status_code == 503

    async def test_in_force_shows_only_the_callers_own_matches(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
    ) -> None:
        other = await _other_user(session_factory, seeded)
        elsewhere = Group(name="secret-oncology")
        session.add(elsewhere)
        theirs = ApiKey(
            user_id=other.id, prefix="gwk_zzzz", key_hash="f" * 64, name="their-secret-key"
        )
        session.add(theirs)
        await session.commit()
        await add_pipeline(
            app,
            session_factory,
            "chat",
            "By group",
            who={"mode": "groups", "ids": [str(seeded.group.id), str(elsewhere.id)]},
        )
        await add_pipeline(
            app,
            session_factory,
            "chat",
            "By key",
            who={"mode": "keys", "ids": [str(seeded.api_key.id), str(theirs.id)]},
            position=1,
        )
        as_user(app, seeded.user)
        response = await client.get("/api/me/privacy/in-force")
        items = {p["name"]: p for p in response.json()["items"]}
        assert set(items) == {"By group", "By key"}
        assert items["By group"]["who"]["ids"] == [str(seeded.group.id)]
        assert items["By key"]["who"]["ids"] == [str(seeded.api_key.id)]
        assert "secret-oncology" not in response.text and "their-secret-key" not in response.text
