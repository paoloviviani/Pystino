"""Who may use which model.

One place, because the rule is now a union and it is asked in three: ``GET
/v1/models``, ``GET /v1/models/{name}``, and the chat path. The same predicate
written out three times is the same predicate until someone edits two of them.

The rule ([0027](../../../docs/adr/0027-inference-providers.md)):

    a caller may use a model if **their billing group has been granted it, or
    they have been granted it personally**

Absence of any grant means no access. There is no global allow-all, and there
are no denials — an explicit deny overriding a group grant would turn "why can
this person not use that model" into a question requiring a search.

A model whose *provider* is inactive is also excluded. Deactivating a provider
is how an operator takes an endpoint out of service, and leaving its models
listed would advertise something every request then fails on.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import ColumnElement, Select, or_, select
from sqlalchemy.orm import selectinload

from gateway.models import GroupModelAccess, ModelDef, Provider, UserModelAccess


def accessible_models(
    *, user_id: uuid.UUID | None, group_ids: Iterable[uuid.UUID]
) -> Select[tuple[ModelDef]]:
    """Every active model this caller may use, as a statement to refine further.

    Returned rather than executed so callers can add their own filters and
    eager-loads without a second round trip.
    """
    groups = list(group_ids)

    # Outer joins plus an OR, rather than a UNION of two queries: a model granted
    # both ways must appear once, and `distinct` on one statement is simpler to
    # be sure of than deduplicating two result sets.
    reachable: list[ColumnElement[bool]] = []
    if groups:
        reachable.append(GroupModelAccess.group_id.in_(groups))
    if user_id is not None:
        reachable.append(UserModelAccess.user_id == user_id)

    statement = (
        select(ModelDef)
        .join(Provider, Provider.id == ModelDef.provider_id)
        .outerjoin(GroupModelAccess, GroupModelAccess.model_id == ModelDef.id)
        .outerjoin(UserModelAccess, UserModelAccess.model_id == ModelDef.id)
        .where(ModelDef.is_active.is_(True), Provider.is_active.is_(True))
        .distinct()
    )

    if not reachable:
        # No groups and no personal grants. `where(false)` rather than an early
        # return of an empty list, so the caller still gets a statement and this
        # function has one shape.
        return statement.where(ModelDef.id.is_(None))

    return statement.where(or_(*reachable))


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
        .options(selectinload(ModelDef.prices), selectinload(ModelDef.provider))
    )
