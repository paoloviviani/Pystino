"""Per-provider identity policy (ADR 0088 draft): kind, group source, admin source."""

from __future__ import annotations

import httpx
import pytest
from conftest import Seeded
from gateway.config import OIDCSettings
from gateway.identity_policy import AdminRule, PolicyError, admin_rule, capabilities, validate
from gateway.models import Membership, User
from gateway.oidc import provision_user, sync_user_from_claims
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin

ISS = "https://sso.example.org/realms/x"


class TestCapabilities:
    def test_each_kind_offers_only_what_it_can(self) -> None:
        assert capabilities("authelia").adapters == ("authelia_file",)
        assert capabilities("keycloak").adapters == ("keycloak_admin",)
        assert capabilities("entra").adapters == ("scim",)
        assert not capabilities("authelia").subject_before_login
        assert capabilities("keycloak").subject_before_login
        assert not capabilities("google").claims_groups
        # Unknown kinds fall back to generic: JIT only, SCIM if it can push.
        assert capabilities("nonsense") == capabilities("generic")

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (("authelia", "claim", "console", "keycloak_admin", "disable"), "cannot use"),
            (("generic", "directory", "console", "none", "disable"), "needs a sync adapter"),
            (("google", "claim", "console", "none", "disable"), "no groups claim"),
            (("generic", "claim", "root", "none", "disable"), "admin_source"),
            (("martian", "claim", "console", "none", "disable"), "kind"),
        ],
    )
    def test_impossible_combinations_are_refused(self, args: tuple, message: str) -> None:
        with pytest.raises(PolicyError, match=message):
            validate(*args)

    def test_safe_defaults_validate(self) -> None:
        validate("generic", "claim", "console", "none", "disable")
        validate("authelia", "directory", "console", "authelia_file", "disable")


class TestAdminRule:
    def test_console_means_no_rule(self) -> None:
        assert admin_rule("console", "groups", ["admins"]) is None

    def test_matches_raw_or_mapped_names(self) -> None:
        rule = AdminRule(claim="groups", values=frozenset({"ops"}))
        assert rule.matches({"groups": ["ops"]})
        assert rule.matches({"groups": ["cn=ops,dc=x"]}, {"cn=ops,dc=x": "ops"})
        assert not rule.matches({"groups": ["users"]})
        nested = AdminRule(claim="realm_access.roles", values=frozenset({"admin"}))
        assert nested.matches({"realm_access": {"roles": ["admin", "user"]}})


async def _login(session: AsyncSession, subject: str, groups: list[str], **policy: object) -> User:
    claims = {"iss": ISS, "sub": subject, "groups": groups}
    user = await provision_user(
        session,
        issuer=ISS,
        subject=subject,
        email=f"{subject}@example.org",
        display_name=None,
        group_names=groups,
        settings=OIDCSettings(),
        claims=claims,
        **policy,  # type: ignore[arg-type]
    )
    await session.flush()
    return user


RULE = AdminRule(claim="groups", values=frozenset({"ops"}))


