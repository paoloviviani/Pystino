"""ADR 0096 (amended): capabilities are per-group flags only.

A person holds code_agents / api_keys when they are an administrator, or
hold an active membership in an active group with the flag on. There is no
mode that gates access anymore — the deployment setting is the default new
groups are created with. A person in no group holds neither (in practice the
default group covers everyone; see the ADR edge).

Covers the pure function, the /v1/me report, the machine-client gate (403
agents_not_allowed), the api_keys gate at mint and use (403
api_keys_not_allowed, keys never deleted), creation-path defaults, the bulk
on/off with preview, the list showing the flags, and the query budget.
"""

from __future__ import annotations

import httpx
from conftest import Seeded, bearer_auth, make_token
from gateway.capabilities import Capabilities, default_flags, effective_capabilities
from gateway.models import (
    Group,
    IdentityEvent,
    Membership,
    MembershipSource,
    OIDCPolicyConfig,
    SeenGroup,
    User,
)
from gateway.oidc_policy import OIDCPolicy
from sqlalchemy import select
from test_me_keys import as_user
from test_oidc_policy import admin_session as admin_session
from test_query_counts import counted

#: The default machine client (opencode-enrollment): the token's azp/client_id
#: that the code_agents gate keys on.
MACHINE_CLIENT = "opencode-enrollment"


def _user(
    *,
    is_active: bool = True,
    is_admin: bool = False,
    memberships: list[Membership] | None = None,
) -> User:
    user = User(
        issuer="https://idp.test",
        subject="subject-1",
        email="member@example.org",
        is_active=is_active,
        is_admin=is_admin,
    )
    user.memberships = memberships or []
    return user


def _group(
    name: str,
    *,
    active: bool = True,
    code_agents: bool = False,
    api_keys: bool = False,
    local_connectors: bool = False,
) -> Group:
    return Group(
        name=name,
        is_active=active,
        code_agents_allowed=code_agents,
        api_keys_allowed=api_keys,
        local_connectors_allowed=local_connectors,
    )


def _membership(group: Group, source: MembershipSource = MembershipSource.MANUAL) -> Membership:
    return Membership(group=group, source=source)


def _caps(code_agents: bool, api_keys: bool, local_connectors: bool = False) -> Capabilities:
    return Capabilities(
        code_agents=code_agents, api_keys=api_keys, local_connectors=local_connectors
    )


class TestEffectiveCapabilities:
    def test_admin_holds_both_with_no_groups(self) -> None:
        assert effective_capabilities(_user(is_admin=True)) == _caps(True, True, True)

    def test_flagged_active_group_grants(self) -> None:
        member = _user(memberships=[_membership(_group("agents", code_agents=True, api_keys=True))])
        assert effective_capabilities(member) == _caps(True, True)

    def test_unflagged_group_grants_neither(self) -> None:
        member = _user(memberships=[_membership(_group("chat"))])
        assert effective_capabilities(member) == _caps(False, False)

    def test_no_group_grants_neither(self) -> None:
        # The ADR edge: a person in no group has neither capability. In
        # practice the default group covers everyone at first sign-in.
        assert effective_capabilities(_user()) == _caps(False, False)

    def test_local_connectors_is_its_own_flag(self) -> None:
        only = _user(memberships=[_membership(_group("lc", local_connectors=True))])
        assert effective_capabilities(only) == _caps(False, False, True)
        agents = _user(memberships=[_membership(_group("ag", code_agents=True))])
        assert effective_capabilities(agents) == _caps(True, False, False)

    def test_flags_are_independent(self) -> None:
        member = _user(memberships=[_membership(_group("agents", code_agents=True))])
        assert effective_capabilities(member) == _caps(True, False)

    def test_disabled_group_does_not_grant(self) -> None:
        disabled = _group("agents", active=False, code_agents=True, api_keys=True)
        assert effective_capabilities(_user(memberships=[_membership(disabled)])) == _caps(
            False, False
        )

    def test_inactive_user_does_not_grant(self) -> None:
        flagged = _group("agents", code_agents=True, api_keys=True)
        member = _user(is_active=False, memberships=[_membership(flagged)])
        assert effective_capabilities(member) == _caps(False, False)

    def test_any_source_counts(self) -> None:
        flagged = _group("agents", code_agents=True, api_keys=True)
        oidc = _user(memberships=[_membership(flagged, MembershipSource.OIDC)])
        assert effective_capabilities(oidc) == _caps(True, True)


