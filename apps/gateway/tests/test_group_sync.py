"""ADR 0057: what a directory's answer about groups may and may not change.

Two separate questions, and the tests are grouped by them.

**Whose grant is it.** A membership a login made is the directory's and stays
revocable. A membership an administrator made is not, and no login touches it —
that is the whole complaint this ADR answers: "I must be able to assign
manually a user to a group, even if the user comes from an IdP and that IdP
does not map to that group."

**How often the directory gets to answer.** ``group_sync`` per provider, in
Keycloak's vocabulary: every login, first login only, or never.

The derived state is tested too, because it was quietly wrong before: with
membership no longer equal to "what the token said", ``is_admin`` and the
default billing group have to be computed from the memberships the person
actually holds. Reading the token there told a manually-added user that the
group they are in was not one of theirs.
"""

from __future__ import annotations

import httpx
import pytest
from conftest import Seeded
from gateway.config import OIDCSettings
from gateway.models import (
    Group,
    GroupSource,
    GroupSync,
    Membership,
    MembershipSource,
    User,
)
from gateway.oidc import _claims_diverge, provision_user, sync_user_from_claims
from gateway.oidc_policy import OIDCPolicy
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin

IDP = "https://idp.test"
SUBJECT = "directory-person"


def policy_with(**kwargs: object) -> OIDCPolicy:
    base: dict[str, object] = {
        "auto_provision": True,
        "unknown_user_policy": "refuse",
        "groups_claim": "groups",
    }
    base.update(kwargs)
    return OIDCPolicy(**base)  # type: ignore[arg-type]


async def sign_in(
    session: AsyncSession,
    *,
    groups: list[str],
    group_sync: GroupSync = GroupSync.EVERY_LOGIN,
    policy: OIDCPolicy | None = None,
    subject: str = SUBJECT,
) -> User:
    user = await provision_user(
        session,
        issuer=IDP,
        subject=subject,
        email="person@example.org",
        display_name="Directory Person",
        group_names=groups,
        settings=OIDCSettings(),
        policy=policy,
        group_sync=group_sync,
    )
    await session.commit()
    return user


async def add_by_hand(
    session: AsyncSession, user: User, name: str, *, source: GroupSource = GroupSource.MANUAL
) -> Group:
    """What the console's "add member" route does: a grant made here."""
    group = (
        await session.execute(select(Group).where(Group.name == name))
    ).scalar_one_or_none()
    if group is None:
        group = Group(name=name, source=source)
        session.add(group)
        await session.flush()
    session.add(
        Membership(user_id=user.id, group_id=group.id, source=MembershipSource.MANUAL)
    )
    await session.commit()
    return group


async def names(session: AsyncSession, user: User) -> set[str]:
    """The groups this person is in, read from the database.

    Not from ``user.memberships``. A membership added by a *sibling* write in
    the same session — which is what an administrator's grant is here — does
    not invalidate an already-loaded collection on the user object, so reading
    the relationship asserted against a stale set and reported a bug that the
    stored rows did not have. Cost me twenty minutes; worth the helper.
    """
    rows = (
        await session.execute(
            select(Group.name)
            .join(Membership, Membership.group_id == Group.id)
            .where(Membership.user_id == user.id)
        )
    ).all()
    return {name for name, in rows}


class TestAnAdministratorsGrantSurvives:
    async def test_a_group_the_directory_never_mentions(self, session: AsyncSession) -> None:
        """The reported bug, in one test."""
        user = await sign_in(session, groups=["research"])
        await add_by_hand(session, user, "finance")

        user = await sign_in(session, groups=["research"])
        assert await names(session, user) == {"research", "finance"}

    async def test_it_survives_many_logins(self, session: AsyncSession) -> None:
        user = await sign_in(session, groups=["research"])
        await add_by_hand(session, user, "finance")
        for _ in range(3):
            user = await sign_in(session, groups=["research"])
        assert "finance" in await names(session, user)

    async def test_even_in_a_group_the_directory_created(self, session: AsyncSession) -> None:
        """Provenance of the *membership*, not of the group.

        The directory created "research" and grants it to other people; this
        person was put in it by hand and the directory does not name it for
        them. Their grant is still not the directory's to withdraw.
        """
        first = await sign_in(session, groups=["research"], subject="someone-else")
        assert await names(session, first) == {"research"}
        research = (
            await session.execute(select(Group).where(Group.name == "research"))
        ).scalar_one()
        assert research.source is GroupSource.OIDC

        user = await sign_in(session, groups=[], subject=SUBJECT)
        session.add(
            Membership(
                user_id=user.id, group_id=research.id, source=MembershipSource.MANUAL
            )
        )
        await session.commit()

        user = await sign_in(session, groups=[], subject=SUBJECT)
        assert await names(session, user) == {"research"}


