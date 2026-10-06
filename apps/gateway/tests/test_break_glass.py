"""`pystino break-glass` (ADR 0093 §10): the deeper recovery door.

Unit-level, against a raw session and a real `UsersFile` on a temp path — no
HTTP, no compose — because the question is entirely "given this target and
this users file, does the right account end up an active administrator with
a working bundled login", the same shape `test_bind_bundled_login.py` and
`test_deploy_admin.py` (`admin grant|revoke`) already answer for their own
doors.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

import pytest
from gateway.config import OIDCSettings, Settings
from gateway.deploy import admin as admin_module
from gateway.deploy.admin import AdminCommandError, break_glass
from gateway.deploy.cli import build_parser
from gateway.deployment_state import get_or_create_deployment_state
from gateway.directory.authelia_users import UsersFile
from gateway.models import (
    DirectoryEntry,
    IdentityEvent,
    IdentityEventAction,
    IdentityProvider,
    User,
)
from gateway.secrets import SecretBox
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

BOX = SecretBox(["test-encryption-key-not-for-production"])
ISS = "https://gw.example.org/authelia"

#: `oidc.enabled` defaults to `False`, so `reseed_from_env` (called at the
#: top of `break_glass`) is a no-op against this and leaves a manually seeded
#: `IdentityProvider` row exactly as the test set it up -- the ordinary case,
#: where `./configure --break-glass` already ran and the row from a previous
#: bundled period (or the very first one) is what break-glass reuses.
NO_ENV_OIDC = Settings()

SEED = """users:
  admin:
    disabled: false
    displayname: Admin
    password: '$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA'
    email: admin@example.org
    groups: [users]
