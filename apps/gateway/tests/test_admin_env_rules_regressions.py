"""Three bugs in `apply_env_admin_rules`, found on review, fixed together.

1. A rule matching a console/CLI/bootstrap (``manual``) admin must change
   nothing — a rule may only ever touch what a rule granted.
2. ``/v1`` (``email_rule=False``) must never revoke an ``admin_rule=="email"``
   grant, even when `provision_user` runs for an unrelated reason (a changed
   group, a changed username) and calls this unconditionally.
3. The hot path must not flood the audit trail: no `admin.grant` row when
   already granted, no relabelling `admin_rule` outside a sign-in, and no
   `admin.refused_last` row on `/v1` (a WARNING log only).
"""

from __future__ import annotations

from gateway.config import OIDCSettings
from gateway.models import GroupSync, IdentityEvent, IdentityEventAction, User
from gateway.oidc import apply_env_admin_rules, provision_user, sync_user_from_claims
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

ISS = "https://idp.test"


def _claim_settings() -> OIDCSettings:
    return OIDCSettings(admin_claim="groups", admin_claim_values="ops")


def _email_settings() -> OIDCSettings:
    return OIDCSettings(admin_emails="ops@example.org")


async def _count_events(session: AsyncSession, action: IdentityEventAction) -> int:
    return len(
        (
            await session.execute(select(IdentityEvent).where(IdentityEvent.action == action))
        )
        .scalars()
        .all()
    )


class TestBug1ManualAdminIsNeverConverted:
    async def test_a_matching_rule_leaves_a_manual_admin_manual(
        self, session: AsyncSession
    ) -> None:
        user = User(
            issuer=ISS, subject="a", email="ops@example.org", is_admin=True, admin_source="manual"
        )
        session.add(user)
        await session.flush()

        outcome = await apply_env_admin_rules(
            session, user, {"email": "ops@example.org", "email_verified": True}, _email_settings()
        )

        assert outcome == "unchanged"
        assert user.is_admin
        assert user.admin_source == "manual"
        assert user.admin_rule is None

    async def test_no_grant_is_audited_for_it(self, session: AsyncSession) -> None:
        user = User(
            issuer=ISS, subject="a", email="ops@example.org", is_admin=True, admin_source="manual"
        )
        session.add(user)
        await session.flush()

        await apply_env_admin_rules(
            session, user, {"email": "ops@example.org", "email_verified": True}, _email_settings()
        )

        assert await _count_events(session, IdentityEventAction.ADMIN_GRANT) == 0

    async def test_a_manual_admin_matching_the_claim_rule_via_a_door_login(
        self, session: AsyncSession
    ) -> None:
        """The same guard through `provision_user`, not just the unit call."""
        user = User(
            issuer=ISS, subject="a", email="ops@example.org", is_admin=True, admin_source="manual"
        )
        session.add(user)
        await session.flush()

        again = await provision_user(
            session,
            issuer=ISS,
            subject="a",
            email="ops@example.org",
            display_name=None,
            group_names=["ops"],
            settings=_claim_settings(),
            claims={"groups": ["ops"]},
        )
        assert again.admin_source == "manual"
        assert again.admin_rule is None


class TestBug2V1NeverRevokesAnEmailRuleGrant:
    async def test_an_unrelated_group_divergence_does_not_revoke_it(
        self, session: AsyncSession
    ) -> None:
        # A backup admin, so the last-admin guard cannot mask the bug this
        # tests: without it, "kept-last-admin" would protect `user` below for
        # the wrong reason even with the fix reverted.
        session.add(User(issuer=ISS, subject="keeper", is_admin=True, admin_source="manual"))
        await session.flush()

        # Granted at a door, by the email rule.
        user = await provision_user(
            session,
            issuer=ISS,
            subject="a",
            email="ops@example.org",
            display_name=None,
            group_names=[],
            settings=_email_settings(),
            claims={"email": "ops@example.org", "email_verified": True},
            email_verified=True,
        )
        assert user.is_admin and user.admin_rule == "email"
        await session.commit()

        # A bearer call whose claims carry no email at all (so the email rule
        # cannot match — it never runs there) and a *different* group set, so
        # `_claims_diverge` is true and `provision_user` runs regardless of
        # what `_admin_diverges` says about the admin flag on its own.
        reconciled = await sync_user_from_claims(
            session,
            claims={"iss": ISS, "sub": "a", "groups": ["research"]},
            settings=_email_settings(),
            group_sync=GroupSync.EVERY_LOGIN,
            group_source="claim",
        )
        assert reconciled.is_admin, "an email-rule grant must survive an unrelated /v1 divergence"
        assert reconciled.admin_rule == "email", "and keep its label"

    async def test_apply_env_admin_rules_directly_with_email_rule_false(
        self, session: AsyncSession
    ) -> None:
        # A backup admin: without it "kept-last-admin" would protect `user`
        # for the wrong reason even with the fix reverted.
        session.add(User(issuer=ISS, subject="keeper", is_admin=True, admin_source="manual"))
        user = User(
            issuer=ISS,
            subject="a",
            email="ops@example.org",
            is_admin=True,
            admin_source="env",
            admin_rule="email",
        )
        session.add(user)
        await session.flush()

        outcome = await apply_env_admin_rules(
            session, user, {"groups": []}, _email_settings(), email_rule=False
        )

        assert outcome == "unchanged"
        assert user.is_admin and user.admin_rule == "email"

    async def test_admin_diverges_does_not_flag_an_email_rule_admin(self) -> None:
        from gateway.oidc import _admin_diverges

        user = User(
            issuer=ISS,
            subject="a",
            is_admin=True,
            admin_source="env",
            admin_rule="email",
        )
        assert not _admin_diverges(
            user, {"groups": []}, GroupSync.EVERY_LOGIN, _email_settings(), None
        )


