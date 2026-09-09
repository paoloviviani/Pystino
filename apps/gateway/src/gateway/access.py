"""Who may use which model.

One place, because the rule is now a union and it is asked in three: ``GET
/v1/models``, ``GET /v1/models/{name}``, and the chat path. The same predicate
written out three times is the same predicate until someone edits two of them.

The rule (ADR 0027, extended by ADR 0045):

    a caller may use a model if **their billing group has been granted it, or
    they have been granted it personally, or the model is public**

Absence of any grant means no access. There are no denials — an explicit deny
overriding a group grant would turn "why can this person not use that model"
into a question requiring a search.

A model whose *provider* is inactive is also excluded. Deactivating a provider
is how an operator takes an endpoint out of service, and leaving its models
listed would advertise something every request then fails on.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import ColumnElement, Select, literal, or_, select
from sqlalchemy.orm import joinedload, selectinload

from gateway.models import GroupModelAccess, ModelDef, Provider, UserModelAccess


def accessible_models(
    *, user_id: uuid.UUID | None, group_ids: Iterable[uuid.UUID]
) -> Select[tuple[ModelDef]]:
    """Every active model this caller may use, as a statement to refine further.

    Returned rather than executed so callers can add their own filters and
    eager-loads without a second round trip.
    """
    groups = list(group_ids)

    # EXISTS per grant kind, rather than outer joins plus DISTINCT.
    #
    # The joins were the obvious spelling and they were wrong twice over. A
    # model granted through two of the caller's groups fans out into two rows,
    # so the joins force a DISTINCT to put it back — and `DISTINCT` requires an
    # equality operator for every selected column. `ModelDef.provider` is
    # `lazy="joined"`, which drags the provider's `extra_headers` into the
    # select list, and PostgreSQL has no equality operator for `json`. The
    # result was a 500 on `/v1/models` that SQLite could not reproduce, because
    # SQLite is happy to compare JSON.
    #
    # EXISTS cannot fan out, so there is nothing to deduplicate and no DISTINCT
    # to go wrong. It is also the cheaper plan: no join-then-dedup over the
    # grant tables.
    reachable: list[ColumnElement[bool]] = []
    if groups:
        reachable.append(
            select(literal(1))
            .where(
                GroupModelAccess.model_id == ModelDef.id,
                GroupModelAccess.group_id.in_(groups),
            )
            .exists()
        )
    if user_id is not None:
        reachable.append(
            select(literal(1))
            .where(
                UserModelAccess.model_id == ModelDef.id,
                UserModelAccess.user_id == user_id,
            )
            .exists()
        )

    statement = (
        select(ModelDef)
        .join(Provider, Provider.id == ModelDef.provider_id)
        .where(ModelDef.is_active.is_(True), Provider.is_active.is_(True))
    )

    if not reachable:
        # No groups and no personal grants — but a public model needs no grant
        # at all, so the empty case still has to admit one. `where(false)`
        # rather than an early return of an empty list, so the caller still
        # gets a statement and this function has one shape.
        return statement.where(
            or_(ModelDef.is_public.is_(True), ModelDef.id.is_(None))
        )

    # The public flag is a column predicate alongside the grant EXISTS clauses,
    # not a third EXISTS: it reads off the row already being fetched, and
    # "public" is a property of the model rather than a relationship to it.
    return statement.where(or_(*reachable, ModelDef.is_public.is_(True)))


def accessible_model_by_name(
    name: str, *, user_id: uuid.UUID | None, group_ids: Iterable[uuid.UUID]
) -> Select[tuple[ModelDef]]:
    """One named model, if this caller may use it.

    Eager-loads prices and the provider: the chat path needs both immediately,
    and touching an unloaded relationship from async code raises MissingGreenlet
    rather than quietly emitting a query.
    """
    return (
        accessible_models(user_id=user_id, group_ids=group_ids)
        .where(ModelDef.name == name)
        # The provider is many-to-one, so it joins without multiplying rows;
        # prices are to-many and stay a separate IN query. One round trip saved
        # on every metered request.
        .options(selectinload(ModelDef.prices), joinedload(ModelDef.provider))
    )
