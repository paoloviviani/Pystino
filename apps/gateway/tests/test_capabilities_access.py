"""ADR 0096: per-group capabilities — coding agents and API keys.

Covers the pure effective-capability function, the ``/v1/me`` report, the
machine-client gate (403 ``agents_not_allowed`` on every ``/v1`` route),
the api_keys gate at mint and at use (403 ``api_keys_not_allowed``, keys
never deleted so a re-grant restores them), and the query budget staying
unchanged under both gates.
"""

from __future__ import annotations

import httpx
from conftest import Seeded, bearer_auth, make_token
from gateway.capabilities import Capabilities, effective_capabilities
from gateway.models import Group, Membership, MembershipSource, OIDCPolicyConfig, User
from gateway.oidc_policy import OIDCPolicy
from test_me_keys import as_user
from test_query_counts import counted

#: The default machine client (opencode-enrollment): the token's azp/client_id
#: that the code_agents gate keys on.
MACHINE_CLIENT = "opencode-enrollment"


def _caps(code_agents: bool, api_keys: bool) -> Capabilities:
    return Capabilities(code_agents=code_agents, api_keys=api_keys)


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
) -> Group:
    return Group(
        name=name,
        is_active=active,
        code_agents_allowed=code_agents,
        api_keys_allowed=api_keys,
    )


def _membership(group: Group, source: MembershipSource = MembershipSource.MANUAL) -> Membership:
    return Membership(group=group, source=source)


class TestEffectiveCapabilities:
    def test_everyone_mode_grants_both_to_anyone(self) -> None:
        policy = OIDCPolicy(code_agents_mode="everyone", api_keys_mode="everyone")
        lonely = _user()
        assert effective_capabilities(lonely, policy) == _caps(True, True)

    def test_groups_mode_needs_a_flagged_active_group(self) -> None:
        policy = OIDCPolicy(code_agents_mode="groups", api_keys_mode="groups")
        flagged = _group("agents", code_agents=True, api_keys=True)
        member = _user(memberships=[_membership(flagged)])
        assert effective_capabilities(member, policy) == _caps(True, True)

    def test_groups_mode_refuses_without_a_flag(self) -> None:
        policy = OIDCPolicy(code_agents_mode="groups", api_keys_mode="groups")
        plain = _group("chat")
        member = _user(memberships=[_membership(plain)])
        assert effective_capabilities(member, policy) == _caps(False, False)

    def test_flags_are_independent(self) -> None:
        policy = OIDCPolicy(code_agents_mode="groups", api_keys_mode="groups")
        group = _group("agents", code_agents=True)
        member = _user(memberships=[_membership(group)])
        assert effective_capabilities(member, policy) == _caps(True, False)

    def test_admin_is_exempt_in_groups_mode(self) -> None:
        policy = OIDCPolicy(code_agents_mode="groups", api_keys_mode="groups")
        admin = _user(is_admin=True)
        assert effective_capabilities(admin, policy) == _caps(True, True)

    def test_disabled_group_does_not_grant(self) -> None:
        policy = OIDCPolicy(code_agents_mode="groups", api_keys_mode="groups")
        disabled = _group("agents", active=False, code_agents=True, api_keys=True)
        member = _user(memberships=[_membership(disabled)])
        assert effective_capabilities(member, policy) == _caps(False, False)

    def test_inactive_user_does_not_grant(self) -> None:
        policy = OIDCPolicy(code_agents_mode="groups", api_keys_mode="groups")
        flagged = _group("agents", code_agents=True, api_keys=True)
        member = _user(is_active=False, memberships=[_membership(flagged)])
        assert effective_capabilities(member, policy) == _caps(False, False)

    def test_any_source_counts(self) -> None:
        policy = OIDCPolicy(code_agents_mode="groups", api_keys_mode="groups")
        flagged = _group("agents", code_agents=True, api_keys=True)
        oidc = _user(memberships=[_membership(flagged, MembershipSource.OIDC)])
        assert effective_capabilities(oidc, policy) == _caps(True, True)

    def test_everyone_mode_ignores_flags_and_admin(self) -> None:
        # The default must change nothing for an upgrade: flags and admin are
        # irrelevant while the setting says "everyone".
        policy = OIDCPolicy(code_agents_mode="everyone", api_keys_mode="everyone")
        plain = _group("chat")
        member = _user(memberships=[_membership(plain)])
        assert effective_capabilities(member, policy) == _caps(True, True)
        assert effective_capabilities(_user(is_admin=False), policy) == _caps(True, True)


async def _fold_policy(app: object, session_factory: object, **row: object) -> None:
    """Insert an oidc_config decision and fold it onto the worker's policy."""

    async with session_factory() as session:  # type: ignore[attr-defined]
        session.add(OIDCPolicyConfig(**row))
        await session.commit()
    await app.state.oidc_policy.refresh_once()  # type: ignore[attr-defined]


class TestV1MeReportsCapabilities:
    async def test_everyone_mode_reports_both(
        self, bearer_app: object, client: httpx.AsyncClient, signing_key: object
    ) -> None:
        response = await client.get("/v1/me", headers=bearer_auth(make_token(signing_key)))
        assert response.status_code == 200
        assert response.json()["capabilities"] == {"code_agents": True, "api_keys": True}

    async def test_groups_mode_reflects_the_persons_flags(
        self,
        bearer_app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        signing_key: object,
        session_factory: object,
    ) -> None:
        await _fold_policy(
            bearer_app,
            session_factory,
            code_agents_mode="groups",
            api_keys_mode="groups",
            group_mappings=[["research", "research"]],
        )
        response = await client.get("/v1/me", headers=bearer_auth(make_token(signing_key)))
        assert response.status_code == 200
        # The seeded user holds "research", which carries neither flag.
        assert response.json()["capabilities"] == {"code_agents": False, "api_keys": False}

        async with session_factory() as session:  # type: ignore[attr-defined]
            group = await session.get(Group, seeded.group.id)
            group.code_agents_allowed = True
            await session.commit()
        await bearer_app.state.oidc_policy.refresh_once()  # type: ignore[attr-defined]

        response = await client.get("/v1/me", headers=bearer_auth(make_token(signing_key)))
        assert response.json()["capabilities"] == {"code_agents": True, "api_keys": False}