"""


async def _bundled_provider(session: AsyncSession, tmp_path: Path) -> IdentityProvider:
    path = tmp_path / "users_database.yml"
    path.write_text(SEED)
    row = IdentityProvider(
        name="default",
        issuer=ISS,
        client_id="pystino-console",
        client_secret_encrypted=BOX.encrypt("s"),
        scopes=["openid"],
        groups_claim="groups",
        fetch_userinfo=False,
        group_mappings=[],
        link_by_email=False,
        kind="authelia",
        group_source="none",
        group_sync="never",
        sync_adapter="none",
        admin_source="console",
        admin_values=[],
        sync_deprovision="disable",
        sync_create_users=True,
        sync_confirmed=False,
        sync_interval_minutes=60,
        sync_config_encrypted=BOX.encrypt(json.dumps({"path": str(path)})),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


def _read_file(tmp_path: Path) -> dict:
    import yaml

    return yaml.safe_load((tmp_path / "users_database.yml").read_text(encoding="utf-8"))


def _user(issuer: str, subject: str, email: str) -> User:
    """A signed-in-once account: `email_normalized` is always populated by
    then (`provision_user`/`sign_in` write it alongside `email`), so a test
    standing in for "an existing account" must set it too -- `break_glass`
    matches on the normalised column, never the raw one."""
    return User(issuer=issuer, subject=subject, email=email, email_normalized=email.casefold())


class TestTarget:
    async def test_no_match_creates_a_pending_user(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        await _bundled_provider(session, tmp_path)
        result = await break_glass(
            session,
            NO_ENV_OIDC,
            BOX,
            email="new@example.org",
            login=None,
            user_id=None,
            reason="lost every admin",
        )
        assert result.login_created
        user = (
            await session.execute(select(User).where(User.email_normalized == "new@example.org"))
        ).scalar_one()
        assert user.is_admin and user.is_active and user.admin_source == "manual"
        assert user.issuer == "pystino:pending"

        # ADR 0093 to-do item 1: a pending user has never had a membership,
        # and being an administrator authenticates through the console's
        # session cookie, never a billing group -- but this person may still
        # want to call `/v1` directly.
        await session.refresh(user, attribute_names=["memberships"])
        assert {m.group.name for m in user.memberships} == {"users"}
        assert user.default_billing_group_id == user.memberships[0].group_id

    async def test_one_match_is_used_outright(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        await _bundled_provider(session, tmp_path)
        session.add(_user("https://old.example.org", "s1", "ops@example.org"))
        await session.commit()

        result = await break_glass(
            session,
            NO_ENV_OIDC,
            BOX,
            email="Ops@Example.org",
            login=None,
            user_id=None,
            reason="lost every admin",
        )
        user = (
            await session.execute(select(User).where(User.email_normalized == "ops@example.org"))
        ).scalar_one()
        assert user.is_admin and user.is_active
        assert result.login_created

    async def test_several_matches_require_user_id(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        await _bundled_provider(session, tmp_path)
        session.add_all(
            [
                _user("https://a.example.org", "1", "dup@example.org"),
                _user("https://b.example.org", "2", "dup@example.org"),
            ]
        )
        await session.commit()
        with pytest.raises(AdminCommandError, match="pass --user-id"):
            await break_glass(
                session,
                NO_ENV_OIDC,
                BOX,
                email="dup@example.org",
                login=None,
                user_id=None,
                reason="lost every admin",
            )

        target = (
            await session.execute(
                select(User).where(User.issuer == "https://b.example.org")
            )
        ).scalar_one()
        result = await break_glass(
            session,
            NO_ENV_OIDC,
            BOX,
            email="dup@example.org",
            login=None,
            user_id=target.id,
            reason="lost every admin",
        )
        assert result.user.id == target.id
        await session.refresh(target)
        assert target.is_admin

    async def test_an_unknown_user_id_is_refused(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        await _bundled_provider(session, tmp_path)
        session.add_all(
            [
                _user("https://a.example.org", "1", "dup@example.org"),
                _user("https://b.example.org", "2", "dup@example.org"),
            ]
        )
        await session.commit()
        with pytest.raises(AdminCommandError, match="does not name"):
            await break_glass(
                session,
                NO_ENV_OIDC,
                BOX,
                email="dup@example.org",
                login=None,
                user_id=uuid.uuid4(),
                reason="lost every admin",
            )


class TestLogin:
    async def test_reuses_a_bound_login_re_enabling_and_resetting_it(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        provider = await _bundled_provider(session, tmp_path)
        target = _user("https://old.example.org", "s1", "alice@example.org")
        session.add(target)
        await session.flush()
        session.add(
            DirectoryEntry(
                provider_id=provider.id,
                external_id="alice",
                username="alice",
                email="alice@example.org",
                user_id=target.id,
            )
        )
        # The line itself, disabled -- as an operator might leave a login
        # they no longer expected to need. §10 re-enables it unconditionally
        # regardless of its current state.
        users_file = UsersFile(tmp_path / "users_database.yml")
        users_file.create("alice", "alice@example.org", "Alice")
        users_file.update("alice", disabled=True)
        await session.commit()

        result = await break_glass(
            session,
            NO_ENV_OIDC,
            BOX,
            email="alice@example.org",
            login=None,
            user_id=None,
            reason="lost every admin",
        )
        assert not result.login_created
        assert result.login == "alice"
        data = _read_file(tmp_path)
        assert data["users"]["alice"]["disabled"] is False
        # The file has exactly one entry for "alice" -- reusing an entry
        # never creates a second directory_entries row for the same login.
        rows = (
            await session.execute(
                select(DirectoryEntry).where(DirectoryEntry.external_id == "alice")
            )
        ).scalars().all()
        assert len(rows) == 1

    async def test_creates_a_derived_login_when_none_is_bound(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        await _bundled_provider(session, tmp_path)
        session.add(_user("https://old.example.org", "s1", "bob@example.org"))
        await session.commit()

        result = await break_glass(
            session,
            NO_ENV_OIDC,
            BOX,
            email="bob@example.org",
            login=None,
            user_id=None,
            reason="lost every admin",
        )
        assert result.login_created
        assert result.login == "bob"
        data = _read_file(tmp_path)
        assert data["users"]["bob"]["groups"] == ["users"]
        assert data["users"]["bob"]["email"] == "bob@example.org"

    async def test_an_explicit_login_is_honoured(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        await _bundled_provider(session, tmp_path)
        session.add(_user("https://old.example.org", "s1", "carl@example.org"))
        await session.commit()

        result = await break_glass(
            session,
            NO_ENV_OIDC,
            BOX,
            email="carl@example.org",
            login="ops",
            user_id=None,
            reason="lost every admin",
        )
        assert result.login == "ops"

    async def test_a_taken_login_name_is_derived_around(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        # The seed file already carries "admin"; a target whose local part is
        # also "admin" must not collide with it.
        await _bundled_provider(session, tmp_path)
        session.add(_user("https://old.example.org", "s1", "admin@other.org"))
        await session.commit()

        result = await break_glass(
            session,
            NO_ENV_OIDC,
            BOX,
            email="admin@other.org",
            login=None,
            user_id=None,
            reason="lost every admin",
        )
        assert result.login == "admin-2"


class TestAuditAndBootstrap:
    async def test_marks_the_bootstrap_consumed_and_is_audited(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        await _bundled_provider(session, tmp_path)
        session.add(_user("https://old.example.org", "s1", "ops@example.org"))
        await session.commit()

        await break_glass(
            session,
            NO_ENV_OIDC,
            BOX,
            email="ops@example.org",
            login=None,
            user_id=None,
            reason="every admin left the org",
        )

        state = await get_or_create_deployment_state(session)
        assert state.bootstrap_admin_consumed_at is not None

        row = (
            await session.execute(
                select(IdentityEvent).where(IdentityEvent.action == IdentityEventAction.BREAK_GLASS)
            )
        ).scalar_one()
        assert row.reason == "every admin left the org"
        assert row.actor_type.value == "cli"
        assert row.detail == {"login_new": True}

    @pytest.mark.parametrize("reason", [None, "", "   "])
    async def test_reason_is_optional(
        self, session: AsyncSession, tmp_path: Path, reason: str | None
    ) -> None:
        await _bundled_provider(session, tmp_path)
        kwargs = {} if reason is None else {"reason": reason}
        result = await break_glass(
            session,
            NO_ENV_OIDC,
            BOX,
            email="ops@example.org",
            login=None,
            user_id=None,
            **kwargs,
        )
        assert result.login_created

        row = (
            await session.execute(
                select(IdentityEvent).where(IdentityEvent.action == IdentityEventAction.BREAK_GLASS)
            )
        ).scalar_one()
        assert not row.reason

    def test_the_cli_takes_break_glass_without_a_reason(self) -> None:
        args = build_parser().parse_args(["break-glass", "--email", "ops@example.org"])
        assert args.reason == ""

    async def test_the_password_is_never_logged(
        self, session: AsyncSession, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        await _bundled_provider(session, tmp_path)
        session.add(_user("https://old.example.org", "s1", "dana@example.org"))
        await session.commit()

        with caplog.at_level(logging.DEBUG):
            result = await break_glass(
                session,
                NO_ENV_OIDC,
                BOX,
                email="dana@example.org",
                login=None,
                user_id=None,
                reason="lost every admin",
            )
        assert result.password
        assert all(result.password not in record.getMessage() for record in caplog.records)


class TestNoProviderRowYet:
    async def test_self_reseeds_before_a_compose_up_ever_would(
        self, session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The order `./configure --break-glass` runs in (ADR 0093 §10 host
        steps): `.env` is rewritten to the bundled Authelia (step 2), then
        this command runs via `docker compose run --rm --no-deps gateway`
        (step 4) *before* `docker compose up -d --wait` (step 5) would
        otherwise reseed the provider table. No `IdentityProvider` row
        exists yet -- this command must produce one itself.

        The freshly reseeded row carries no `sync_config_encrypted` (it is
        an environment-sourced row, not a console-configured one), so
        `bundled_users_file` would resolve the real container's mount point
        (`/authelia/users_database.yml`) -- correct in production, where the
        gateway and bootstrap containers share that mount, but not
        reachable from a unit test. Patched here to the temp path instead;
        the point under test is that a provider row exists to resolve at
        all, not the mount path itself, which nothing here changes.
        """
        path = tmp_path / "users_database.yml"
        path.write_text(SEED)
        monkeypatch.setattr(
            admin_module, "bundled_users_file", lambda row, secrets: UsersFile(path)
        )
        env = Settings(
            oidc=OIDCSettings(
                enabled=True,
                issuer=ISS,
                client_id="pystino-console",
                kind="authelia",
                internal_base_url="http://authelia:9091/authelia",
            )
        )

        result = await break_glass(
            session,
            env,
            BOX,
            email="ops@example.org",
            login=None,
            user_id=None,
            reason="fresh bundled switch, no prior row",
        )
        assert result.login_created
        row = (await session.execute(select(IdentityProvider))).scalar_one()
        assert row.kind == "authelia" and row.is_enabled