class TestDefaultFlags:
    def test_no_policy_means_on(self) -> None:
        assert default_flags(None) == _caps(True, True, True)

    def test_on_off_follow_the_folded_default(self) -> None:
        assert default_flags(OIDCPolicy(code_agents_mode="on", api_keys_mode="off")) == _caps(
            True, False, True
        )

    def test_local_connectors_default_follows_its_own_mode(self) -> None:
        assert default_flags(OIDCPolicy(local_connectors_mode="off")) == _caps(True, True, False)
        assert default_flags(OIDCPolicy(local_connectors_mode="groups")) == _caps(True, True, False)

    def test_legacy_values_fold_without_behaviour_change(self) -> None:
        assert default_flags(
            OIDCPolicy(code_agents_mode="everyone", api_keys_mode="groups")
        ) == _caps(True, False, True)


async def _fold_policy(app: object, session_factory: object, **row: object) -> None:
    """Insert an oidc_config decision and fold it onto the worker's policy."""
    async with session_factory() as session:  # type: ignore[attr-defined]
        session.add(OIDCPolicyConfig(**row))
        await session.commit()
    await app.state.oidc_policy.refresh_once()  # type: ignore[attr-defined]


async def _reload_user(session_factory: object, user_id: object) -> User:
    """A user with its memberships and groups, freshly read — the object the
    management routes would load, so in-memory flags reflect a re-grant."""
    from sqlalchemy.orm import selectinload

    async with session_factory() as session:  # type: ignore[attr-defined]
        return await session.get(
            User, user_id, options=[selectinload(User.memberships).selectinload(Membership.group)]
        )


class TestV1MeReportsCapabilities:
    async def test_reports_both_from_the_persons_flags(
        self,
        bearer_app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        signing_key: object,
    ) -> None:
        response = await client.get("/v1/me", headers=bearer_auth(make_token(signing_key)))
        assert response.status_code == 200
        # The seeded person holds "research", an ordinary working group with
        # both flags on.
        assert response.json()["capabilities"] == {
            "code_agents": True,
            "api_keys": True,
            "local_connectors": True,
        }

    async def test_a_person_in_no_group_holds_neither(
        self, bearer_app: object, client: httpx.AsyncClient, signing_key: object
    ) -> None:
        # The ADR edge, over a real door: a token whose claim nothing maps
        # provisions a person with no membership, and /v1/me answers both
        # False — the chat-only extreme. The default group is switched off
        # here: it would otherwise be created with the deployment's default
        # (on) and put this person in a group that holds both.
        bearer_app.state.settings.oidc.default_group = ""  # type: ignore[attr-defined]
        response = await client.get("/v1/me", headers=bearer_auth(make_token(signing_key)))
        assert response.status_code == 200
        assert response.json()["capabilities"] == {
            "code_agents": False,
            "api_keys": False,
            "local_connectors": False,
        }

    async def test_reflects_the_persons_flags(
        self,
        bearer_app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        signing_key: object,
        session_factory: object,
    ) -> None:
        # The seeded group carries both flags; clearing api_keys makes the
        # person's answer follow the flags alone.
        async with session_factory() as session:  # type: ignore[attr-defined]
            group = await session.get(Group, seeded.group.id)
            group.api_keys_allowed = False
            await session.commit()

        response = await client.get("/v1/me", headers=bearer_auth(make_token(signing_key)))
        assert response.json()["capabilities"] == {
            "code_agents": True,
            "api_keys": False,
            "local_connectors": True,
        }


