"""The `local_connectors` capability (Cerea ADR 0102), beside code_agents.

The gate and `/v1/me` behaviour is in test_capabilities_access.py; this file
covers the admin surface: new groups' default, the per-group toggle, the bulk
flip and its preview, the policy default, and the machine-client refusal.
"""

from __future__ import annotations

import httpx
from conftest import Seeded
from gateway.config import AccessSettings, OIDCSettings
from gateway.models import Group
from sqlalchemy import select
from test_capabilities_access import _fold_policy
from test_oidc_policy import admin_session as admin_session


async def test_new_groups_default_on_and_follow_the_policy_default(
    app: object,
    client: httpx.AsyncClient,
    admin_session: dict[str, str],
    session_factory: object,
) -> None:
    created = await client.post("/api/admin/groups", json={"name": "g-on"}, headers=admin_session)
    assert created.status_code == 201
    assert created.json()["local_connectors_allowed"] is True

    await _fold_policy(
        app, session_factory, group_mappings=[["research", "research"]], local_connectors_mode="off"
    )
    off = await client.post("/api/admin/groups", json={"name": "g-off"}, headers=admin_session)
    assert off.json()["local_connectors_allowed"] is False
    # Independent of code_agents, whose default stays on.
    assert off.json()["code_agents_allowed"] is True


def test_environment_default_is_on_and_parses_off() -> None:
    assert AccessSettings().local_connectors == "on"
    assert AccessSettings(local_connectors="off").local_connectors == "off"