class TestTheDirectoryKeepsWhatItGranted:
    async def test_a_group_it_stops_naming_is_revoked(self, session: AsyncSession) -> None:
        """The property that must not be lost while fixing the one above.

        Revoking someone in the directory has to revoke their ability to bill
        that group, or the access is only half withdrawn.
        """
        user = await sign_in(session, groups=["research", "finance"])
        assert await names(session, user) == {"research", "finance"}

        user = await sign_in(session, groups=["research"])
        assert await names(session, user) == {"research"}

    async def test_even_in_a_group_an_administrator_created(
        self, session: AsyncSession
    ) -> None:
        """The case that ruled out "whoever created the group owns it".

        An administrator creates "engineering" in the console; the directory
        also names it, so the directory granted this membership. It may
        therefore take it back — under the rejected rule it could add people
        to that group forever and never remove them.
        """
        group = Group(name="engineering", source=GroupSource.MANUAL)
        session.add(group)
        await session.commit()

        user = await sign_in(session, groups=["engineering"])
        assert await names(session, user) == {"engineering"}

        user = await sign_in(session, groups=[])
        assert await names(session, user) == set()


class TestSyncModes:
    async def test_every_login_is_the_default(self, session: AsyncSession) -> None:
        user = await provision_user(
            session,
            issuer=IDP,
            subject=SUBJECT,
            email=None,
            display_name=None,
            group_names=["research"],
            settings=OIDCSettings(),
        )
        await session.commit()
        assert await names(session, user) == {"research"}

    async def test_first_login_seeds_then_stops(self, session: AsyncSession) -> None:
        user = await sign_in(session, groups=["research"], group_sync=GroupSync.FIRST_LOGIN)
        assert await names(session, user) == {"research"}, "seeded from the directory at creation"

        user = await sign_in(
            session, groups=["research", "finance"], group_sync=GroupSync.FIRST_LOGIN
        )
        assert await names(session, user) == {"research"}, "and never consulted again"

        user = await sign_in(session, groups=[], group_sync=GroupSync.FIRST_LOGIN)
        assert await names(session, user) == {"research"}, "including for removals"

    async def test_never_grants_nothing_at_all(self, session: AsyncSession) -> None:
        """Authentication from the directory, authorisation decided here."""
        user = await sign_in(session, groups=["research"], group_sync=GroupSync.NEVER)
        assert await names(session, user) == set()
        assert user.default_billing_group_id is None

    async def test_never_does_not_create_the_claim_s_groups(
        self, session: AsyncSession
    ) -> None:
        """A vocabulary nobody uses should not be left lying in the database."""
        await sign_in(session, groups=["research"], group_sync=GroupSync.NEVER)
        assert (await session.execute(select(Group))).scalars().all() == []

    async def test_never_leaves_an_administrators_grant_alone(
        self, session: AsyncSession
    ) -> None:
        user = await sign_in(session, groups=[], group_sync=GroupSync.NEVER)
        await add_by_hand(session, user, "finance")

        user = await sign_in(session, groups=["research"], group_sync=GroupSync.NEVER)
        assert await names(session, user) == {"finance"}

    async def test_never_leaves_an_earlier_directory_grant_alone(
        self, session: AsyncSession
    ) -> None:
        """Switching the policy does not reach back over what is already granted."""
        user = await sign_in(session, groups=["research"])
        assert await names(session, user) == {"research"}

        user = await sign_in(session, groups=[], group_sync=GroupSync.NEVER)
        assert await names(session, user) == {"research"}


class TestDerivedState:
    """`is_admin` and the default billing group read what the person holds."""

    async def test_no_group_confers_admin(self, session: AsyncSession) -> None:
        """ADR 0069: the flag is a console fact, not a derivation.

        This test used to assert the opposite — that a manual grant into the
        admin group *did* confer admin, as the escape hatch for the lockout
        ADR 0056 warns about. The derivation it relied on is gone: no claim
        names the flag, so no group carries it either, and an administrator
        is made in the console or by `gateway passwd --admin`.
        """
        user = await sign_in(session, groups=["research"])
        assert user.is_admin is False

        await add_by_hand(session, user, "ops")
        user = await sign_in(session, groups=["research", "ops"])
        assert user.is_admin is False

    async def test_login_never_moves_the_admin_flag(self, session: AsyncSession) -> None:
        """The flag set here stays set, whatever the directory says next.

        This used to be "the directory still revokes admin it granted" — true
        only while the flag was derived from membership. With derivation gone
        the durable thing is the flag itself, in *both* directions.
        """
        user = await sign_in(session, groups=["ops"])
        user.is_admin = True
        await session.commit()

        user = await sign_in(session, groups=["research"])
        assert user.is_admin is True

    async def test_a_sole_manual_group_becomes_the_default_billing_group(
        self, session: AsyncSession
    ) -> None:
        """It used to be cleared, and the account could not bill at all.

        The user held exactly one group, an administrator had granted it, and
        the check asked whether the *token* named it.
        """
        user = await sign_in(session, groups=[], group_sync=GroupSync.NEVER)
        group = await add_by_hand(session, user, "finance")

        user = await sign_in(session, groups=[], group_sync=GroupSync.NEVER)
        assert user.default_billing_group_id == group.id

    async def test_a_manual_default_is_not_cleared_by_a_login(
        self, session: AsyncSession
    ) -> None:
        user = await sign_in(session, groups=["research"])
        finance = await add_by_hand(session, user, "finance")
        user.default_billing_group_id = finance.id
        await session.commit()

        user = await sign_in(session, groups=["research"])
        assert user.default_billing_group_id == finance.id