class TestBug3NoFloodingOrRelabellingOnTheHotPath:
    async def test_an_already_current_grant_writes_no_second_audit_row(
        self, session: AsyncSession
    ) -> None:
        user = User(issuer=ISS, subject="a", email="ops@example.org")
        session.add(user)
        await session.flush()

        first = await apply_env_admin_rules(
            session, user, {"email": "ops@example.org", "email_verified": True}, _email_settings()
        )
        second = await apply_env_admin_rules(
            session, user, {"email": "ops@example.org", "email_verified": True}, _email_settings()
        )

        assert (first, second) == ("granted", "unchanged")
        assert await _count_events(session, IdentityEventAction.ADMIN_GRANT) == 1

    async def test_admin_rule_is_not_relabelled_on_v1(self, session: AsyncSession) -> None:
        """Granted by the claim rule at a door; a bearer call where the claim
        rule *also* still matches must not touch the label — only a door may
        relabel, and only in favour of "email" when it matches too."""
        user = User(
            issuer=ISS,
            subject="a",
            is_admin=True,
            admin_source="env",
            admin_rule="claim",
        )
        session.add(user)
        await session.flush()

        outcome = await apply_env_admin_rules(
            session,
            user,
            {"groups": ["ops"]},
            _claim_settings(),
            email_rule=False,
        )

        assert outcome == "unchanged"
        assert user.admin_rule == "claim"

    async def test_a_door_may_relabel_silently_when_email_also_matches(
        self, session: AsyncSession
    ) -> None:
        user = User(
            issuer=ISS,
            subject="a",
            email="ops@example.org",
            is_admin=True,
            admin_source="env",
            admin_rule="claim",
        )
        session.add(user)
        await session.flush()

        settings = OIDCSettings(
            admin_emails="ops@example.org", admin_claim="groups", admin_claim_values="ops"
        )
        outcome = await apply_env_admin_rules(
            session,
            user,
            {"email": "ops@example.org", "email_verified": True, "groups": ["ops"]},
            settings,
            email_rule=True,
        )

        assert outcome == "unchanged"
        assert user.admin_rule == "email"
        # A label update is not a grant.
        assert await _count_events(session, IdentityEventAction.ADMIN_GRANT) == 0

    async def test_refused_last_is_not_audited_on_v1(self, session: AsyncSession) -> None:
        lone = User(
            issuer=ISS,
            subject="lone",
            is_admin=True,
            admin_source="env",
            admin_rule="claim",
        )
        session.add(lone)
        await session.flush()

        outcome = await apply_env_admin_rules(
            session, lone, {"groups": []}, _claim_settings(), email_rule=False
        )

        assert outcome == "kept-last-admin"
        assert lone.is_admin
        assert await _count_events(session, IdentityEventAction.ADMIN_REFUSED_LAST) == 0

    async def test_refused_last_is_audited_at_a_sign_in(self, session: AsyncSession) -> None:
        lone = User(
            issuer=ISS,
            subject="lone",
            is_admin=True,
            admin_source="env",
            admin_rule="claim",
        )
        session.add(lone)
        await session.flush()

        outcome = await apply_env_admin_rules(
            session, lone, {"groups": []}, _claim_settings(), email_rule=True
        )

        assert outcome == "kept-last-admin"
        assert lone.is_admin
        assert await _count_events(session, IdentityEventAction.ADMIN_REFUSED_LAST) == 1
