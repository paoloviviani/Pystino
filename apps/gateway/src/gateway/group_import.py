"""Directory group names seen at sign-in, imported by hand, and the default group.

Two answers to the same question, "which group does a person who just signed
in belong to", that a real deployment needed once the directory stopped being
trusted to create groups:

- **Seen groups.** With ``GATEWAY_OIDC__GROUP_IMPORT=manual`` a name the token
  carries and no group here has is recorded (``seen_groups``), not created.
  One GitLab sign-in created 67 groups under the old behaviour, one per GitLab
  group its owner was in. The console lists the names; Import creates the
  group and grants it at once to the people recorded as carrying it.
- **The default group** (``GATEWAY_OIDC__DEFAULT_GROUP``, ``users`` unless
  set): every person is put in it once, so a brand-new account has a group to
  bill from even when nothing their directory says has been imported. Once,
  not every sign-in: an administrator who takes someone out of it means it.

Every write here happens on a sign-in or an administrator's action, never on a
steady-state ``/v1`` request: the bearer path only reaches provisioning when a
token disagrees with the stored row, and ``users.unresolved_group_names`` is
what lets "a name that did not resolve last time" stop counting as
disagreement.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.models import (
    Group,
    GroupSource,
    GroupSync,
    IdentityProvider,
    Membership,
    MembershipSource,
    SeenGroup,
    SeenGroupUser,
    User,
)
from gateway.types import utcnow


def _insert(session: AsyncSession, model: type[Any]) -> Any:
    """An INSERT that can be told to ignore a conflict, on either dialect.

    Two first sign-ins at once both see "no such group yet" and both insert
    it; with a plain INSERT the second one fails, and with it that person's
    sign-in. `ON CONFLICT DO NOTHING` exists on PostgreSQL and SQLite alike,
    under the same name, from each dialect's own `insert`.
    """
    if session.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        return pg_insert(model)
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    return sqlite_insert(model)


# -- the default group ---------------------------------------------------------


async def _group_named(session: AsyncSession, name: str) -> Group:
    """The group called *name*, created (as the console's) if it is missing."""
    await session.execute(
        _insert(session, Group)
        .values(id=uuid.uuid4(), name=name, source=GroupSource.MANUAL)
        .on_conflict_do_nothing(index_elements=["name"])
    )
    return (await session.execute(select(Group).where(Group.name == name))).scalar_one()


async def grant_default_group(session: AsyncSession, user: User, name: str) -> Group | None:
    """Put *user* in the default group, once in their life here.

    Returns the group when this call is the one that granted it, else None.
    The membership is the console's (``MembershipSource.MANUAL``), so no
    directory's sync takes it away; ``users.default_group_granted_at`` makes
    it happen once, so an administrator's removal sticks. An existing
    membership is kept and becomes the console's: the bundled Authelia's
    ``users`` group is this same group by name, and the people already in it
    stay in it, now beyond the reach of any directory's sync.
    """
    if not name or user.default_group_granted_at is not None:
        return None
    group = await _group_named(session, name)
    await session.execute(
        _insert(session, Membership)
        .values(user_id=user.id, group_id=group.id, source=MembershipSource.MANUAL)
        .on_conflict_do_nothing(index_elements=["user_id", "group_id"])
    )
    # A membership that was already there may be a directory's (the old
    # auto-created `users`, or a directory group of that name): made the
    # console's now, once, or the claim sync would take away the group this
    # grant promises the moment the token stopped naming it.
    await session.execute(
        update(Membership)
        .where(
            Membership.user_id == user.id,
            Membership.group_id == group.id,
            Membership.source != MembershipSource.MANUAL,
        )
        .values(source=MembershipSource.MANUAL)
        # Synchronised (the default): the reconciliation that follows reads
        # these rows back through the identity map, and a stale `oidc` there
        # is exactly the membership it would delete.
    )
    user.default_group_granted_at = utcnow()
    await session.flush()
    return group


# -- seen groups ------------------------------------------------------------------


async def record_unresolved(
    session: AsyncSession,
    user: User,
    *,
    issuer: str,
    unresolved: list[str],
    missing: list[str],
    touch_login: bool,
) -> None:
    """Remember what this provisioning could not resolve, writing only changes.

    *unresolved* is every claimed name that did not become a membership
    (missing, or a deactivated group); it goes on the user row, for the hot
    path's divergence check. *missing* is the subset no group carries at all;
    those are the seen names, with this person counted against each. A sign-in
    (*touch_login*) also moves their ``last_seen_at``, in one statement.
    """
    wanted = sorted(set(unresolved))
    changed = user.unresolved_group_names is None or sorted(user.unresolved_group_names) != wanted
    if changed:
        user.unresolved_group_names = wanted

    missing_set = set(missing)
    if changed:
        held = dict(
            (
                await session.execute(
                    select(SeenGroup.name, SeenGroup.id)
                    .join(SeenGroupUser, SeenGroupUser.seen_group_id == SeenGroup.id)
                    .where(SeenGroupUser.user_id == user.id, SeenGroup.issuer == issuer)
                )
            )
            .tuples()
            .all()
        )
        gone = [seen_id for name, seen_id in held.items() if name not in missing_set]
        if gone:
            await session.execute(
                delete(SeenGroupUser).where(
                    SeenGroupUser.user_id == user.id, SeenGroupUser.seen_group_id.in_(gone)
                )
            )
        new = sorted(missing_set - set(held))
        if new:
            now = utcnow()
            await session.execute(
                _insert(session, SeenGroup)
                .values(
                    [
                        {
                            "id": uuid.uuid4(),
                            "issuer": issuer,
                            "name": name,
                            "first_seen_at": now,
                            "last_seen_at": now,
                        }
                        for name in new
                    ]
                )
                .on_conflict_do_nothing(index_elements=["issuer", "name"])
            )
            ids = (
                (
                    await session.execute(
                        select(SeenGroup.id).where(
                            SeenGroup.issuer == issuer, SeenGroup.name.in_(new)
                        )
                    )
                )
                .scalars()
                .all()
            )
            await session.execute(
                _insert(session, SeenGroupUser)
                .values([{"seen_group_id": seen_id, "user_id": user.id} for seen_id in ids])
                .on_conflict_do_nothing(index_elements=["seen_group_id", "user_id"])
            )
    if touch_login and missing_set:
        await session.execute(
            update(SeenGroup)
            .where(SeenGroup.issuer == issuer, SeenGroup.name.in_(sorted(missing_set)))
            .values(last_seen_at=utcnow())
            .execution_options(synchronize_session=False)
        )
    await session.flush()


def _issuer_spellings(issuer: str) -> set[str]:
    bare = issuer.rstrip("/")
    return {issuer, bare, bare + "/"}


async def provider_for_issuer(session: AsyncSession, issuer: str) -> IdentityProvider | None:
    return (
        (
            await session.execute(
                select(IdentityProvider)
                .where(IdentityProvider.issuer.in_(_issuer_spellings(issuer)))
                .order_by(IdentityProvider.name)
            )
        )
        .scalars()
        .first()
    )


@dataclass(frozen=True)
class ImportResult:
    group: Group
    #: People the group was granted to now, from what their last sign-in said.
    members_added: int
    #: "now" when the provider's answer applies at every login, so the
    #: recorded claims could be applied at once; "next_login" when it does
    #: not (first_login, never, or a provider this gateway has no row for).
    applied: str


async def import_seen_group(session: AsyncSession, seen: SeenGroup) -> ImportResult:
    """Create the group a seen name asks for, and grant it where that is sound.

    The grant is immediate only for a provider whose answer is the token's
    and applies at every login: that is the provider whose next sign-in
    would grant the same thing anyway, so doing it now changes the timing and
    nothing else. Under ``first_login`` the claim of a past login was never
    going to be applied again, and inventing that now would be a grant the
    operator did not configure.
    """
    group = Group(name=seen.name, source=GroupSource.OIDC)
    session.add(group)
    await session.flush()

    provider = await provider_for_issuer(session, seen.issuer)
    immediate = (
        provider is not None
        and provider.group_sync is GroupSync.EVERY_LOGIN
        and (provider.group_source or "claim") == "claim"
    )
    added = 0
    if immediate:
        users = list(
            (
                await session.execute(
                    select(User)
                    .join(SeenGroupUser, SeenGroupUser.user_id == User.id)
                    .where(SeenGroupUser.seen_group_id == seen.id)
                )
            )
            .scalars()
            .all()
        )
        for user in users:
            session.add(
                Membership(user_id=user.id, group_id=group.id, source=MembershipSource.OIDC)
            )
            user.unresolved_group_names = [
                name for name in (user.unresolved_group_names or []) if name != seen.name
            ]
            added += 1
        await session.flush()
        # The same rule provisioning applies: someone whose only group this
        # now is would otherwise have nothing to bill until they chose it.
        for user in users:
            await session.refresh(user, attribute_names=["memberships"])
            if user.default_billing_group_id is None and len(user.memberships) == 1:
                user.default_billing_group_id = group.id

    await session.delete(seen)
    await session.flush()
    return ImportResult(
        group=group, members_added=added, applied="now" if immediate else "next_login"
    )