class TestDivergenceOnTheRequestPath:
    """`/v1` asks "would a sync change anything", and must usually answer no.

    A permanent yes means a database write per request — and, before ADR 0057,
    a write that would have stripped the administrator's grant.
    """

    async def test_an_administrators_grant_is_not_a_divergence(
        self, session: AsyncSession
    ) -> None:
        user = await sign_in(session, groups=["research"])
        await add_by_hand(session, user, "finance")
        await session.refresh(user, attribute_names=["memberships"])

        assert _claims_diverge(user, ["research"], OIDCSettings()) is False

    async def test_a_removal_in_the_directory_is_a_divergence(
        self, session: AsyncSession
    ) -> None:
        user = await sign_in(session, groups=["research"])
        assert _claims_diverge(user, [], OIDCSettings()) is True

    async def test_a_new_group_in_the_token_is_a_divergence(
        self, session: AsyncSession
    ) -> None:
        user = await sign_in(session, groups=["research"])
        assert _claims_diverge(user, ["research", "finance"], OIDCSettings()) is True

    @pytest.mark.parametrize("mode", [GroupSync.FIRST_LOGIN, GroupSync.NEVER])
    async def test_groups_are_not_compared_when_the_directory_does_not_set_them(
        self, session: AsyncSession, mode: GroupSync
    ) -> None:
        user = await sign_in(session, groups=["research"], group_sync=mode)
        assert _claims_diverge(user, ["anything", "else"], OIDCSettings(), None, mode) is False

    async def test_the_bearer_path_leaves_an_administrators_grant_alone(
        self, session: AsyncSession
    ) -> None:
        """End to end through the function `/v1` actually calls."""
        user = await sign_in(session, groups=["research"])
        await add_by_hand(session, user, "finance")

        resolved = await sync_user_from_claims(
            session,
            claims={"iss": IDP, "sub": SUBJECT, "groups": ["research"]},
            settings=OIDCSettings(),
        )
        await session.commit()
        assert await names(session, resolved) == {"research", "finance"}


class TestTheApiSurface:
    """Reading the policy through the management API.

    The three tests that used to open this class — creating a provider row
    through the now-removed `POST`/`PUT /admin/identity-providers` to prove
    `group_sync` round-tripped and its bad values were refused — are gone
    along with those routes (ADR 0093 §14, closed in stage (a)'s removals):
    the row is a projection of the environment now, re-seeded at every start,
    so there is nothing left there for an administrator to create or edit.
    """

    async def test_the_members_listing_says_who_granted_each_membership(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The screen where "why is this person still in this group" is asked."""
        async with session_factory() as db:
            directory_person = await sign_in(db, groups=["research"])
            by_hand = await sign_in(db, groups=[], subject="added-by-hand")
            research = (
                await db.execute(select(Group).where(Group.name == "research"))
            ).scalar_one()
            db.add(
                Membership(
                    user_id=by_hand.id,
                    group_id=research.id,
                    source=MembershipSource.MANUAL,
                )
            )
            await db.commit()
            group_id, directory_id, hand_id = research.id, directory_person.id, by_hand.id

        as_user(app, await make_admin(session_factory, seeded))
        response = await client.get(f"/api/admin/groups/{group_id}/members?limit=100")
        assert response.status_code == 200
        rows = {row["id"]: row["membership_source"] for row in response.json()["items"]}
        assert rows[str(directory_id)] == "oidc"
        assert rows[str(hand_id)] == "manual"

    async def test_the_user_listing_does_not_guess_a_membership_source(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A person has many memberships; "which one" has no answer there."""
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.get("/api/admin/users?limit=5")
        assert response.status_code == 200
        assert all(row["membership_source"] is None for row in response.json()["items"])
