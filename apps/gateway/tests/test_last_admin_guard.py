"""The last-admin guard and the bootstrap door's no-re-arm rule (ADR 0093 §5.3, §5.5).

The console's PATCH and DELETE routes get their own HTTP-level tests in
`test_admin.py`; this file is the one query and the one door those routes,
the CLI (`test_deploy_admin.py`) and the two rule-driven revocations in
`apply_admin_answer` all share.
"""

from __future__ import annotations

from gateway.deployment_state import get_or_create_deployment_state, mark_bootstrap_consumed
from gateway.models import DeploymentState, User
from gateway.oidc import PENDING_USER_ISSUER, apply_admin_answer, other_active_admin_exists
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

ISS = "https://idp.test"


def _user(**kwargs: object) -> User:
    defaults: dict[str, object] = {"issuer": ISS, "subject": "s", "email": "a@example.org"}
    defaults.update(kwargs)
    return User(**defaults)  # type: ignore[arg-type]


class TestOtherActiveAdminExists:
    async def test_false_with_no_other_admin(self, session: AsyncSession) -> None:
        lone = _user(is_admin=True, subject="lone")
        session.add(lone)
        await session.flush()
        assert not await other_active_admin_exists(session, excluding=lone.id)

    async def test_true_with_another_active_admin(self, session: AsyncSession) -> None:
        lone = _user(is_admin=True, subject="lone")
        other = _user(is_admin=True, subject="other", email="b@example.org")
        session.add_all([lone, other])
        await session.flush()
        assert await other_active_admin_exists(session, excluding=lone.id)

    async def test_an_inactive_admin_does_not_count(self, session: AsyncSession) -> None:
        lone = _user(is_admin=True, subject="lone")
        inactive = _user(is_admin=True, is_active=False, subject="inactive", email="b@example.org")
        session.add_all([lone, inactive])
        await session.flush()
        assert not await other_active_admin_exists(session, excluding=lone.id)

    async def test_a_pending_admin_does_not_count(self, session: AsyncSession) -> None:
        # Nothing creates one yet (stage b does), but the query already
        # excludes the placeholder issuer so that stage needs no change here.
        lone = _user(is_admin=True, subject="lone")
        pending = _user(
            is_admin=True, issuer=PENDING_USER_ISSUER, subject="pending", email="b@example.org"
        )
        session.add_all([lone, pending])
        await session.flush()
        assert not await other_active_admin_exists(session, excluding=lone.id)


class TestApplyAdminAnswerGuard:
    async def test_revoke_is_kept_when_it_would_leave_no_admin(self, session: AsyncSession) -> None:
        lone = _user(is_admin=True, admin_source="env", subject="lone")
        session.add(lone)
        await session.flush()
        outcome = await apply_admin_answer(session, lone, False)
        assert outcome == "kept-last-admin"
        assert lone.is_admin

    async def test_revoke_proceeds_with_another_admin(self, session: AsyncSession) -> None:
        lone = _user(is_admin=True, admin_source="env", subject="lone")
        other = _user(is_admin=True, subject="other", email="b@example.org")
        session.add_all([lone, other])
        await session.flush()
        assert await apply_admin_answer(session, lone, False) == "revoked"
        assert not lone.is_admin

    async def test_a_grant_marks_the_bootstrap_consumed(self, session: AsyncSession) -> None:
        user = _user(subject="fresh")
        session.add(user)
        await session.flush()
        assert await apply_admin_answer(session, user, True) == "granted"
        state = await get_or_create_deployment_state(session)
        assert state.bootstrap_admin_consumed_at is not None


class TestMarkBootstrapConsumed:
    async def test_idempotent(self, session: AsyncSession) -> None:
        await mark_bootstrap_consumed(session)
        first = (await get_or_create_deployment_state(session)).bootstrap_admin_consumed_at
        await mark_bootstrap_consumed(session)
        second = (await get_or_create_deployment_state(session)).bootstrap_admin_consumed_at
        assert first == second

    async def test_the_row_does_not_exist_until_asked_for(self, session: AsyncSession) -> None:
        assert (await session.execute(select(DeploymentState))).scalar_one_or_none() is None
        await get_or_create_deployment_state(session)
        assert (await session.execute(select(DeploymentState))).scalar_one_or_none() is not None
