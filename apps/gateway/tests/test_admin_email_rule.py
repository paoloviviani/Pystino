"""`OIDC_ADMIN_EMAIL` (ADR 0093 §5.1) and the startup sweep that revokes it.

`test_identity_policy.py::TestAdminFromClaim` covers the claim rule and the
door/bearer split at length; this file is the email rule specifically, and
`sweep_env_admin_email_rule`, which only that rule can run offline.
"""

from __future__ import annotations

from gateway.config import OIDCSettings
from gateway.models import IdentityEvent, IdentityEventAction, User
from gateway.oidc import (
    apply_env_admin_rules,
    provision_user,
    sweep_env_admin_email_rule,
    sync_user_from_claims,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

ISS = "https://idp.test"


def _settings(**kwargs: object) -> OIDCSettings:
    return OIDCSettings(admin_emails="ops@example.org", **kwargs)  # type: ignore[arg-type]


async def _door_login(session: AsyncSession, subject: str, claims: dict[str, object]) -> User:
    user = await provision_user(
        session,
        issuer=ISS,
        subject=subject,
        email=claims.get("email"),  # type: ignore[arg-type]
        display_name=None,
        group_names=[],
        settings=_settings(),
        claims=claims,
        email_verified=claims.get("email_verified"),  # type: ignore[arg-type]
    )
    await session.flush()
    return user


class TestTheEmailRule:
    async def test_a_verified_listed_address_grants(self, session: AsyncSession) -> None:
        user = await _door_login(session, "a", {"email": "Ops@Example.org", "email_verified": True})
        assert user.is_admin
        assert user.admin_source == "env"
        assert user.admin_rule == "email"

    async def test_the_string_true_does_not_grant(self, session: AsyncSession) -> None:
        user = await _door_login(
            session, "a", {"email": "ops@example.org", "email_verified": "true"}
        )
        assert not user.is_admin

    async def test_an_unverified_address_does_not_grant(self, session: AsyncSession) -> None:
        user = await _door_login(
            session, "a", {"email": "ops@example.org", "email_verified": False}
        )
        assert not user.is_admin

    async def test_an_address_not_on_the_list_does_not_grant(self, session: AsyncSession) -> None:
        user = await _door_login(
            session, "a", {"email": "someone-else@example.org", "email_verified": True}
        )
        assert not user.is_admin

    async def test_a_non_ascii_address_does_not_grant(self, session: AsyncSession) -> None:
        user = await provision_user(
            session,
            issuer=ISS,
            subject="a",
            email="héllo@example.org",
            display_name=None,
            group_names=[],
            settings=OIDCSettings(admin_emails="héllo@example.org"),
            claims={"email": "héllo@example.org", "email_verified": True},
            email_verified=True,
        )
        assert not user.is_admin

    async def test_is_not_evaluated_on_a_plain_v1_call(self, session: AsyncSession) -> None:
        """ADR 0093 §5.1: the claim set on `/v1` may lack `email_verified`, so
        the email rule never runs there at all — `sync_user_from_claims` sets
        `email_rule=False` unconditionally."""
        user = await sync_user_from_claims(
            session,
            claims={
                "iss": ISS,
                "sub": "a",
                "email": "ops@example.org",
                "email_verified": True,
            },
            settings=_settings(),
        )
        assert not user.is_admin

    async def test_either_rule_grants_and_email_takes_the_label(
        self, session: AsyncSession
    ) -> None:
        settings = OIDCSettings(
            admin_emails="ops@example.org", admin_claim="groups", admin_claim_values="admins"
        )
        user = await provision_user(
            session,
            issuer=ISS,
            subject="a",
            email="ops@example.org",
            display_name=None,
            group_names=["admins"],
            settings=settings,
            claims={"email": "ops@example.org", "email_verified": True, "groups": ["admins"]},
            email_verified=True,
        )
        assert user.is_admin and user.admin_rule == "email"

    async def test_a_refusal_to_revoke_the_last_admin_is_audited(
        self, session: AsyncSession
    ) -> None:
        user = await _door_login(session, "a", {"email": "ops@example.org", "email_verified": True})
        assert user.is_admin
        again = await _door_login(
            session, "a", {"email": "ops@example.org", "email_verified": False}
        )
        assert again.is_admin, "revoking would have left no administrator"
        event = (
            await session.execute(
                select(IdentityEvent).where(
                    IdentityEvent.action == IdentityEventAction.ADMIN_REFUSED_LAST
                )
            )
        ).scalar_one()
        assert event.target_user_id == user.id

    async def test_revocation_with_a_backup_admin_is_audited_with_the_rule(
        self, session: AsyncSession
    ) -> None:
        keeper = await _door_login(
            session, "keeper", {"email": "ops@example.org", "email_verified": True}
        )
        assert keeper.is_admin
        # A second address on the list, so revoking the first still leaves one.
        settings = OIDCSettings(admin_emails="ops@example.org,ops2@example.org")
        other = await provision_user(
            session,
            issuer=ISS,
            subject="a",
            email="ops2@example.org",
            display_name=None,
            group_names=[],
            settings=settings,
            claims={"email": "ops2@example.org", "email_verified": True},
            email_verified=True,
        )
        assert other.is_admin
        revoked = await provision_user(
            session,
            issuer=ISS,
            subject="a",
            email="ops2@example.org",
            display_name=None,
            group_names=[],
            settings=OIDCSettings(admin_emails="ops@example.org"),
            claims={"email": "ops2@example.org", "email_verified": True},
            email_verified=True,
        )
        assert not revoked.is_admin
        event = (
            (
                await session.execute(
                    select(IdentityEvent)
                    .where(IdentityEvent.action == IdentityEventAction.ADMIN_REVOKE)
                    .order_by(IdentityEvent.at.desc())
                )
            )
            .scalars()
            .first()
        )
        assert event is not None
        assert event.detail == {"rule": "email"}


class TestApplyEnvAdminRulesUnchanged:
    async def test_an_already_current_grant_is_a_no_op(self, session: AsyncSession) -> None:
        user = User(issuer=ISS, subject="a", email="ops@example.org")
        session.add(user)
        await session.flush()
        outcome = await apply_env_admin_rules(
            session, user, {"email": "ops@example.org", "email_verified": True}, _settings()
        )
        assert outcome == "granted"
        again = await apply_env_admin_rules(
            session, user, {"email": "ops@example.org", "email_verified": True}, _settings()
        )
        assert again == "unchanged"

    async def test_a_manual_admin_is_untouched_by_a_non_matching_rule(
        self, session: AsyncSession
    ) -> None:
        user = User(issuer=ISS, subject="a", is_admin=True, admin_source="manual")
        session.add(user)
        await session.flush()
        outcome = await apply_env_admin_rules(
            session, user, {"email": "nobody@example.org", "email_verified": True}, _settings()
        )
        assert outcome == "unchanged"
        assert user.is_admin and user.admin_source == "manual"


class TestSweepEnvAdminEmailRule:
    async def test_revokes_an_address_dropped_from_the_list(self, session: AsyncSession) -> None:
        # A manual admin, not one the email rule granted — a stable backup the
        # sweep never touches, regardless of which candidate it visits first.
        keeper = User(issuer=ISS, subject="keeper", is_admin=True, admin_source="manual")
        gone = User(
            issuer=ISS,
            subject="gone",
            email="gone@example.org",
            email_normalized="gone@example.org",
            is_admin=True,
            admin_source="env",
            admin_rule="email",
        )
        session.add_all([keeper, gone])
        await session.commit()

        await sweep_env_admin_email_rule(session, OIDCSettings(admin_emails="keeper@example.org"))
        await session.refresh(gone)
        assert not gone.is_admin

    async def test_the_last_active_admin_is_kept(self, session: AsyncSession) -> None:
        lone = User(
            issuer=ISS,
            subject="lone",
            email="lone@example.org",
            email_normalized="lone@example.org",
            is_admin=True,
            admin_source="env",
            admin_rule="email",
        )
        session.add(lone)
        await session.commit()

        await sweep_env_admin_email_rule(session, OIDCSettings(admin_emails=""))
        await session.refresh(lone)
        assert lone.is_admin

    async def test_a_claim_granted_admin_is_never_swept(self, session: AsyncSession) -> None:
        keeper = User(
            issuer=ISS, subject="keeper", is_admin=True, admin_source="env", admin_rule="email"
        )
        claim_admin = User(
            issuer=ISS,
            subject="claim",
            email="claim@example.org",
            email_normalized="claim@example.org",
            is_admin=True,
            admin_source="env",
            admin_rule="claim",
        )
        session.add_all([keeper, claim_admin])
        await session.commit()

        await sweep_env_admin_email_rule(session, OIDCSettings(admin_emails=""))
        await session.refresh(claim_admin)
        assert claim_admin.is_admin, "the sweep only ever touches admin_rule == 'email'"

    async def test_a_manual_admin_is_never_swept(self, session: AsyncSession) -> None:
        manual = User(
            issuer=ISS, subject="m", email="m@example.org", is_admin=True, admin_source="manual"
        )
        session.add(manual)
        await session.commit()

        await sweep_env_admin_email_rule(session, OIDCSettings(admin_emails=""))
        await session.refresh(manual)
        assert manual.is_admin