class TestMachineClientGate:
    """A machine-client token from a person without code_agents is refused
    with 403 agents_not_allowed on every /v1 route — /v1/me included — and a
    chat-client token for the same person passes."""

    async def _deny_code_agents(self, session_factory: object, seeded: Seeded) -> None:
        async with session_factory() as session:  # type: ignore[attr-defined]
            group = await session.get(Group, seeded.group.id)
            group.code_agents_allowed = False
            group.local_connectors_allowed = False
            await session.commit()

    async def _set_flags(
        self,
        session_factory: object,
        seeded: Seeded,
        *,
        code_agents: bool,
        local_connectors: bool,
    ) -> None:
        async with session_factory() as session:  # type: ignore[attr-defined]
            group = await session.get(Group, seeded.group.id)
            group.code_agents_allowed = code_agents
            group.local_connectors_allowed = local_connectors
            await session.commit()

    async def test_only_code_agents_passes_everywhere(
        self,
        bearer_app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        signing_key: object,
        session_factory: object,
    ) -> None:
        await self._set_flags(session_factory, seeded, code_agents=True, local_connectors=False)
        machine = bearer_auth(make_token(signing_key, azp=MACHINE_CLIENT))
        for path in ("/v1/me", "/v1/models"):
            assert (await client.get(path, headers=machine)).status_code == 200, path
        me = await client.get("/v1/me", headers=machine)
        assert me.json()["capabilities"]["code_agents"] is True
        assert me.json()["capabilities"]["local_connectors"] is False

    async def test_only_local_connectors_reaches_v1_me_and_nothing_else(
        self,
        bearer_app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        signing_key: object,
        session_factory: object,
    ) -> None:
        await self._set_flags(session_factory, seeded, code_agents=False, local_connectors=True)
        machine = bearer_auth(make_token(signing_key, azp=MACHINE_CLIENT))
        me = await client.get("/v1/me", headers=machine)
        assert me.status_code == 200
        assert me.json()["capabilities"]["code_agents"] is False
        assert me.json()["capabilities"]["local_connectors"] is True
        # No model access: every other route is still refused for the machine
        # client, and the chat client is untouched.
        for path in ("/v1/models", "/v1/me/identities"):
            response = await client.get(path, headers=machine)
            assert response.status_code == 403, path
            assert response.json()["error"]["code"] == "agents_not_allowed", path
        chat = await client.get("/v1/models", headers=bearer_auth(make_token(signing_key)))
        assert chat.status_code == 200

    async def test_neither_is_refused_with_capabilities_in_the_body(
        self,
        bearer_app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        signing_key: object,
        session_factory: object,
    ) -> None:
        await self._set_flags(session_factory, seeded, code_agents=False, local_connectors=False)
        machine = bearer_auth(make_token(signing_key, azp=MACHINE_CLIENT))
        response = await client.get("/v1/me", headers=machine)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "agents_not_allowed"
        assert response.json()["capabilities"]["local_connectors"] is False

    async def test_both_passes_everywhere(
        self,
        bearer_app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        signing_key: object,
        session_factory: object,
    ) -> None:
        await self._set_flags(session_factory, seeded, code_agents=True, local_connectors=True)
        machine = bearer_auth(make_token(signing_key, azp=MACHINE_CLIENT))
        for path in ("/v1/me", "/v1/models"):
            assert (await client.get(path, headers=machine)).status_code == 200, path

    async def test_machine_token_is_refused_everywhere(
        self,
        bearer_app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        signing_key: object,
        session_factory: object,
    ) -> None:
        await self._deny_code_agents(session_factory, seeded)
        machine = make_token(signing_key, azp=MACHINE_CLIENT)
        for path in ("/v1/me", "/v1/models", "/v1/me/identities"):
            response = await client.get(path, headers=bearer_auth(machine))
            assert response.status_code == 403, path
            assert response.json()["error"]["code"] == "agents_not_allowed", path

    async def test_v1_me_refusal_still_carries_capabilities(
        self,
        bearer_app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        signing_key: object,
        session_factory: object,
    ) -> None:
        await self._deny_code_agents(session_factory, seeded)
        response = await client.get(
            "/v1/me", headers=bearer_auth(make_token(signing_key, azp=MACHINE_CLIENT))
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "agents_not_allowed"
        assert response.json()["capabilities"] == {
            "code_agents": False,
            "api_keys": True,
            "local_connectors": False,
        }

    async def test_chat_client_token_for_the_same_user_passes(
        self,
        bearer_app: object,
        client: httpx.AsyncClient,
        signing_key: object,
    ) -> None:
        response = await client.get("/v1/models", headers=bearer_auth(make_token(signing_key)))
        assert response.status_code == 200

    async def test_a_flagged_machine_token_passes(
        self,
        bearer_app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        signing_key: object,
        session_factory: object,
    ) -> None:
        # Deny, then re-grant: the machine token passes again the moment the
        # flag comes back — a loss one group flag restores.
        await self._deny_code_agents(session_factory, seeded)
        denied = await client.get(
            "/v1/models", headers=bearer_auth(make_token(signing_key, azp=MACHINE_CLIENT))
        )
        assert denied.status_code == 403

        async with session_factory() as session:  # type: ignore[attr-defined]
            group = await session.get(Group, seeded.group.id)
            group.code_agents_allowed = True
            await session.commit()

        response = await client.get(
            "/v1/models", headers=bearer_auth(make_token(signing_key, azp=MACHINE_CLIENT))
        )
        assert response.status_code == 200

    async def test_machine_gate_adds_no_queries(
        self,
        bearer_app: object,
        client: httpx.AsyncClient,
        signing_key: object,
    ) -> None:
        engine = bearer_app.state.engine  # type: ignore[attr-defined]
        # No default group, so this person holds no group and the gate refuses
        # (the default group is otherwise created holding code_agents).
        bearer_app.state.settings.oidc.default_group = ""  # type: ignore[attr-defined]
        machine = make_token(signing_key, azp=MACHINE_CLIENT)
        # Warm-up like test_query_counts does: the first token request syncs
        # the identity and may write memberships; the steady-state one after it
        # is the one whose budget the gate must not move.
        assert (await client.get("/v1/models", headers=bearer_auth(machine))).status_code == 403
        with counted(engine) as statements:
            response = await client.get("/v1/models", headers=bearer_auth(machine))
            assert response.status_code == 403
        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        writes = [
            s for s in statements if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]
        assert not writes, "the machine gate must not write"
        # Same budget as a plain bearer /v1/models call: the gate reads rows
        # authentication already loaded.
        assert len(selects) <= 5


class TestApiKeysGate:
    async def _deny_api_keys(self, session_factory: object, seeded: Seeded) -> None:
        async with session_factory() as session:  # type: ignore[attr-defined]
            group = await session.get(Group, seeded.group.id)
            group.api_keys_allowed = False
            await session.commit()

    async def test_mint_is_refused_and_restored_by_regrant(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: object,
    ) -> None:
        await self._deny_api_keys(session_factory, seeded)
        as_user(app, await _reload_user(session_factory, seeded.user.id))

        response = await client.post(
            "/api/me/keys",
            json={"name": "chat-only", "billing_group_id": str(seeded.group.id)},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "api_keys_not_allowed"

        async with session_factory() as session:  # type: ignore[attr-defined]
            group = await session.get(Group, seeded.group.id)
            group.api_keys_allowed = True
            await session.commit()

        as_user(app, await _reload_user(session_factory, seeded.user.id))
        response = await client.post(
            "/api/me/keys",
            json={"name": "allowed", "billing_group_id": str(seeded.group.id)},
        )
        assert response.status_code == 201

    async def test_use_is_refused_and_restored_by_regrant(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: object,
    ) -> None:
        await self._deny_api_keys(session_factory, seeded)
        response = await client.get("/v1/models", headers=seeded.auth)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "api_keys_not_allowed"

        # A granted flag makes the same key work again — the key was never
        # deleted, because the loss is one a group can restore.
        async with session_factory() as session:  # type: ignore[attr-defined]
            group = await session.get(Group, seeded.group.id)
            group.api_keys_allowed = True
            await session.commit()

        response = await client.get("/v1/models", headers=seeded.auth)
        assert response.status_code == 200

    async def test_admin_is_exempt_at_use(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: object,
    ) -> None:
        async with session_factory() as session:  # type: ignore[attr-defined]
            user = await session.get(User, seeded.user.id)
            user.is_admin = True
            await session.commit()
        response = await client.get("/v1/models", headers=seeded.auth)
        assert response.status_code == 200


class TestNewGroupsGetTheDefault:
    """Every creation path stamps the folded default onto a new group (ADR
    0096, amended). Each test folds an `off` decision in, creates through the
    path, and expects the flag off; a second case with no decision expects
    on — the upgrade-changes-nothing guarantee."""

    async def _fold(self, app: object, session_factory: object, **row: object) -> None:
        await _fold_policy(app, session_factory, group_mappings=[["research", "research"]], **row)

    async def test_console_create(
        self,
        app: object,
        client: httpx.AsyncClient,
        admin_session: dict[str, str],
        session_factory: object,
    ) -> None:
        await self._fold(app, session_factory, code_agents_mode="off")
        created = await client.post(
            "/api/admin/groups", json={"name": "new-group"}, headers=admin_session
        )
        assert created.status_code == 201
        assert created.json()["code_agents_allowed"] is False

    async def test_console_create_defaults_on(
        self, app: object, client: httpx.AsyncClient, admin_session: dict[str, str]
    ) -> None:
        created = await client.post(
            "/api/admin/groups", json={"name": "new-group"}, headers=admin_session
        )
        assert created.status_code == 201
        assert created.json()["code_agents_allowed"] is True

    async def test_map_new_group(
        self, client: httpx.AsyncClient, admin_session: dict[str, str], session_factory: object
    ) -> None:
        # The seen-group surface: a mapped IdP name creating a new group.
        async with session_factory() as session:
            session.add(SeenGroup(issuer="https://idp.test", name="gitlab/eng"))
            await session.commit()
        seen = (await client.get("/api/admin/groups/seen", headers=admin_session)).json()["items"]
        response = await client.post(
            f"/api/admin/groups/seen/{seen[0]['id']}/map",
            json={"target": "new_same_name"},
            headers=admin_session,
        )
        assert response.status_code == 200, response.text
        assert response.json()["group"]["code_agents_allowed"] is True

    async def test_auto_import(
        self,
        bearer_app: object,
        client: httpx.AsyncClient,
        signing_key: object,
        session_factory: object,
    ) -> None:
        # group_import=auto creates a group for a claimed name at sign-in;
        # the folded default must ride along.
        from gateway.config import OIDCSettings

        bearer_app.state.settings.oidc = OIDCSettings(  # type: ignore[attr-defined]
            enabled=True,
            issuer="https://idp.test",
            client_id="llm-gateway",
            groups_claim="groups",
            access_token_audience="pystino-api",
            group_import="auto",
        )
        async with session_factory() as session:  # type: ignore[attr-defined]
            session.add(
                OIDCPolicyConfig(code_agents_mode="off", group_mappings=[["fresh", "fresh"]])
            )
            await session.commit()
        await bearer_app.state.oidc_policy.refresh_once()  # type: ignore[attr-defined]

        token = make_token(signing_key, groups=["fresh"])
        response = await client.get("/v1/models", headers=bearer_auth(token))
        assert response.status_code == 200

        async with session_factory() as session:  # type: ignore[attr-defined]
            group = (await session.execute(select(Group).where(Group.name == "fresh"))).scalar_one()
            assert group.code_agents_allowed is False


class TestGroupFlagsListed:
    """The list shows the stored flags: PATCH a flag, GET the list, and it
    shows — set it back, and it shows false."""

    async def test_patch_then_list_shows_the_flag(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        admin_session: dict[str, str],
    ) -> None:
        """The list shows the stored flag — the bug this pins: the list once
        built GroupAdminResponse by hand and answered always-false, so the
        console's refetch snapped every toggle back."""
        group_id = str(seeded.group.id)
        # The seeded group carries both flags; clear one, and the list says so.
        await client.patch(
            f"/api/admin/groups/{group_id}",
            json={"code_agents_allowed": False},
            headers=admin_session,
        )
        listing = (await client.get("/api/admin/groups", headers=admin_session)).json()
        row = next(item for item in listing["items"] if item["id"] == group_id)
        assert row["code_agents_allowed"] is False
        assert row["api_keys_allowed"] is True

        patched = await client.patch(
            f"/api/admin/groups/{group_id}",
            json={"code_agents_allowed": True},
            headers=admin_session,
        )
        assert patched.status_code == 200
        assert patched.json()["code_agents_allowed"] is True

        listing = (await client.get("/api/admin/groups", headers=admin_session)).json()
        row = next(item for item in listing["items"] if item["id"] == group_id)
        assert row["code_agents_allowed"] is True


class TestCapabilityBulk:
    async def _plain_member(self, session_factory: object, group_id: object, email: str) -> None:
        """An active, non-admin person holding the group — the preview's unit."""
        import uuid

        from gateway.models import MembershipSource

        async with session_factory() as session:  # type: ignore[attr-defined]
            user = User(
                issuer="https://idp.test",
                subject=f"subject-{uuid.uuid4().hex[:8]}",
                email=email,
            )
            session.add(user)
            await session.flush()
            session.add(
                Membership(user_id=user.id, group_id=group_id, source=MembershipSource.MANUAL)  # type: ignore[arg-type]
            )
            await session.commit()

    async def test_preview_counts_who_would_gain(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        admin_session: dict[str, str],
        session_factory: object,
    ) -> None:
        # The seeded person is an administrator here (the fixture promotes
        # them) and the seeded group carries the flag, so the preview needs a
        # plain member of an *unflagged* group — the people a bulk turn-on
        # would grant.
        unflagged = Group(name="plain", description="no flags")
        async with session_factory() as session:  # type: ignore[attr-defined]
            session.add(unflagged)
            await session.commit()
        await self._plain_member(session_factory, unflagged.id, "plain@example.org")
        response = await client.get(
            "/api/admin/oidc/policy/preview?bulk_capability=code_agents&bulk_value=true",
            headers=admin_session,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["would_gain"] == 1
        assert body["would_lose"] is None

    async def test_preview_counts_who_would_lose(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        admin_session: dict[str, str],
        session_factory: object,
    ) -> None:
        await self._plain_member(session_factory, seeded.group.id, "plain@example.org")
        async with session_factory() as session:  # type: ignore[attr-defined]
            group = await session.get(Group, seeded.group.id)
            group.code_agents_allowed = True
            await session.commit()

        response = await client.get(
            "/api/admin/oidc/policy/preview?bulk_capability=code_agents&bulk_value=false",
            headers=admin_session,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["would_lose"] == 1
        assert body["would_gain"] is None

    async def test_bulk_on_flips_every_group_and_audits(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        admin_session: dict[str, str],
        session_factory: object,
    ) -> None:
        response = await client.post(
            "/api/admin/groups/capabilities",
            json={"capability": "code_agents", "value": True},
            headers=admin_session,
        )
        assert response.status_code == 200, response.text
        assert response.json()["updated"] >= 1

        listing = (await client.get("/api/admin/groups", headers=admin_session)).json()
        assert listing["items"]
        assert all(item["code_agents_allowed"] for item in listing["items"])

        async with session_factory() as session:  # type: ignore[attr-defined]
            events = (
                (
                    await session.execute(
                        select(IdentityEvent).where(IdentityEvent.action == "group.capability_bulk")
                    )
                )
                .scalars()
                .all()
            )
        assert len(events) == 1
        assert events[0].detail == {
            "capability": "code_agents",
            "value": True,
            "count": response.json()["updated"],
        }

    async def test_bulk_off_refused_when_machine_client_unaccepted(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        admin_session: dict[str, str],
    ) -> None:
        from gateway.config import OIDCSettings

        app.state.settings.oidc = OIDCSettings(  # type: ignore[attr-defined]
            machine_client_id="other", accepted_clients="console,chat"
        )
        response = await client.post(
            "/api/admin/groups/capabilities",
            json={"capability": "code_agents", "value": False},
            headers=admin_session,
        )
        assert response.status_code == 400, response.text
        assert "MACHINE_CLIENT_ID" in response.text

    async def test_patch_off_refused_when_machine_client_unaccepted(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        admin_session: dict[str, str],
    ) -> None:
        from gateway.config import OIDCSettings

        app.state.settings.oidc = OIDCSettings(  # type: ignore[attr-defined]
            machine_client_id="other", accepted_clients="console,chat"
        )
        refused = await client.patch(
            f"/api/admin/groups/{seeded.group.id}",
            json={"code_agents_allowed": False},
            headers=admin_session,
        )
        assert refused.status_code == 400, refused.text
        assert "MACHINE_CLIENT_ID" in refused.text

        # Granting is always fine, and api_keys never touches the machine check.
        granted = await client.patch(
            f"/api/admin/groups/{seeded.group.id}",
            json={"code_agents_allowed": True},
            headers=admin_session,
        )
        assert granted.status_code == 200
        cleared = await client.patch(
            f"/api/admin/groups/{seeded.group.id}",
            json={"api_keys_allowed": False},
            headers=admin_session,
        )
        assert cleared.status_code == 200
