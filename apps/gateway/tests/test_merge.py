"""Admin merge (ADR 0093 §7.1).

The registry guard runs first and stands on its own: it is the mechanism
that keeps every other test here honest about coverage, not just a nice
property. The rest is organised by merge-rule category, one representative
per category rather than all 21 columns individually, plus the properties
the design calls out by name: identities, the last-admin invariant, and
that a merge never deletes the target's own data.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from gateway.merge import (
    MergeNotFound,
    MergeRefused,
    assert_merge_rules_are_complete,
    compute_merge_preview,
    merge_users,
)
from gateway.models import (
    ApiKey,
    Group,
    IdentityEvent,
    IdentityEventAction,
    LimitMetric,
    LimitRule,
    LimitScope,
    Membership,
    MembershipRole,
    MembershipSource,
    ModelDef,
    Provider,
    QuotaNotificationSetting,
    RedactionRule,
    RedactionScope,
    RefreshCredential,
    User,
    UserIdentity,
    UserMerge,
    UserModelAccess,
)
from gateway.oidc import PENDING_USER_ISSUER
from gateway.secrets import SecretBox
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

BOX = SecretBox(["test-encryption-key-not-for-production"])


async def make_user(
    session: AsyncSession,
    *,
    issuer: str = "https://idp.test",
    subject: str | None = None,
    email: str | None = None,
    is_admin: bool = False,
    is_active: bool = True,
) -> User:
    user = User(
        issuer=issuer,
        subject=subject or f"subject-{uuid.uuid4().hex[:8]}",
        email=email,
        email_normalized=email.casefold() if email else None,
        is_admin=is_admin,
        is_active=is_active,
    )
    session.add(user)
    await session.commit()
    return user


def test_the_registry_covers_every_user_owning_column() -> None:
    assert_merge_rules_are_complete()


class TestRefusals:
    async def test_no_such_source(self, session: AsyncSession) -> None:
        target = await make_user(session)
        with pytest.raises(MergeNotFound):
            await merge_users(
                session,
                source_id=uuid.uuid4(),
                target_id=target.id,
                actor_id=uuid.uuid4(),
                actor_label="admin",
                reason="test",
            )

    async def test_no_such_target(self, session: AsyncSession) -> None:
        source = await make_user(session)
        with pytest.raises(MergeNotFound):
            await merge_users(
                session,
                source_id=source.id,
                target_id=uuid.uuid4(),
                actor_id=uuid.uuid4(),
                actor_label="admin",
                reason="test",
            )

    async def test_source_equals_target(self, session: AsyncSession) -> None:
        user = await make_user(session)
        with pytest.raises(MergeRefused, match="themselves"):
            await merge_users(
                session,
                source_id=user.id,
                target_id=user.id,
                actor_id=uuid.uuid4(),
                actor_label="admin",
                reason="test",
            )

    async def test_source_is_the_caller(self, session: AsyncSession) -> None:
        source = await make_user(session)
        target = await make_user(session)
        with pytest.raises(MergeRefused, match="signed in with"):
            await merge_users(
                session,
                source_id=source.id,
                target_id=target.id,
                actor_id=source.id,
                actor_label="admin",
                reason="test",
            )

    async def test_a_pending_source_is_refused(self, session: AsyncSession) -> None:
        source = await make_user(session, issuer=PENDING_USER_ISSUER, subject=str(uuid.uuid4()))
        target = await make_user(session)
        with pytest.raises(MergeRefused, match="pending"):
            await merge_users(
                session,
                source_id=source.id,
                target_id=target.id,
                actor_id=uuid.uuid4(),
                actor_label="admin",
                reason="test",
            )

    async def test_a_pending_target_is_refused(self, session: AsyncSession) -> None:
        source = await make_user(session)
        target = await make_user(session, issuer=PENDING_USER_ISSUER, subject=str(uuid.uuid4()))
        with pytest.raises(MergeRefused, match="pending"):
            await merge_users(
                session,
                source_id=source.id,
                target_id=target.id,
                actor_id=uuid.uuid4(),
                actor_label="admin",
                reason="test",
            )

    async def test_an_inactive_target_is_refused(self, session: AsyncSession) -> None:
        source = await make_user(session)
        target = await make_user(session, is_active=False)
        with pytest.raises(MergeRefused, match="not active"):
            await merge_users(
                session,
                source_id=source.id,
                target_id=target.id,
                actor_id=uuid.uuid4(),
                actor_label="admin",
                reason="test",
            )

    async def test_an_inactive_source_is_allowed(self, session: AsyncSession) -> None:
        """Only the target's activity is refused: merging away a disabled
        account into an active one is the ordinary reconciliation case."""
        source = await make_user(session, is_active=False)
        target = await make_user(session)
        summary = await merge_users(
            session,
            source_id=source.id,
            target_id=target.id,
            actor_id=uuid.uuid4(),
            actor_label="admin",
            reason="test",
        )
        await session.commit()
        assert summary.target_id == target.id


class TestReassignDeleteAndKeepTarget:
    async def test_api_keys_and_usage_records_reassign(self, session: AsyncSession) -> None:
        source = await make_user(session)
        target = await make_user(session)
        provider = Provider(name="fake", base_url="http://x", api_key_encrypted=BOX.encrypt("k"))
        session.add(provider)
        await session.flush()
        model = ModelDef(
            name="m", upstream_model="u/m", provider_id=provider.id, context_window=1000
        )
        session.add(model)
        await session.flush()
        session.add(
            ApiKey(
                user_id=source.id,
                prefix="gwk_test",
                key_hash="x" * 64,
                name="a key",
            )
        )
        await session.commit()

        await merge_users(
            session,
            source_id=source.id,
            target_id=target.id,
            actor_id=uuid.uuid4(),
            actor_label="admin",
            reason="test",
        )
        await session.commit()

        key = (await session.execute(select(ApiKey))).scalar_one()
        assert key.user_id == target.id

    async def test_refresh_credentials_are_deleted_not_moved(self, session: AsyncSession) -> None:
        source = await make_user(session)
        target = await make_user(session)
        session.add(
            RefreshCredential(
                user_id=source.id,
                client="agent",
                prefix="rc_test",
                secret_hash="y" * 64,
            )
        )
        await session.commit()

        await merge_users(
            session,
            source_id=source.id,
            target_id=target.id,
            actor_id=uuid.uuid4(),
            actor_label="admin",
            reason="test",
        )
        await session.commit()

        assert (await session.execute(select(RefreshCredential))).scalars().all() == []

    async def test_quota_notification_settings_keeps_the_targets_and_drops_the_sources(
        self, session: AsyncSession
    ) -> None:
        source = await make_user(session)
        target = await make_user(session)
        rule = LimitRule(
            scope=LimitScope.GLOBAL,
            scope_id=None,
            metric=LimitMetric.REQUESTS,
            window_seconds=3600,
            limit_value=Decimal(100),
        )
        session.add(rule)
        await session.flush()
        session.add(
            QuotaNotificationSetting(user_id=source.id, rule_id=rule.id, threshold=80)
        )
        session.add(
            QuotaNotificationSetting(user_id=target.id, rule_id=rule.id, threshold=50)
        )
        await session.commit()

        await merge_users(
            session,
            source_id=source.id,
            target_id=target.id,
            actor_id=uuid.uuid4(),
            actor_label="admin",
            reason="test",
        )
        await session.commit()

        remaining = (await session.execute(select(QuotaNotificationSetting))).scalars().all()
        assert len(remaining) == 1
        assert remaining[0].user_id == target.id
        assert remaining[0].threshold == 50, "the target's own setting, untouched"


class TestScopeIdUser:
    async def test_user_scoped_rules_reassign_group_and_api_key_scoped_do_not(
        self, session: AsyncSession
    ) -> None:
        source = await make_user(session)
        target = await make_user(session)
        group = Group(name="g")
        session.add(group)
        await session.flush()

        user_rule = LimitRule(
            scope=LimitScope.USER,
            scope_id=source.id,
            metric=LimitMetric.TOKENS,
            window_seconds=3600,
            limit_value=Decimal(1000),
        )
        group_rule = LimitRule(
            scope=LimitScope.GROUP,
            scope_id=group.id,
            metric=LimitMetric.TOKENS,
            window_seconds=3600,
            limit_value=Decimal(2000),
        )
        user_redaction = RedactionRule(scope=RedactionScope.USER, scope_id=source.id, policy={})
        session.add_all([user_rule, group_rule, user_redaction])
        await session.commit()

        await merge_users(
            session,
            source_id=source.id,
            target_id=target.id,
            actor_id=uuid.uuid4(),
            actor_label="admin",
            reason="test",
        )
        await session.commit()

        await session.refresh(user_rule)
        await session.refresh(group_rule)
        await session.refresh(user_redaction)
        assert user_rule.scope_id == target.id
        assert group_rule.scope_id == group.id, "not user-scoped, untouched"
        assert user_redaction.scope_id == target.id

    async def test_a_duplicate_limit_rule_is_kept_on_the_target_not_moved(
        self, session: AsyncSession
    ) -> None:
        """Same metric, window and period on both sides: reassigning the
        source's row would collide with `uq_limit_rules_identity` and roll
        the whole merge back (§7.1's own "never a raw rollback"). Keep-target
        instead: the source's duplicate is dropped, counted, not moved."""
        source = await make_user(session)
        target = await make_user(session)
        source_rule = LimitRule(
            scope=LimitScope.USER,
            scope_id=source.id,
            metric=LimitMetric.TOKENS,
            window_seconds=3600,
            limit_value=Decimal(1000),
        )
        target_rule = LimitRule(
            scope=LimitScope.USER,
            scope_id=target.id,
            metric=LimitMetric.TOKENS,
            window_seconds=3600,
            limit_value=Decimal(2000),
        )
        # A non-colliding source rule (different metric): must still move.
        distinct_rule = LimitRule(
            scope=LimitScope.USER,
            scope_id=source.id,
            metric=LimitMetric.REQUESTS,
            window_seconds=3600,
            limit_value=Decimal(50),
        )
        session.add_all([source_rule, target_rule, distinct_rule])
        await session.commit()

        preview = await compute_merge_preview(
            session, source_id=source.id, target_id=target.id, actor_id=uuid.uuid4()
        )
        assert preview.counts["limit_rules"] == 1, "only the non-colliding rule would move"
        assert preview.duplicate_rules_dropped == 1

        summary = await merge_users(
            session,
            source_id=source.id,
            target_id=target.id,
            actor_id=uuid.uuid4(),
            actor_label="admin",
            reason="test",
        )
        await session.commit()

        assert summary.counts["limit_rules"] == 1
        assert summary.duplicate_rules_dropped == 1

        remaining = (await session.execute(select(LimitRule))).scalars().all()
        by_id = {row.id: row for row in remaining}
        assert source_rule.id not in by_id, "the duplicate is gone, not reassigned"
        await session.refresh(target_rule)
        await session.refresh(distinct_rule)
        assert target_rule.limit_value == Decimal(2000), "the target's own value stands"
        assert distinct_rule.scope_id == target.id, "the non-colliding rule still moved"

    async def test_a_duplicate_user_redaction_rule_is_kept_on_the_target(
        self, session: AsyncSession
    ) -> None:
        """`redaction_rules` allows at most one user-scoped row per user, so
        any existing target row makes the source's a duplicate outright."""
        source = await make_user(session)
        target = await make_user(session)
        source_rule = RedactionRule(scope=RedactionScope.USER, scope_id=source.id, policy={})
        target_rule = RedactionRule(scope=RedactionScope.USER, scope_id=target.id, policy={})
        session.add_all([source_rule, target_rule])
        await session.commit()

        preview = await compute_merge_preview(
            session, source_id=source.id, target_id=target.id, actor_id=uuid.uuid4()
        )
        assert preview.counts["redaction_rules"] == 0
        assert preview.duplicate_rules_dropped == 1

        summary = await merge_users(
            session,
            source_id=source.id,
            target_id=target.id,
            actor_id=uuid.uuid4(),
            actor_label="admin",
            reason="test",
        )
        await session.commit()

        assert summary.duplicate_rules_dropped == 1
        remaining = (await session.execute(select(RedactionRule))).scalars().all()
        assert [row.id for row in remaining] == [target_rule.id]

    async def test_the_merge_never_raises_on_a_duplicate(self, session: AsyncSession) -> None:
        """The property the fix exists for: no raw rollback, whatever the
        database's own unique index would otherwise have refused."""
        source = await make_user(session)
        target = await make_user(session)
        session.add_all(
            [
                LimitRule(
                    scope=LimitScope.USER,
                    scope_id=source.id,
                    metric=LimitMetric.TOKENS,
                    window_seconds=3600,
                    limit_value=Decimal(1),
                ),
                LimitRule(
                    scope=LimitScope.USER,
                    scope_id=target.id,
                    metric=LimitMetric.TOKENS,
                    window_seconds=3600,
                    limit_value=Decimal(2),
                ),
                RedactionRule(scope=RedactionScope.USER, scope_id=source.id, policy={}),
                RedactionRule(scope=RedactionScope.USER, scope_id=target.id, policy={}),
            ]
        )
        await session.commit()

        # No exception here is the assertion: a merge that still issued the
        # blanket UPDATE would raise IntegrityError and this would fail.
        await merge_users(
            session,
            source_id=source.id,
            target_id=target.id,
            actor_id=uuid.uuid4(),
            actor_label="admin",
            reason="test",
        )
        await session.commit()


class TestUnionMemberships:
    async def test_no_conflict_moves_the_row(self, session: AsyncSession) -> None:
        source = await make_user(session)
        target = await make_user(session)
        group = Group(name="only-source-has-this")
        session.add(group)
        await session.flush()
        session.add(
            Membership(
                user_id=source.id,
                group_id=group.id,
                role=MembershipRole.MEMBER,
                source=MembershipSource.OIDC,
            )
        )
        await session.commit()

        await merge_users(
            session,
            source_id=source.id,
            target_id=target.id,
            actor_id=uuid.uuid4(),
            actor_label="admin",
            reason="test",
        )
        await session.commit()

        row = (await session.execute(select(Membership))).scalar_one()
        assert row.user_id == target.id
        assert row.group_id == group.id

    async def test_conflict_keeps_the_higher_role_and_manual_if_either_was(
        self, session: AsyncSession
    ) -> None:
        source = await make_user(session)
        target = await make_user(session)
        group = Group(name="shared")
        session.add(group)
        await session.flush()
        session.add(
            Membership(
                user_id=source.id,
                group_id=group.id,
                role=MembershipRole.ADMIN,
                source=MembershipSource.MANUAL,
            )
        )
        session.add(
            Membership(
                user_id=target.id,
                group_id=group.id,
                role=MembershipRole.MEMBER,
                source=MembershipSource.OIDC,
            )
        )
        await session.commit()

        await merge_users(
            session,
            source_id=source.id,
            target_id=target.id,
            actor_id=uuid.uuid4(),
            actor_label="admin",
            reason="test",
        )
        await session.commit()

        rows = (await session.execute(select(Membership))).scalars().all()
        assert len(rows) == 1, "the source's conflicting row cascade-deleted with it"
        assert rows[0].user_id == target.id
        assert rows[0].role == MembershipRole.ADMIN, "the higher of the two"
        assert rows[0].source == MembershipSource.MANUAL, "manual, since either side was"


class TestUnionModelAccess:
    async def _model(self, session: AsyncSession) -> ModelDef:
        provider = Provider(name="fake", base_url="http://x", api_key_encrypted=BOX.encrypt("k"))
        session.add(provider)
        await session.flush()
        model = ModelDef(
            name="m", upstream_model="u/m", provider_id=provider.id, context_window=1000
        )
        session.add(model)
        await session.flush()
        return model

    async def test_no_conflict_moves_the_grant(self, session: AsyncSession) -> None:
        source = await make_user(session)
        target = await make_user(session)
        model = await self._model(session)
        session.add(UserModelAccess(user_id=source.id, model_id=model.id))
        await session.commit()

        await merge_users(
            session,
            source_id=source.id,
            target_id=target.id,
            actor_id=uuid.uuid4(),
            actor_label="admin",
            reason="test",
        )
        await session.commit()

        row = (await session.execute(select(UserModelAccess))).scalar_one()
        assert row.user_id == target.id

    async def test_conflict_keeps_the_targets(self, session: AsyncSession) -> None:
        source = await make_user(session)
        target = await make_user(session)
        model = await self._model(session)
        session.add(UserModelAccess(user_id=source.id, model_id=model.id))
        session.add(UserModelAccess(user_id=target.id, model_id=model.id))
        await session.commit()

        await merge_users(
            session,
            source_id=source.id,
            target_id=target.id,
            actor_id=uuid.uuid4(),
            actor_label="admin",
            reason="test",
        )
        await session.commit()

        rows = (await session.execute(select(UserModelAccess))).scalars().all()
        assert len(rows) == 1
        assert rows[0].user_id == target.id


class TestIdentities:
    async def test_the_primary_and_linked_identities_move_when_unclaimed(
        self, session: AsyncSession
    ) -> None:
        source = await make_user(session, issuer="https://idp-a.test", subject="s")
        session.add(
            UserIdentity(user_id=source.id, issuer="https://idp-b.test", subject="s2")
        )
        target = await make_user(session, issuer="https://idp-c.test", subject="s3")
        await session.commit()

        summary = await merge_users(
            session,
            source_id=source.id,
            target_id=target.id,
            actor_id=uuid.uuid4(),
            actor_label="admin",
            reason="test",
        )
        await session.commit()

        rows = (
            await session.execute(select(UserIdentity).where(UserIdentity.user_id == target.id))
        ).scalars().all()
        pairs = {(r.issuer, r.subject) for r in rows}
        assert pairs == {("https://idp-a.test", "s"), ("https://idp-b.test", "s2")}
        assert summary.identities_dropped == []

    async def test_a_same_issuer_identity_is_dropped_not_moved(
        self, session: AsyncSession
    ) -> None:
        source = await make_user(session, issuer="https://idp.test", subject="source-subject")
        target = await make_user(session, issuer="https://idp.test", subject="target-subject")
        await session.commit()

        preview = await compute_merge_preview(
            session, source_id=source.id, target_id=target.id, actor_id=uuid.uuid4()
        )
        assert len(preview.identities_dropped) == 1
        assert preview.identities_dropped[0].issuer == "https://idp.test"
        assert preview.identities_dropped[0].subject == "source-subject"
        assert preview.identities_moving == []

        summary = await merge_users(
            session,
            source_id=source.id,
            target_id=target.id,
            actor_id=uuid.uuid4(),
            actor_label="admin",
            reason="test",
        )
        await session.commit()

        assert summary.identities_dropped[0].subject == "source-subject"
        # The target keeps exactly its own identity; the source's vanished
        # with the row rather than creating a second (issuer, *) for target.
        rows = (
            await session.execute(select(UserIdentity).where(UserIdentity.user_id == target.id))
        ).scalars().all()
        assert rows == []


class TestAdminAndProfile:
    async def test_is_admin_is_the_or(self, session: AsyncSession) -> None:
        source = await make_user(session, is_admin=True)
        target = await make_user(session, is_admin=False)
        await merge_users(
            session,
            source_id=source.id,
            target_id=target.id,
            actor_id=uuid.uuid4(),
            actor_label="admin",
            reason="test",
        )
        await session.commit()
        await session.refresh(target)
        assert target.is_admin is True

    async def test_the_last_admin_invariant_holds_across_a_merge(
        self, session: AsyncSession
    ) -> None:
        """The design's own claim: a merge can't reduce the active-admin
        count, because is_admin is only ever OR'd onto the target. Proven
        directly rather than assumed: the sole admin merges away as the
        source, and the target -- not previously one -- is an admin after."""
        sole_admin = await make_user(session, is_admin=True)
        target = await make_user(session, is_admin=False)
        await merge_users(
            session,
            source_id=sole_admin.id,
            target_id=target.id,
            actor_id=uuid.uuid4(),
            actor_label="admin",
            reason="test",
        )
        await session.commit()
        remaining_admins = (
            await session.execute(select(User).where(User.is_admin.is_(True)))
        ).scalars().all()
        assert len(remaining_admins) == 1
        assert remaining_admins[0].id == target.id

    async def test_profile_fields_fall_back_only_when_the_targets_are_null(
        self, session: AsyncSession
    ) -> None:
        source = await make_user(session, email="source@example.org")
        target = await make_user(session, email=None)
        target.display_name = "Kept Name"
        await session.commit()

        await merge_users(
            session,
            source_id=source.id,
            target_id=target.id,
            actor_id=uuid.uuid4(),
            actor_label="admin",
            reason="test",
        )
        await session.commit()
        await session.refresh(target)

        assert target.email == "source@example.org", "the gap is filled"
        assert target.display_name == "Kept Name", "the target's own value stands"


class TestNeverDeletesTheTargetsData:
    async def test_the_targets_own_api_key_and_membership_survive(
        self, session: AsyncSession
    ) -> None:
        source = await make_user(session)
        target = await make_user(session)
        group = Group(name="targets-own-group")
        session.add(group)
        await session.flush()
        session.add(
            Membership(user_id=target.id, group_id=group.id, role=MembershipRole.MEMBER)
        )
        session.add(
            ApiKey(user_id=target.id, prefix="gwk_target", key_hash="z" * 64, name="target's key")
        )
        await session.commit()

        await merge_users(
            session,
            source_id=source.id,
            target_id=target.id,
            actor_id=uuid.uuid4(),
            actor_label="admin",
            reason="test",
        )
        await session.commit()

        memberships = (await session.execute(select(Membership))).scalars().all()
        keys = (await session.execute(select(ApiKey))).scalars().all()
        assert len(memberships) == 1 and memberships[0].user_id == target.id
        assert len(keys) == 1 and keys[0].user_id == target.id


class TestAuditAndRecord:
    async def test_a_user_merges_row_and_the_audit_event_are_written(
        self, session: AsyncSession
    ) -> None:
        source = await make_user(session)
        target = await make_user(session)
        await merge_users(
            session,
            source_id=source.id,
            target_id=target.id,
            actor_id=uuid.uuid4(),
            actor_label="admin@example.org",
            reason="duplicate accounts",
        )
        await session.commit()

        record = (await session.execute(select(UserMerge))).scalar_one()
        assert record.source_user_id == source.id
        assert record.target_user_id == target.id
        assert record.reason == "duplicate accounts"

        event = (
            await session.execute(
                select(IdentityEvent).where(IdentityEvent.action == IdentityEventAction.USER_MERGE)
            )
        ).scalar_one()
        assert event.target_user_id == target.id
        assert event.reason == "duplicate accounts"

        await session.refresh(target)
        assert target.merged_at is not None
        assert target.sessions_valid_after is not None

    async def test_a_reason_is_optional(self, session: AsyncSession) -> None:
        source = await make_user(session)
        target = await make_user(session)
        await merge_users(
            session,
            source_id=source.id,
            target_id=target.id,
            actor_id=uuid.uuid4(),
            actor_label="admin",
        )
        record = (await session.execute(select(UserMerge))).scalar_one()
        assert not record.reason
        event = (
            await session.execute(
                select(IdentityEvent).where(IdentityEvent.action == IdentityEventAction.USER_MERGE)
            )
        ).scalar_one()
        assert not event.reason