class TestAdminFromClaim:
    async def test_grant_is_recorded_as_the_directorys(self, session: AsyncSession) -> None:
        user = await _login(session, "a", ["ops"], admin_rule=RULE)
        assert user.is_admin and user.admin_source == "oidc"

    async def test_the_directory_revokes_only_what_it_granted(self, session: AsyncSession) -> None:
        keeper = await _login(session, "keeper", [])
        keeper.is_admin, keeper.admin_source = True, "manual"
        granted = await _login(session, "a", ["ops"], admin_rule=RULE)
        assert granted.is_admin
        again = await _login(session, "a", ["users"], admin_rule=RULE)
        assert not again.is_admin
        # A console-made admin is never demoted by the directory.
        manual = await _login(session, "keeper", ["users"], admin_rule=RULE)
        assert manual.is_admin

    async def test_the_last_active_admin_is_kept(self, session: AsyncSession) -> None:
        only = await _login(session, "only", ["ops"], admin_rule=RULE)
        assert only.is_admin
        still = await _login(session, "only", [], admin_rule=RULE)
        assert still.is_admin, "revoking would have left no administrator"

    async def test_admin_follows_the_refresh_policy(self, session: AsyncSession) -> None:
        from gateway.models import GroupSync

        user = await _login(
            session, "b", ["ops"], admin_rule=RULE, group_sync=GroupSync.FIRST_LOGIN
        )
        assert user.is_admin
        keeper = await _login(session, "k", [])
        keeper.is_admin = True
        later = await _login(session, "b", [], admin_rule=RULE, group_sync=GroupSync.FIRST_LOGIN)
        assert later.is_admin, "first_login: the directory answered once"

    async def test_the_bearer_path_reconciles_a_changed_admin_answer(
        self, session: AsyncSession
    ) -> None:
        keeper = await _login(session, "keeper", [])
        keeper.is_admin = True
        await _login(session, "c", ["ops"], admin_rule=RULE)
        user = await sync_user_from_claims(
            session,
            claims={"iss": ISS, "sub": "c", "groups": []},
            settings=OIDCSettings(),
            admin_rule=RULE,
        )
        assert not user.is_admin


class TestGroupSource:
    async def test_none_and_directory_ignore_the_token(self, session: AsyncSession) -> None:
        for source in ("none", "directory"):
            user = await _login(session, f"u-{source}", ["research"], group_source=source)
            memberships = (
                (await session.execute(select(Membership).where(Membership.user_id == user.id)))
                .scalars()
                .all()
            )
            assert memberships == [], source

    async def test_claim_is_the_default(self, session: AsyncSession) -> None:
        user = await _login(session, "u-claim", ["research"])
        assert {m.group.name for m in user.memberships} == {"research"}


class TestTheApi:
    async def test_create_reports_capabilities_and_refuses_an_empty_admin_rule(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        base = {
            "name": "kc",
            "issuer": ISS,
            "client_id": "gateway",
            "client_secret": "s",
            "kind": "keycloak",
        }
        bad = await client.post(
            "/api/admin/identity-providers", json={**base, "admin_source": "claim"}
        )
        assert bad.status_code == 400
        ok = await client.post(
            "/api/admin/identity-providers",
            json={**base, "admin_source": "claim", "admin_values": ["ops"]},
        )
        assert ok.status_code == 201, ok.text
        body = ok.json()
        assert body["capabilities"]["adapters"] == ["keycloak_admin"]
        assert (body["admin_source"], body["admin_values"]) == ("claim", ["ops"])
        wrong = await client.put(
            f"/api/admin/identity-providers/{body['id']}", json={"sync_adapter": "scim"}
        )
        assert wrong.status_code == 400

    async def test_the_subject_claim_is_locked_once_users_exist(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        created = await client.post(
            "/api/admin/identity-providers",
            json={"name": "e", "issuer": ISS, "client_id": "g", "client_secret": "s"},
        )
        pid = created.json()["id"]
        assert (
            await client.put(f"/api/admin/identity-providers/{pid}", json={"subject_claim": "oid"})
        ).status_code == 200
        async with session_factory() as session:
            session.add(User(issuer=ISS, subject="x"))
            await session.commit()
        locked = await client.put(
            f"/api/admin/identity-providers/{pid}", json={"subject_claim": "sub"}
        )
        assert locked.status_code == 400

    async def test_a_console_admin_grant_is_manual(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        async with session_factory() as session:
            person = User(issuer=ISS, subject="p", is_admin=True, admin_source="oidc")
            session.add(person)
            await session.commit()
            pid = person.id
        response = await client.patch(f"/api/admin/users/{pid}", json={"is_admin": True})
        assert response.status_code == 200
        async with session_factory() as session:
            assert (await session.get(User, pid)).admin_source == "manual"
