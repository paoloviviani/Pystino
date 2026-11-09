"""The API surface for redaction policies (ADR 0113): the admin's CRUD, and the preview.

A policy is what to hide and has no scope; which requests it applies to is a
pipeline's business, so what is left for these routes is keeping names unique,
refusing to delete a policy a pipeline still applies, and auditing.

The preview is here too, and two of its properties are load-bearing rather than
cosmetic: a blocked sample is a 200, because a preview that fails looks broken at
the moment it is working; and the sample never reaches a log, because within a
week of shipping it will contain the real prompt that came back wrong.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
import pytest
from conftest import Seeded
from gateway.config import (
    EntityMode,
    EntityPolicy,
    RedactionPolicy,
    RedactionSettings,
    Settings,
)
from gateway.models import AdminAuditEvent
from privacy_helpers import add_pipeline, add_policy, catch_all
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin
from test_redaction_http import FakeDetector


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """The conftest settings, plus a placeholder key.

    Overridden for this module alone because the preview derives placeholders
    with the deployment's HMAC key — the same key ``build_for`` hands every
    engine it constructs — and deriving one from an empty key is refused by the
    placeholder scheme. A deployment running an engine that anonymises always has
    one; only the noop-engined test fixture does not.
    """
    return settings.model_copy(
        update={"redaction": RedactionSettings(engine="noop", placeholder_key=SecretStr("k"))}
    )


def policy(**kwargs: Any) -> dict[str, Any]:
    return RedactionPolicy(**kwargs).model_dump(mode="json")


def install_detector(app: Any, spans_for: dict[str, str]) -> None:
    """Put a detecting engine in force, keeping the app's real resolver."""
    app.state.redactor = FakeDetector(spans_for).redactor()


async def admin_client(
    app: Any, session_factory: async_sessionmaker[AsyncSession], seeded: Seeded
) -> None:
    as_user(app, await make_admin(session_factory, seeded))


# --------------------------------------------------------------------------
# admin CRUD
# --------------------------------------------------------------------------