async def test_policy_default_saves_and_reads_back(
    client: httpx.AsyncClient, admin_session: dict[str, str]
) -> None:
    before = await client.get("/api/admin/oidc/policy", headers=admin_session)
    assert before.json()["local_connectors_mode"] == "on"
    saved = await client.put(
        "/api/admin/oidc/policy", json={"local_connectors_mode": "off"}, headers=admin_session
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["local_connectors_mode"] == "off"
    assert saved.json()["code_agents_mode"] == "on"
    bad = await client.put(
        "/api/admin/oidc/policy", json={"local_connectors_mode": "maybe"}, headers=admin_session
    )
    assert bad.status_code == 400


async def test_patch_toggles_the_flag_and_the_list_shows_it(
    client: httpx.AsyncClient, seeded: Seeded, admin_session: dict[str, str]
) -> None:
    group_id = str(seeded.group.id)
    patched = await client.patch(
        f"/api/admin/groups/{group_id}",
        json={"local_connectors_allowed": False},
        headers=admin_session,
    )
    assert patched.status_code == 200
    assert patched.json()["local_connectors_allowed"] is False
    assert patched.json()["code_agents_allowed"] is True
    listing = (await client.get("/api/admin/groups", headers=admin_session)).json()
    row = next(item for item in listing["items"] if item["id"] == group_id)
    assert row["local_connectors_allowed"] is False


async def test_bulk_flip_and_preview(
    client: httpx.AsyncClient,
    seeded: Seeded,
    admin_session: dict[str, str],
    session_factory: object,
) -> None:
    async with session_factory() as session:  # type: ignore[attr-defined]
        (await session.get(Group, seeded.group.id)).local_connectors_allowed = False  # type: ignore[union-attr]
        await session.commit()
    preview = await client.get(
        "/api/admin/oidc/policy/preview?bulk_capability=local_connectors&bulk_value=true",
        headers=admin_session,
    )
    assert preview.status_code == 200, preview.text
    on = await client.post(
        "/api/admin/groups/capabilities",
        json={"capability": "local_connectors", "value": True},
        headers=admin_session,
    )
    assert on.status_code == 200, on.text
    async with session_factory() as session:  # type: ignore[attr-defined]
        flags = (await session.execute(select(Group.local_connectors_allowed))).scalars().all()
    assert flags and all(flags)
    # Bulk on local_connectors never touched code_agents.
    listing = (await client.get("/api/admin/groups", headers=admin_session)).json()
    assert all(item["code_agents_allowed"] for item in listing["items"])


async def test_turning_it_off_needs_an_accepted_machine_client(
    app: object, client: httpx.AsyncClient, seeded: Seeded, admin_session: dict[str, str]
) -> None:
    app.state.settings.oidc = OIDCSettings(  # type: ignore[attr-defined]
        machine_client_id="other", accepted_clients="console,chat"
    )
    refused = await client.patch(
        f"/api/admin/groups/{seeded.group.id}",
        json={"local_connectors_allowed": False},
        headers=admin_session,
    )
    assert refused.status_code == 400, refused.text
    assert "MACHINE_CLIENT_ID" in refused.text
    bulk = await client.post(
        "/api/admin/groups/capabilities",
        json={"capability": "local_connectors", "value": False},
        headers=admin_session,
    )
    assert bulk.status_code == 400
    granted = await client.patch(
        f"/api/admin/groups/{seeded.group.id}",
        json={"local_connectors_allowed": True},
        headers=admin_session,
    )
    assert granted.status_code == 200


class TestStandaloneWithoutChat:
    """`GATEWAY_CHAT__ENABLED` false: code agents and local connectors are not
    capabilities. Not reported, not settable, no gating."""

    @staticmethod
    def _standalone(app: object) -> None:
        app.state.settings.chat.enabled = False  # type: ignore[attr-defined]

    async def test_v1_me_reports_only_api_keys_and_the_machine_client_is_not_gated(
        self,
        bearer_app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        signing_key: object,
        session_factory: object,
    ) -> None:
        from conftest import bearer_auth, make_token

        self._standalone(bearer_app)
        async with session_factory() as session:  # type: ignore[attr-defined]
            group = await session.get(Group, seeded.group.id)
            group.code_agents_allowed = False
            group.local_connectors_allowed = False
            await session.commit()
        machine = bearer_auth(make_token(signing_key, azp="opencode-enrollment"))
        me = await client.get("/v1/me", headers=machine)
        assert me.status_code == 200
        assert me.json()["capabilities"] == {
            "code_agents": None,
            "api_keys": True,
            "local_connectors": None,
        }
        assert (await client.get("/v1/models", headers=machine)).status_code == 200

    async def test_admin_api_refuses_to_set_either_flag(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        admin_session: dict[str, str],
    ) -> None:
        self._standalone(app)
        for body in ({"code_agents_allowed": False}, {"local_connectors_allowed": True}):
            refused = await client.patch(
                f"/api/admin/groups/{seeded.group.id}", json=body, headers=admin_session
            )
            assert refused.status_code == 400, body
            assert "GATEWAY_CHAT__ENABLED" in refused.text
        for capability in ("code_agents", "local_connectors"):
            bulk = await client.post(
                "/api/admin/groups/capabilities",
                json={"capability": capability, "value": True},
                headers=admin_session,
            )
            assert bulk.status_code == 400
        for knob in ("code_agents_mode", "local_connectors_mode"):
            saved = await client.put(
                "/api/admin/oidc/policy", json={knob: "off"}, headers=admin_session
            )
            assert saved.status_code == 400, knob
        # api_keys is unaffected.
        ok = await client.patch(
            f"/api/admin/groups/{seeded.group.id}",
            json={"api_keys_allowed": False},
            headers=admin_session,
        )
        assert ok.status_code == 200

    async def test_api_me_tells_the_console_whether_there_is_a_chat(
        self, app: object, client: httpx.AsyncClient, admin_session: dict[str, str]
    ) -> None:
        assert (await client.get("/api/me", headers=admin_session)).json()["chat_enabled"] is True
        self._standalone(app)
        me = (await client.get("/api/me", headers=admin_session)).json()
        assert me["chat_enabled"] is False
        assert me["capabilities"]["code_agents"] is None
        assert me["capabilities"]["local_connectors"] is None


def test_chat_enabled_defaults_false() -> None:
    from gateway.config import ChatSettings

    assert ChatSettings().enabled is False