class TestMachineClientGate:
    """A machine-client token from a person without code_agents is refused
    with 403 agents_not_allowed on every /v1 route — /v1/me included — and a
    chat-client token for the same person passes."""

    async def _deny_code_agents(self, bearer_app: object, session_factory: object) -> None:
        await _fold_policy(
            bearer_app,
            session_factory,
            code_agents_mode="groups",
            api_keys_mode="groups",
            group_mappings=[["research", "research"]],
        )

    async def test_machine_token_is_refused_everywhere(
        self,
        bearer_app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        signing_key: object,
        session_factory: object,
    ) -> None:
        await self._deny_code_agents(bearer_app, session_factory)
        machine = make_token(signing_key, azp=MACHINE_CLIENT)
        for path in ("/v1/me", "/v1/models", "/v1/me/identities"):
            response = await client.get(path, headers=bearer_auth(machine))
            assert response.status_code == 403, path
            assert response.json()["error"]["code"] == "agents_not_allowed", path

    async def test_v1_me_refusal_still_carries_capabilities(
        self,
        bearer_app: object,
        client: httpx.AsyncClient,
        signing_key: object,
        session_factory: object,
    ) -> None:
        await self._deny_code_agents(bearer_app, session_factory)
        response = await client.get(
            "/v1/me", headers=bearer_auth(make_token(signing_key, azp=MACHINE_CLIENT))
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "agents_not_allowed"
        assert response.json()["capabilities"] == {"code_agents": False, "api_keys": False}

    async def test_chat_client_token_for_the_same_user_passes(
        self,
        bearer_app: object,
        client: httpx.AsyncClient,
        signing_key: object,
        session_factory: object,
    ) -> None:
        await self._deny_code_agents(bearer_app, session_factory)
        response = await client.get("/v1/models", headers=bearer_auth(make_token(signing_key)))
        assert response.status_code == 200

    async def test_everyone_mode_changes_nothing_for_a_machine_token(
        self,
        bearer_app: object,
        client: httpx.AsyncClient,
        signing_key: object,
    ) -> None:
        response = await client.get(
            "/v1/models", headers=bearer_auth(make_token(signing_key, azp=MACHINE_CLIENT))
        )
        assert response.status_code == 200

    async def test_machine_gate_adds_no_queries(
        self,
        bearer_app: object,
        client: httpx.AsyncClient,
        signing_key: object,
        session_factory: object,
    ) -> None:
        await self._deny_code_agents(bearer_app, session_factory)
        engine = bearer_app.state.engine  # type: ignore[attr-defined]
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


async def _reload_user(session_factory: object, user_id: object) -> User:
    """A user with its memberships and groups, freshly read — the object the
    management routes would load, so in-memory flags reflect a re-grant."""
    from sqlalchemy.orm import selectinload

    async with session_factory() as session:  # type: ignore[attr-defined]
        return await session.get(
            User, user_id, options=[selectinload(User.memberships).selectinload(Membership.group)]
        )


class TestApiKeysGate:
    async def _deny_api_keys(self, app: object, session_factory: object) -> None:
        await _fold_policy(
            app,
            session_factory,
            code_agents_mode="groups",
            api_keys_mode="groups",
            group_mappings=[["research", "research"]],
        )

    async def test_mint_is_refused_and_restored_by_regrant(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: object,
    ) -> None:
        await self._deny_api_keys(app, session_factory)
        as_user(app, await _reload_user(session_factory, seeded.user.id))

        response = await client.post(
            "/api/me/keys",
            json={"name": "chat-only", "billing_group_id": str(seeded.group.id)},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "api_keys_not_allowed"

        # Regaining the capability restores minting; the denied key was never
        # created, so nothing to resurrect here.
        async with session_factory() as session:  # type: ignore[attr-defined]
            group = await session.get(Group, seeded.group.id)
            group.api_keys_allowed = True
            await session.commit()
        await app.state.oidc_policy.refresh_once()  # type: ignore[attr-defined]

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
        await self._deny_api_keys(app, session_factory)

        response = await client.get("/v1/models", headers=seeded.auth)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "api_keys_not_allowed"

        # A granted flag makes the same key work again — the key was never
        # deleted, because the loss is one a group can restore.
        async with session_factory() as session:  # type: ignore[attr-defined]
            group = await session.get(Group, seeded.group.id)
            group.api_keys_allowed = True
            await session.commit()
        await app.state.oidc_policy.refresh_once()  # type: ignore[attr-defined]

        response = await client.get("/v1/models", headers=seeded.auth)
        assert response.status_code == 200

    async def test_admin_is_exempt_at_use(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: object,
    ) -> None:
        await self._deny_api_keys(app, session_factory)
        async with session_factory() as session:  # type: ignore[attr-defined]
            user = await session.get(User, seeded.user.id)
            user.is_admin = True
            await session.commit()
        response = await client.get("/v1/models", headers=seeded.auth)
        assert response.status_code == 200