class TestPolicyCrud:
    async def test_create_list_update_delete_with_audit(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await admin_client(app, session_factory, seeded)
        created = await client.post(
            "/api/admin/privacy/policies",
            json={
                "name": "Default",
                "policy": policy(entities={"PERSON": EntityPolicy(mode=EntityMode.REDACT)}),
                "reason": "baseline",
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["name"] == "Default" and body["used_by"] == []
        assert "scope" not in body and "scope_id" not in body

        listed = (await client.get("/api/admin/privacy/policies")).json()
        assert [p["name"] for p in listed["items"]] == ["Default"]

        patched = await client.patch(
            f"/api/admin/privacy/policies/{body['id']}",
            json={
                "name": "Default policy",
                "policy": policy(entities={"PERSON": EntityPolicy(mode=EntityMode.BLOCK)}),
            },
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["policy"]["entities"]["PERSON"]["mode"] == "block"
        # This worker sees the change at once.
        assert app.state.privacy.policies[body["id"]].name == "Default policy"

        assert (await client.delete(f"/api/admin/privacy/policies/{body['id']}")).status_code == 204
        assert app.state.privacy.policies == {}

        async with session_factory() as db:
            actions = [
                r.action for r in (await db.execute(select(AdminAuditEvent))).scalars().all()
            ]
        assert {
            "redaction_policy.create",
            "redaction_policy.update",
            "redaction_policy.delete",
        } <= set(actions)

    async def test_names_are_unique_ignoring_case(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await admin_client(app, session_factory, seeded)
        await add_policy(app, session_factory, "Default")
        again = await client.post(
            "/api/admin/privacy/policies", json={"name": "default", "policy": policy()}
        )
        assert again.status_code == 409, again.text

    async def test_a_policy_a_pipeline_applies_cannot_be_deleted(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A pipeline that names nothing would silently stop hiding what it was written to."""
        await admin_client(app, session_factory, seeded)
        pid = await add_policy(app, session_factory, "Default", entities={"PERSON": EntityPolicy()})
        await add_pipeline(
            app,
            session_factory,
            "chat",
            "Chat",
            otherwise={"level": "pass", "redact_with": str(pid)},
        )
        refused = await client.delete(f"/api/admin/privacy/policies/{pid}")
        assert refused.status_code == 409, refused.text
        assert "Chat" in refused.json()["error"]["message"]
        listed = (await client.get("/api/admin/privacy/policies")).json()["items"][0]
        assert listed["used_by"] == ["Chat"]

    async def test_a_policy_cannot_name_a_profile_that_does_not_exist(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await admin_client(app, session_factory, seeded)
        response = await client.post(
            "/api/admin/privacy/policies",
            json={"name": "P", "policy": policy(), "profile_id": str(uuid.uuid4())},
        )
        assert response.status_code in (400, 422), response.text

    async def test_an_inactive_policy_is_not_in_force(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await admin_client(app, session_factory, seeded)
        pid = await add_policy(app, session_factory, "Off", active=False)
        assert str(pid) not in app.state.privacy.policies


class TestAccessControl:
    async def test_a_non_admin_cannot_reach_the_policies(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, seeded.user)
        for path in ("policies", "pipelines", "classifiers", "options"):
            assert (await client.get(f"/api/admin/privacy/{path}")).status_code == 403, path

    async def test_no_session_is_401(self, client: httpx.AsyncClient, seeded: Seeded) -> None:
        assert (await client.get("/api/admin/privacy/policies")).status_code == 401


class TestPreview:
    async def test_it_shows_the_rewrite_the_model_would_receive(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Not a proxy to /detect: the spans alone say nothing about what the
        provider would see, which is the only question being asked."""
        await admin_client(app, session_factory, seeded)
        install_detector(app, {"Mario Rossi": "PERSON"})
        pid = await catch_all(app, session_factory, PERSON=EntityPolicy())

        response = await client.post(
            "/api/admin/redaction/preview",
            json={"text": "please email Mario Rossi about it", "policy_id": str(pid)},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["blocked"] is False
        assert body["entity_count"] == 1
        assert "Mario Rossi" not in body["redacted_text"]
        assert "<PERSON_" in body["redacted_text"]
        span = body["spans"][0]
        assert span["entity_type"] == "PERSON"
        assert (span["start"], span["end"]) == (13, 24)
        assert span["score"] == pytest.approx(0.9)
        assert span["mode"] == "anonymise_restore"

    async def test_a_span_the_policy_ignores_is_shown_with_its_mode(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The Italian bug in one screen: the detector finds it, the policy says
        leave it, and until this route existed nothing anywhere showed either."""
        await admin_client(app, session_factory, seeded)
        install_detector(app, {"ilpost.it": "URL"})

        response = await client.post(
            "/api/admin/redaction/preview",
            json={"text": "riassumi le notizie da ilpost.it"},
        )

        body = response.json()
        assert body["entity_count"] == 0
        assert body["redacted_text"] == "riassumi le notizie da ilpost.it"
        assert body["spans"][0] == {
            "entity_type": "URL",
            "start": 23,
            "end": 32,
            "score": pytest.approx(0.9),
            "mode": "off",
            "threshold": pytest.approx(0.5),
            "allow_listed": False,
        }

    async def test_a_blocked_preview_is_a_200_that_says_so(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A preview that refuses looks broken at the moment it is working."""
        await admin_client(app, session_factory, seeded)
        install_detector(app, {"123-45-6789": "US_SSN"})
        created = await client.post(
            "/api/admin/privacy/policies",
            json={
                "name": "Block SSN",
                "policy": policy(entities={"US_SSN": EntityPolicy(mode=EntityMode.BLOCK)}),
            },
        )
        assert created.status_code == 201, created.text

        response = await client.post(
            "/api/admin/redaction/preview",
            json={"text": "my ssn is 123-45-6789", "policy_id": created.json()["id"]},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["blocked"] is True
        assert "US_SSN" in body["blocked_reason"]
        # Never the matched value: a refusal is logged and pasted into tickets.
        assert "123-45-6789" not in body["blocked_reason"]
        # Nothing was rewritten, so there is no text to claim would be sent.
        assert body["redacted_text"] is None
        assert body["rule_id"] == created.json()["id"]

    async def test_the_sample_never_reaches_a_log(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Within a week of shipping this box holds the prompt that came back
        wrong — which is real personal data, pasted in by an admin who is
        debugging. A redaction inspector that logs it has defeated itself."""
        await admin_client(app, session_factory, seeded)
        install_detector(app, {"Mario Rossi": "PERSON"})

        with caplog.at_level(logging.DEBUG):
            response = await client.post(
                "/api/admin/redaction/preview",
                json={"text": "Mario Rossi lives at 12 Via Roma"},
            )

        assert response.status_code == 200
        assert "Mario Rossi" not in caplog.text
        assert "Via Roma" not in caplog.text

    async def test_an_unsaved_policy_previews_without_being_stored(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The policy page previews its form as it stands, and its own effect is the
        question: nothing is stored and no other policy is folded in."""
        await admin_client(app, session_factory, seeded)
        install_detector(app, {"Mario Rossi": "PERSON", "mario@example.com": "EMAIL_ADDRESS"})
        await catch_all(app, session_factory, PERSON=EntityPolicy())

        draft = policy(entities={"EMAIL_ADDRESS": EntityPolicy(mode=EntityMode.REDACT)})
        response = await client.post(
            "/api/admin/redaction/preview",
            json={"text": "Mario Rossi, mario@example.com", "policy": draft},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["entity_count"] == 1
        assert "mario@example.com" not in body["redacted_text"]
        assert "Mario Rossi" in body["redacted_text"]
        listing = await client.get("/api/admin/privacy/policies")
        assert [p["name"] for p in listing.json()["items"]] == ["All requests"]

    async def test_a_policy_that_does_not_exist_is_refused(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await admin_client(app, session_factory, seeded)
        response = await client.post(
            "/api/admin/redaction/preview", json={"text": "hello", "policy_id": str(uuid.uuid4())}
        )
        assert response.status_code == 404, response.text

    async def test_an_engine_that_detects_nothing_says_so(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """An empty result under noop must not read as "nothing here to redact"."""
        await admin_client(app, session_factory, seeded)

        response = await client.post("/api/admin/redaction/preview", json={"text": "Mario Rossi"})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["engine"] == "noop"
        assert body["spans"] == []
        assert body["redacted_text"] == "Mario Rossi"
        assert "detects nothing" in body["note"]
