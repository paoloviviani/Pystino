"""The sharing ACL.

Access control gets tests for the same reason accounting does: a wrong answer
here is not a stack trace, it is one person reading another's documents. The
assertions that matter most are the *negative* ones, and the single most
valuable test in this file is
`test_a_manually_granted_membership_still_reaches_a_group_share` — it is the one
that fails for an implementation reading groups out of a token, which is the
trap ADR 0057 records and which has already been got wrong once in this project.
"""

from __future__ import annotations

import uuid

import pytest
from gateway.models import (
    Group,
    KnowledgeBase,
    Membership,
    MembershipSource,
    ResourceKind,
    ResourceShare,
    SharePrincipal,
    ShareRole,
    User,
)
from gateway.sharing import (
    delete_shares,
    effective_group_ids,
    grant,
    list_shares,
    may_reach,
    reachable,
    revoke,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

KIND = ResourceKind.KNOWLEDGE_BASE


async def _user(db: AsyncSession, subject: str, *, issuer: str = "https://idp.test") -> User:
    user = User(
        issuer=issuer,
        subject=subject,
        email=f"{subject}@example.org",
        display_name=subject,
    )
    db.add(user)
    await db.flush()
    return user


async def _group(db: AsyncSession, name: str) -> Group:
    group = Group(name=name)
    db.add(group)
    await db.flush()
    return group


async def _base(db: AsyncSession, owner: User, name: str = "base") -> KnowledgeBase:
    base = KnowledgeBase(name=name, owner_user_id=owner.id)
    db.add(base)
    await db.flush()
    return base


async def _reachable_ids(
    db: AsyncSession, user: User, *, role: ShareRole = ShareRole.VIEWER
) -> set[uuid.UUID]:
    """Every base this user can reach, through the composable predicate."""
    groups = await effective_group_ids(db, user.id)
    rows = await db.execute(
        select(KnowledgeBase.id).where(
            reachable(
                kind=KIND,
                owner_column=KnowledgeBase.owner_user_id,
                resource_id_column=KnowledgeBase.id,
                user_id=user.id,
                group_ids=groups,
                role=role,
            )
        )
    )
    return set(rows.scalars())


@pytest.mark.asyncio
async def test_the_owner_reaches_their_own_resource_with_no_share_at_all(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The property that makes revoking every grant safe: ownership is on the
    # resource, not in the grant table.
    async with session_factory() as db:
        owner = await _user(db, "owner")
        base = await _base(db, owner)
        assert await _reachable_ids(db, owner) == {base.id}
        assert await may_reach(
            db, kind=KIND, resource_id=base.id, owner_user_id=owner.id, user_id=owner.id
        )


@pytest.mark.asyncio
async def test_a_stranger_reaches_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        owner = await _user(db, "owner")
        stranger = await _user(db, "stranger")
        base = await _base(db, owner)
        assert await _reachable_ids(db, stranger) == set()
        assert not await may_reach(
            db, kind=KIND, resource_id=base.id, owner_user_id=owner.id, user_id=stranger.id
        )


@pytest.mark.asyncio
async def test_a_personal_share_is_reachable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        owner = await _user(db, "owner")
        other = await _user(db, "other")
        base = await _base(db, owner)
        await grant(
            db,
            kind=KIND,
            resource_id=base.id,
            principal_kind=SharePrincipal.USER,
            principal_id=other.id,
            role=ShareRole.VIEWER,
            granted_by=owner.id,
        )
        await db.flush()
        assert await _reachable_ids(db, other) == {base.id}


@pytest.mark.asyncio
async def test_a_group_share_reaches_members_and_not_others(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        owner = await _user(db, "owner")
        member = await _user(db, "member")
        outsider = await _user(db, "outsider")
        group = await _group(db, "research")
        db.add(Membership(user_id=member.id, group_id=group.id))
        base = await _base(db, owner)
        await grant(
            db,
            kind=KIND,
            resource_id=base.id,
            principal_kind=SharePrincipal.GROUP,
            principal_id=group.id,
            role=ShareRole.VIEWER,
            granted_by=owner.id,
        )
        await db.flush()

        assert await _reachable_ids(db, member) == {base.id}
        assert await _reachable_ids(db, outsider) == set()


@pytest.mark.asyncio
async def test_a_manually_granted_membership_still_reaches_a_group_share(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The ADR 0057 trap, as a test that fails for a token-reading answer.

    A membership an administrator granted by hand has ``source = manual`` and
    appears in **no** directory token. An implementation that resolved groups
    from the access token's ``groups`` claim would pass every other test in this
    file and fail this one, which is precisely why it exists.
    """
    async with session_factory() as db:
        owner = await _user(db, "owner")
        hand_added = await _user(db, "hand-added")
        group = await _group(db, "reviewers")
        db.add(
            Membership(
                user_id=hand_added.id,
                group_id=group.id,
                source=MembershipSource.MANUAL,
            )
        )
        base = await _base(db, owner)
        await grant(
            db,
            kind=KIND,
            resource_id=base.id,
            principal_kind=SharePrincipal.GROUP,
            principal_id=group.id,
            role=ShareRole.VIEWER,
            granted_by=owner.id,
        )
        await db.flush()

        assert group.id in await effective_group_ids(db, hand_added.id)
        assert await _reachable_ids(db, hand_added) == {base.id}
        assert await may_reach(
            db,
            kind=KIND,
            resource_id=base.id,
            owner_user_id=owner.id,
            user_id=hand_added.id,
        )


@pytest.mark.asyncio
async def test_someone_elses_personal_share_does_not_admit_a_third_party(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The test that catches an `or_` where an `and_` belongs.

    A principal is a *pair*: the kind says which id space the id is in. Joining
    the two halves with OR instead of AND makes "shared with any user at all"
    match, which is a resource being readable by everybody the moment it is
    shared with anybody.

    This needs a very specific arrangement to show up, and its absence is why
    the first version of this file passed against the bug: the caller must not
    be the owner (which short-circuits), the share must exist but belong to
    *someone else*, and the caller must be in no group that holds a grant. Every
    other negative test here fails closed for an unrelated reason — no share at
    all, the wrong resource kind, the wrong role — and so proves nothing about
    this.
    """
    async with session_factory() as db:
        owner = await _user(db, "owner")
        invited = await _user(db, "invited")
        bystander = await _user(db, "bystander")
        base = await _base(db, owner)
        await grant(
            db,
            kind=KIND,
            resource_id=base.id,
            principal_kind=SharePrincipal.USER,
            principal_id=invited.id,
            role=ShareRole.EDITOR,
            granted_by=owner.id,
        )
        await db.flush()

        assert await may_reach(
            db, kind=KIND, resource_id=base.id, owner_user_id=owner.id, user_id=invited.id
        )
        assert not await may_reach(
            db,
            kind=KIND,
            resource_id=base.id,
            owner_user_id=owner.id,
            user_id=bystander.id,
        )
        assert await _reachable_ids(db, bystander) == set()


@pytest.mark.asyncio
async def test_a_group_share_does_not_admit_a_user_whose_id_is_not_a_group_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The mirror of the test above, on the group branch: a grant to a group must
    # not match a caller merely because the caller is a user.
    async with session_factory() as db:
        owner = await _user(db, "owner")
        outsider = await _user(db, "outsider")
        group = await _group(db, "research")
        base = await _base(db, owner)
        await grant(
            db,
            kind=KIND,
            resource_id=base.id,
            principal_kind=SharePrincipal.GROUP,
            principal_id=group.id,
            role=ShareRole.VIEWER,
            granted_by=owner.id,
        )
        await db.flush()

        assert not await may_reach(
            db,
            kind=KIND,
            resource_id=base.id,
            owner_user_id=owner.id,
            user_id=outsider.id,
        )


@pytest.mark.asyncio
async def test_a_viewer_share_does_not_satisfy_editor(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        owner = await _user(db, "owner")
        other = await _user(db, "other")
        base = await _base(db, owner)
        await grant(
            db,
            kind=KIND,
            resource_id=base.id,
            principal_kind=SharePrincipal.USER,
            principal_id=other.id,
            role=ShareRole.VIEWER,
            granted_by=owner.id,
        )
        await db.flush()

        assert await _reachable_ids(db, other, role=ShareRole.VIEWER) == {base.id}
        assert await _reachable_ids(db, other, role=ShareRole.EDITOR) == set()
        assert not await may_reach(
            db,
            kind=KIND,
            resource_id=base.id,
            owner_user_id=owner.id,
            user_id=other.id,
            role=ShareRole.EDITOR,
        )


@pytest.mark.asyncio
async def test_an_editor_share_satisfies_viewer(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The role check is a set membership, not an ordering — but for these two
    # roles the superset relation has to hold, or an editor could not read.
    async with session_factory() as db:
        owner = await _user(db, "owner")
        other = await _user(db, "other")
        base = await _base(db, owner)
        await grant(
            db,
            kind=KIND,
            resource_id=base.id,
            principal_kind=SharePrincipal.USER,
            principal_id=other.id,
            role=ShareRole.EDITOR,
            granted_by=owner.id,
        )
        await db.flush()

        assert await _reachable_ids(db, other, role=ShareRole.VIEWER) == {base.id}
        assert await _reachable_ids(db, other, role=ShareRole.EDITOR) == {base.id}


@pytest.mark.asyncio
async def test_the_resource_kind_is_part_of_the_grant(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A share of a chat project must not admit anyone to a knowledge base.

    The ids are uuid4 so a collision cannot happen by accident, but the kind is
    in the primary key precisely so that a caller passing the wrong one gets
    nothing rather than something.
    """
    async with session_factory() as db:
        owner = await _user(db, "owner")
        other = await _user(db, "other")
        base = await _base(db, owner)
        await grant(
            db,
            kind=ResourceKind.CHAT_PROJECT,
            resource_id=base.id,
            principal_kind=SharePrincipal.USER,
            principal_id=other.id,
            role=ShareRole.EDITOR,
            granted_by=owner.id,
        )
        await db.flush()

        assert await _reachable_ids(db, other) == set()
        assert not await may_reach(
            db, kind=KIND, resource_id=base.id, owner_user_id=owner.id, user_id=other.id
        )


@pytest.mark.asyncio
async def test_regranting_changes_the_role_and_keeps_its_provenance(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A delete-then-insert would lose granted_by and created_at on what is only
    # a role change.
    async with session_factory() as db:
        owner = await _user(db, "owner")
        other = await _user(db, "other")
        admin = await _user(db, "admin")
        base = await _base(db, owner)
        first = await grant(
            db,
            kind=KIND,
            resource_id=base.id,
            principal_kind=SharePrincipal.USER,
            principal_id=other.id,
            role=ShareRole.VIEWER,
            granted_by=admin.id,
        )
        await db.flush()
        created = first.created_at

        again = await grant(
            db,
            kind=KIND,
            resource_id=base.id,
            principal_kind=SharePrincipal.USER,
            principal_id=other.id,
            role=ShareRole.EDITOR,
            granted_by=None,
        )
        await db.flush()

        assert again.role == ShareRole.EDITOR
        assert again.granted_by == admin.id
        assert again.created_at == created
        rows = await db.execute(
            select(ResourceShare).where(ResourceShare.resource_id == base.id)
        )
        assert len(rows.scalars().all()) == 1


@pytest.mark.asyncio
async def test_revoke_removes_exactly_one_grant(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        owner = await _user(db, "owner")
        one = await _user(db, "one")
        two = await _user(db, "two")
        base = await _base(db, owner)
        for person in (one, two):
            await grant(
                db,
                kind=KIND,
                resource_id=base.id,
                principal_kind=SharePrincipal.USER,
                principal_id=person.id,
                role=ShareRole.VIEWER,
                granted_by=owner.id,
            )
        await db.flush()

        assert await revoke(
            db,
            kind=KIND,
            resource_id=base.id,
            principal_kind=SharePrincipal.USER,
            principal_id=one.id,
        )
        assert await _reachable_ids(db, one) == set()
        assert await _reachable_ids(db, two) == {base.id}

        # Revoking what is not there is False rather than an error: an
        # administrator clicking twice has not made a mistake.
        assert not await revoke(
            db,
            kind=KIND,
            resource_id=base.id,
            principal_kind=SharePrincipal.USER,
            principal_id=one.id,
        )


@pytest.mark.asyncio
async def test_delete_shares_clears_one_resource_and_leaves_the_others(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Nothing in the schema does this — resource_id is not a foreign key — so
    # this function is the only thing standing between a deleted base and a
    # table of dead grants.
    async with session_factory() as db:
        owner = await _user(db, "owner")
        other = await _user(db, "other")
        kept = await _base(db, owner, name="kept")
        gone = await _base(db, owner, name="gone")
        for base in (kept, gone):
            await grant(
                db,
                kind=KIND,
                resource_id=base.id,
                principal_kind=SharePrincipal.USER,
                principal_id=other.id,
                role=ShareRole.VIEWER,
                granted_by=owner.id,
            )
        await db.flush()

        assert await delete_shares(db, kind=KIND, resource_id=gone.id) == 1
        assert await _reachable_ids(db, other) == {kept.id}


@pytest.mark.asyncio
async def test_list_shares_names_users_and_reports_a_dead_grant(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        owner = await _user(db, "owner")
        person = await _user(db, "person")
        group = await _group(db, "team")
        base = await _base(db, owner)
        for kind, principal in (
            (SharePrincipal.USER, person.id),
            (SharePrincipal.GROUP, group.id),
            # A grant to a principal that does not exist: what a deleted user
            # leaves behind, since principal_id cannot be a foreign key.
            (SharePrincipal.USER, uuid.uuid4()),
        ):
            await grant(
                db,
                kind=KIND,
                resource_id=base.id,
                principal_kind=kind,
                principal_id=principal,
                role=ShareRole.VIEWER,
                granted_by=owner.id,
            )
        await db.flush()

        listed = await list_shares(db, kind=KIND, resource_id=base.id)
        assert len(listed) == 3
        by_principal = {share.principal_id: email for share, email in listed}
        assert by_principal[person.id] == "person@example.org"
        # A group share has no email, and neither does a grant whose user is
        # gone — but both are still *listed*, because nothing else would ever
        # tell an administrator the dead row is there.
        assert by_principal[group.id] is None


@pytest.mark.asyncio
async def test_two_group_shares_do_not_duplicate_a_resource(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The reason `reachable` is EXISTS rather than joins.

    A resource shared with two groups the caller belongs to would fan out into
    two rows under a join, and putting them back needs a DISTINCT that
    PostgreSQL cannot always perform. EXISTS cannot fan out, and this asserts
    the count that proves it.
    """
    async with session_factory() as db:
        owner = await _user(db, "owner")
        member = await _user(db, "member")
        first = await _group(db, "alpha")
        second = await _group(db, "beta")
        db.add(Membership(user_id=member.id, group_id=first.id))
        db.add(Membership(user_id=member.id, group_id=second.id))
        base = await _base(db, owner)
        for group in (first, second):
            await grant(
                db,
                kind=KIND,
                resource_id=base.id,
                principal_kind=SharePrincipal.GROUP,
                principal_id=group.id,
                role=ShareRole.VIEWER,
                granted_by=owner.id,
            )
        await db.flush()

        groups = await effective_group_ids(db, member.id)
        rows = await db.execute(
            select(KnowledgeBase.id).where(
                reachable(
                    kind=KIND,
                    owner_column=KnowledgeBase.owner_user_id,
                    resource_id_column=KnowledgeBase.id,
                    user_id=member.id,
                    group_ids=groups,
                )
            )
        )
        assert rows.scalars().all() == [base.id]


@pytest.mark.asyncio
async def test_a_share_to_a_group_the_caller_left_stops_reaching(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Membership is re-read per request rather than cached anywhere, so leaving
    # a group takes effect on the next call and needs no invalidation.
    async with session_factory() as db:
        owner = await _user(db, "owner")
        member = await _user(db, "member")
        group = await _group(db, "research")
        membership = Membership(user_id=member.id, group_id=group.id)
        db.add(membership)
        base = await _base(db, owner)
        await grant(
            db,
            kind=KIND,
            resource_id=base.id,
            principal_kind=SharePrincipal.GROUP,
            principal_id=group.id,
            role=ShareRole.VIEWER,
            granted_by=owner.id,
        )
        await db.flush()
        assert await _reachable_ids(db, member) == {base.id}

        await db.delete(membership)
        await db.flush()
        assert await _reachable_ids(db, member) == set()
