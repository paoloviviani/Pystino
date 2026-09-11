"""Who may see a resource its owner shared.

The sibling of `access.py`, and deliberately the same shape, because the rest of
this gateway has exactly one access question and adding a second one that
behaves differently would mean two mental models for one word.

The rule (ADR 0062):

    a caller may reach a resource if **they own it, or it is shared with them
    personally, or it is shared with one of their groups**

Three properties inherited from `access.py`, each of which was argued there and
is not re-argued here:

* access is the **union** of every applicable grant;
* **absence means no access** — there is no allow-all;
* **there are no denial rows**, because an explicit deny overriding a group
  grant turns "why can this person not see that" into a search rather than a
  look.

Two properties that are new, and both are consequences of the owner living on
the resource rather than in the grant table:

* **the owner is not a share.** A resource with no grants at all is still
  reachable by exactly one person, so revoking every grant can never orphan it,
  and "who can delete this" has one answer at all times;
* **membership is re-read on every request**, never taken from a token. That is
  the trap ADR 0057 records: a group granted by an administrator appears in no
  directory token, so a share to that group would be invisible to a
  claim-reading implementation. `effective_group_ids` is the only supported way
  to answer "which groups is this caller in".

Two shapes here are load-bearing and easy to get wrong. ``EXISTS`` per grant
kind rather than joins plus ``DISTINCT``, for the reason written out at length
in `access.py`: a resource shared through two of the caller's groups fans out
into two rows, and putting them back with ``DISTINCT`` needs an equality
operator for every selected column — which PostgreSQL does not have for
``json``. And ``resource_shares`` has no foreign keys (its addresses are
polymorphic), so this module is the single place that deletes a resource's
grants and the single place that tolerates a grant pointing at something gone.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import (
    ColumnElement,
    SQLColumnExpression,
    and_,
    delete,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.errors import BadRequestError
from gateway.models import (
    Group,
    Membership,
    ResourceKind,
    ResourceShare,
    SharePrincipal,
    ShareRole,
    User,
)

#: Which roles satisfy a required role. Not an ordering on the enum, because
#: "editor >= viewer" is true of these two and would silently become the wrong
#: answer the moment a third role that is not a superset of the others is added.
_SATISFIES: dict[ShareRole, frozenset[ShareRole]] = {
    ShareRole.VIEWER: frozenset({ShareRole.VIEWER, ShareRole.EDITOR}),
    ShareRole.EDITOR: frozenset({ShareRole.EDITOR}),
}


async def effective_group_ids(session: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    """Every group this user is actually in, whatever granted the membership.

    **Not the token's ``groups`` claim.** A membership an administrator granted
    by hand has ``source = manual`` and appears in no directory token
    (ADR 0057), so a share made to such a group would be unreachable by a
    caller who is genuinely in it. Reading the table is the only correct answer,
    and it is one indexed query.
    """
    rows = await session.execute(select(Membership.group_id).where(Membership.user_id == user_id))
    return list(rows.scalars())


def _grant_exists(
    *,
    kind: ResourceKind,
    user_id: uuid.UUID | None,
    group_ids: Sequence[uuid.UUID],
    roles: frozenset[ShareRole],
    resource_id_column: SQLColumnExpression[uuid.UUID],
) -> list[ColumnElement[bool]]:
    """EXISTS clauses for "this resource is shared with this caller".

    One per principal kind. See the module docstring for why these are not
    joins.
    """
    clauses: list[ColumnElement[bool]] = []
    role_values = list(roles)
    if user_id is not None:
        clauses.append(
            select(literal(1))
            .where(
                ResourceShare.resource_kind == kind,
                ResourceShare.resource_id == resource_id_column,
                ResourceShare.principal_kind == SharePrincipal.USER,
                ResourceShare.principal_id == user_id,
                ResourceShare.role.in_(role_values),
            )
            .exists()
        )
    if group_ids:
        clauses.append(
            select(literal(1))
            .where(
                ResourceShare.resource_kind == kind,
                ResourceShare.resource_id == resource_id_column,
                ResourceShare.principal_kind == SharePrincipal.GROUP,
                ResourceShare.principal_id.in_(list(group_ids)),
                ResourceShare.role.in_(role_values),
            )
            .exists()
        )
    # Published to the whole deployment (ADR 0066). **Unconditional**, and that
    # is its trap: it is the only clause here gated on nothing about the
    # caller, so it is the easiest to forget and the only one whose absence is
    # silent — a published resource would simply stay invisible, with no error
    # and nobody obviously wronged.
    #
    # `resource_id` and `role` are in the predicate for the same reason they
    # are in the two above. Dropping `resource_id` makes one publication
    # publish everything of that kind; dropping `role` makes a viewer grant
    # confer editing. Both are tested by reintroducing exactly those faults.
    clauses.append(
        select(literal(1))
        .where(
            ResourceShare.resource_kind == kind,
            ResourceShare.resource_id == resource_id_column,
            ResourceShare.principal_kind == SharePrincipal.EVERYONE,
            ResourceShare.role.in_(role_values),
        )
        .exists()
    )
    return clauses


def reachable(
    *,
    kind: ResourceKind,
    # Nullable since ADR 0066: null is a resource the deployment owns. Files
    # keep a NOT NULL owner and pass one of those, which this still accepts.
    owner_column: SQLColumnExpression[uuid.UUID | None],
    resource_id_column: SQLColumnExpression[uuid.UUID],
    user_id: uuid.UUID | None,
    group_ids: Iterable[uuid.UUID],
    role: ShareRole = ShareRole.VIEWER,
    is_admin: bool = False,
) -> ColumnElement[bool]:
    """The predicate "this caller may reach this resource, in this role".

    Returned as a predicate rather than a statement, unlike `access.py`'s
    helpers, because it has to be composable into listings over three different
    tables. Callers pass their own table's owner and id columns.

    A caller with no user id — which cannot currently happen, since every
    principal resolves to a user — reaches only what is shared with their
    groups. That is written out rather than special-cased so the function has
    one shape.
    """
    groups = list(group_ids)
    roles = _SATISFIES[role]
    clauses = _grant_exists(
        kind=kind,
        user_id=user_id,
        group_ids=groups,
        roles=roles,
        resource_id_column=resource_id_column,
    )
    if user_id is not None:
        # Ownership is a column comparison on the row already being fetched,
        # not a fourth EXISTS: the owner is a property of the resource rather
        # than a relationship to it. Same reasoning as `is_public` in
        # `access.py`.
        clauses.append(owner_column == user_id)
    if is_admin:
        # The *only* thing administration buys in this predicate: resources the
        # deployment owns (ADR 0066). Not "administrators see everything" — a
        # colleague's private base stays exactly as invisible as it was before
        # this ADR, and there is no administrator read path to one anywhere in
        # this gateway.
        clauses.append(owner_column.is_(None))
    # No empty-case fallback any more, and its removal is deliberate. It used
    # to be `return owner_column.is_(None)`, meaning "nothing can match" —
    # which was true only because the column was NOT NULL. Making it nullable
    # turned that same expression from *no rows* into *every platform
    # resource*: the exact opposite, silently. The clause list can no longer be
    # empty in any case, because the `everyone` EXISTS above is unconditional.
    return or_(*clauses)


async def may_reach(
    session: AsyncSession,
    *,
    kind: ResourceKind,
    resource_id: uuid.UUID,
    owner_user_id: uuid.UUID | None,
    user_id: uuid.UUID,
    role: ShareRole = ShareRole.VIEWER,
    is_admin: bool = False,
) -> bool:
    """The same question for one resource already in hand.

    Used where the row has been fetched for another reason and re-querying it
    through `reachable` would be a second round trip. Short-circuits on
    ownership, which is the common case and needs no query at all.
    """
    if owner_user_id is not None and owner_user_id == user_id:
        return True
    # A platform resource (ADR 0066), reachable by an administrator and by
    # nobody else unless it has been published. `is not None` rather than a
    # bare falsy test: `owner_user_id` is a UUID or None and nothing else, and
    # a truthiness check here is how the nil UUID would sneak through.
    if owner_user_id is None and is_admin:
        return True
    group_ids = await effective_group_ids(session, user_id)
    # Both halves of a principal have to match together: the kind says which id
    # space `principal_id` is in, so an OR here would match every share made to
    # *any* user. Spelled with and_ rather than the `&` operator because the
    # precedence of `&` against `==` is exactly the trap this is guarding.
    principal_match: ColumnElement[bool] = and_(
        ResourceShare.principal_kind == SharePrincipal.USER,
        ResourceShare.principal_id == user_id,
    )
    if group_ids:
        principal_match = or_(
            principal_match,
            and_(
                ResourceShare.principal_kind == SharePrincipal.GROUP,
                ResourceShare.principal_id.in_(group_ids),
            ),
        )
    # Published to everybody. Unconditional, like its counterpart in
    # `_grant_exists`, and it has to be here too: these two functions answer
    # the same question by different routes, and a resource reachable through
    # the listing but not through this one is a 404 on a row the caller can
    # plainly see.
    #
    # No `principal_id` comparison, because for this kind the id is a constant
    # placeholder rather than an address. Comparing it would work and would
    # invite somebody to "fix" the id later.
    principal_match = or_(
        principal_match,
        ResourceShare.principal_kind == SharePrincipal.EVERYONE,
    )
    found = await session.execute(
        select(literal(1))
        .where(
            ResourceShare.resource_kind == kind,
            ResourceShare.resource_id == resource_id,
            ResourceShare.role.in_(list(_SATISFIES[role])),
            principal_match,
        )
        .limit(1)
    )
    return found.scalar() is not None


async def grant(
    session: AsyncSession,
    *,
    kind: ResourceKind,
    resource_id: uuid.UUID,
    principal_kind: SharePrincipal,
    principal_id: uuid.UUID,
    role: ShareRole,
    granted_by: uuid.UUID | None,
) -> ResourceShare:
    """Share a resource, or change the role of an existing share.

    An upsert rather than an insert, because the composite primary key forbids
    one principal holding two roles and "share it again as editor" is what an
    administrator means by re-granting. Doing it as a delete-then-insert would
    lose ``granted_by`` and ``created_at`` on a mere role change, so the row is
    updated in place and its provenance kept.
    """
    existing = await session.get(
        ResourceShare,
        {
            "resource_kind": kind,
            "resource_id": resource_id,
            "principal_kind": principal_kind,
            "principal_id": principal_id,
        },
    )
    if existing is not None:
        existing.role = role
        return existing
    share = ResourceShare(
        resource_kind=kind,
        resource_id=resource_id,
        principal_kind=principal_kind,
        principal_id=principal_id,
        role=role,
        granted_by=granted_by,
    )
    session.add(share)
    return share


async def revoke(
    session: AsyncSession,
    *,
    kind: ResourceKind,
    resource_id: uuid.UUID,
    principal_kind: SharePrincipal,
    principal_id: uuid.UUID,
) -> bool:
    """Withdraw one grant. True if there was one."""
    result = await session.execute(
        delete(ResourceShare).where(
            ResourceShare.resource_kind == kind,
            ResourceShare.resource_id == resource_id,
            ResourceShare.principal_kind == principal_kind,
            ResourceShare.principal_id == principal_id,
        )
    )
    return bool(getattr(result, "rowcount", 0))


async def delete_shares(
    session: AsyncSession, *, kind: ResourceKind, resource_id: uuid.UUID
) -> int:
    """Forget every grant on a resource that is going away.

    **This must be called whenever a shareable resource is deleted.**
    ``resource_shares.resource_id`` is not a foreign key — it cannot be, being
    polymorphic across three kinds and two databases — so nothing in the schema
    will do this. A resource id is a uuid4, so a leaked grant will never be
    inherited by a later resource; the cost of forgetting is a slow leak of dead
    rows, not a security hole. Calling it anyway is what keeps that true.
    """
    result = await session.execute(
        delete(ResourceShare).where(
            ResourceShare.resource_kind == kind,
            ResourceShare.resource_id == resource_id,
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def list_shares(
    session: AsyncSession, *, kind: ResourceKind, resource_id: uuid.UUID
) -> list[tuple[ResourceShare, str | None]]:
    """Every grant on one resource, with the user's email where it is a user.

    The name beside the grant is what makes a sharing dialog usable, and
    fetching it here rather than per row is the difference between one query and
    one per share. A grant whose principal no longer exists comes back with
    ``None`` rather than being hidden: an administrator looking at a share list
    should see that there is a dead row, because nothing else will ever tell
    them.
    """
    rows = await session.execute(
        select(ResourceShare, User.email)
        .outerjoin(
            User,
            (ResourceShare.principal_kind == SharePrincipal.USER)
            & (ResourceShare.principal_id == User.id),
        )
        .where(
            ResourceShare.resource_kind == kind,
            ResourceShare.resource_id == resource_id,
        )
        .order_by(ResourceShare.created_at)
    )
    return [(share, email) for share, email in rows.all()]


async def resolve_principal(
    session: AsyncSession,
    *,
    kind: SharePrincipal,
    principal_id: uuid.UUID | None,
    principal_email: str | None,
    group_name: str | None,
    caller_id: uuid.UUID,
) -> uuid.UUID:
    """The id to store, from whichever way the caller named the principal.

    An id, an email (for a user), or a name (for a group). The friendly forms
    exist because the *chat* is now a first-class client of this surface, and
    somebody sharing with a colleague knows their address rather than their
    uuid — with no endpoint a bearer token can reach that turns one into the
    other.

    **This does disclose whether an address has an account here**, to a
    signed-in caller, and that is a deliberate trade rather than an oversight.
    The alternative is accepting the share and storing a grant against nothing,
    so the sharer believes they have shared and the colleague never sees it — a
    silent failure in exchange for withholding something a colleague could
    confirm by asking. The disclosure is bounded to authenticated callers and
    answers yes or no about one address at a time.

    A group is resolved **only over the caller's own memberships**, which is
    the rule ADR 0061 established for `x-bill-to`: a lookup across every group
    in the deployment would turn a share dialog into a directory listing. The
    consequence is the same one recorded there — a real group the caller does
    not hold is indistinguishable from one that does not exist, which is the
    point.
    """
    if principal_id is not None:
        return principal_id

    if kind is SharePrincipal.USER:
        if not principal_email:
            raise BadRequestError(
                "Name the person by 'principal_id' or 'principal_email'.",
                code="unknown_principal",
            )
        found = (
            (
                await session.execute(
                    select(User).where(
                        func.lower(User.email) == principal_email.strip().lower(),
                        User.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .first()
        )
        if found is None:
            raise BadRequestError(
                f"No active account here uses {principal_email!r}. They have to sign "
                "in once before they can be shared with.",
                code="unknown_principal",
            )
        return found.id

    if not group_name:
        raise BadRequestError(
            "Name the group by 'principal_id' or 'group_name'.",
            code="unknown_principal",
        )
    mine = (
        (
            await session.execute(
                select(Group)
                .join(Membership, Membership.group_id == Group.id)
                .where(
                    Membership.user_id == caller_id,
                    func.lower(Group.name) == group_name.strip().lower(),
                    Group.is_active.is_(True),
                )
            )
        )
        .scalars()
        .first()
    )
    if mine is None:
        raise BadRequestError(
            f"You are not in a group called {group_name!r}. You can only share with "
            "groups you belong to.",
            code="unknown_principal",
        )
    return mine.id


def administers(
    *,
    owner_user_id: uuid.UUID | None,
    user_id: uuid.UUID,
    is_admin: bool,
) -> bool:
    """May this caller publish, unpublish or delete this resource?

    The single place either router asks, and deliberately **not** the same
    question as `may_reach`. Reaching a resource is about seeing it; this is
    about changing who else can.

    Two rules, and the second is the one worth reading (ADR 0066).

    **You administer what you own.** Unchanged.

    **An administrator administers only what the deployment owns.** Not a
    colleague's base — and the reason is sharper than least privilege. There is
    no administrator read path to a private knowledge base anywhere in this
    gateway, so a publish right over one would let an administrator disclose a
    colleague's documents *without ever being able to see what they were
    disclosing*. That is strictly worse than a read backdoor, and it would have
    been invented by the sharing surface rather than decided anywhere.

    Taking a publication *down* is the deliberate asymmetry and does not go
    through here — see `may_unpublish`.
    """
    if owner_user_id is not None:
        return owner_user_id == user_id
    return is_admin


def may_unpublish(
    *,
    owner_user_id: uuid.UUID | None,
    user_id: uuid.UUID,
    is_admin: bool,
) -> bool:
    """May this caller withdraw an ``everyone`` grant?

    Wider than `administers` by exactly one case, and only for this one act: an
    administrator may unpublish anything. Removing reach is not granting it, an
    operator must be able to pull something from the whole deployment without
    deleting somebody's work to do it, and a published resource is in their
    listing to act on.

    Every *other* grant on a resource they do not own stays out of reach —
    including the personal share sitting beside the publication they just
    withdrew. That is why this is a separate function rather than a flag on the
    one above: a flag would have been read as "administrators may edit shares",
    which is a different and much larger permission.
    """
    return administers(owner_user_id=owner_user_id, user_id=user_id, is_admin=is_admin) or is_admin
