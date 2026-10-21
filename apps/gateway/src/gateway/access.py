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

**An alias is in or out on its own account** (ADR 0101). It has no provider of
its own, so the provider rule above cannot apply to it; what an alias inherits
is its ``is_active`` flag and its grants, and what it does *not* inherit is its
target's access. The caller must be able to use a model behind ``fast`` without
being able to use that model directly, or the alias buys nothing. The one
exception is the target's usability, which is a **request-time** refusal
(``503 alias_unavailable``) rather than a visibility rule: see :func:`alias_targets`.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Iterable

from sqlalchemy import ColumnElement, Select, and_, literal, or_, select
from sqlalchemy.orm import joinedload, selectinload

from gateway.models import (
    GroupModelAccess,
    ModelDef,
    ModelRouteTarget,
    Provider,
    UserModelAccess,
)


class AliasUnavailable(enum.StrEnum):
    """Why an alias cannot serve a request (ADR 0101, decision 5).

    The values are the console's and the admin API's wire form, so they are
    stable identifiers rather than a message. ``TARGET_DELETED`` covers a target
    that was deleted *and* one that was never there — the cascade removes the
    route row either way, and the two are the same state to an operator, who
    cannot act differently on either.
    """

    TARGET_DELETED = "target_deleted"
    TARGET_INACTIVE = "target_inactive"
    TARGET_PROVIDER_INACTIVE = "target_provider_inactive"
    #: Not one of ADR 0101's three, because it is not reachable through the
    #: console: the save path refuses to write it. It exists so that a chain
    #: forced into the table directly is classified like the other three rather
    #: than falling through as "available".
    TARGET_IS_ALIAS = "target_is_alias"


def all_active_models() -> Select[tuple[ModelDef]]:
    """Every active model behind an active provider, with no grant check at all.

    The unauthenticated half of ``GET /v1/models`` (ADR 0081): a public listing
    has no caller to check a grant against, and grant-filtering an absent caller
    down to "public models only" would be a narrower endpoint than the one being
    exempted from authentication, not the same one made anonymous. Factored out
    of :func:`accessible_models` rather than duplicated, per this module's own
    rule about one predicate written in one place.
    """
    # An **outer** join, and the alias arm beside the served arm — the two go
    # together, because either alone drops every alias (ADR 0101).
    #
    # The inner join this replaced eliminated alias rows from the result
    # outright: `providers.id = models.provider_id` is NULL for an alias, NULL
    # is not true, and the row never arrives. A caller granted an alias would
    # get a 404 for a name their console says they can use, and `/v1/models`
    # would simply not list it. Nothing raises — the predicate is the one place
    # that decides who may use what, so a join that quietly narrows it is the
    # worst kind of bug to have here.
    #
    # With the join made outer, `providers.is_active` still has to be waived for
    # an alias: it reads NULL there, and `NULL AND NOT is_alias` is not true.
    # An alias's provider is its *target's* provider, and that is checked where
    # the target is dereferenced — refusing the request, not hiding the name,
    # because an administrator has to be able to see an alias whose target is
    # down in order to retarget it.
    return (
        select(ModelDef)
        .outerjoin(Provider, Provider.id == ModelDef.provider_id)
        .where(
            ModelDef.is_active.is_(True),
            or_(
                ModelDef.is_alias.is_(True),
                and_(ModelDef.is_alias.is_(False), Provider.is_active.is_(True)),
            ),
        )
    )


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

    statement = all_active_models()

    if not reachable:
        # No groups and no personal grants — but a public model needs no grant
        # at all, so the empty case still has to admit one. `where(false)`
        # rather than an early return of an empty list, so the caller still
        # gets a statement and this function has one shape.
        return statement.where(or_(ModelDef.is_public.is_(True), ModelDef.id.is_(None)))

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

    Deliberately does **not** load an alias's route targets. `selectinload` on
    the collection issues its round trip whether or not any row needs it, so
    loading here would cost every direct request a query it does not use — and
    `test_query_counts.py` pins that number. An alias pays for its own
    dereference through :func:`alias_targets`, once, and only when the name it
    sent turned out to be one.
    """
    return (
        accessible_models(user_id=user_id, group_ids=group_ids)
        .where(ModelDef.name == name)
        # The provider is many-to-one, so it joins without multiplying rows;
        # prices are to-many and stay a separate IN query. One round trip saved
        # on every metered request.
        .options(selectinload(ModelDef.prices), joinedload(ModelDef.provider))
    )


def alias_unavailable_reason(alias: ModelDef, target: ModelDef | None) -> AliasUnavailable | None:
    """Why this alias cannot serve a request right now, or ``None`` if it can.

    **The one place this is decided** (ADR 0101, decision 5). Three surfaces ask
    it and must agree, because disagreeing is how an operator ends up chasing a
    console that says "Available" while every request 503s: the request path
    turns the answer into the ``503``, ``/v1/models`` drops the card, and the
    admin list shows the reason.

    Written as one function over an *already loaded* target rather than a query,
    so a caller can batch-load targets for a whole page of aliases and classify
    them without a round trip each.
    """
    if target is None:
        return AliasUnavailable.TARGET_DELETED
    if target.is_alias:
        return AliasUnavailable.TARGET_IS_ALIAS
    if not target.is_active:
        return AliasUnavailable.TARGET_INACTIVE
    if target.provider is None or not target.provider.is_active:
        return AliasUnavailable.TARGET_PROVIDER_INACTIVE
    return None


def alias_targets(alias: ModelDef) -> Select[tuple[ModelDef, int, int]]:
    """Every model that may serve ``alias``, with its position and weight.

    No access filter at all: the caller's access was decided against the name
    they sent, and an alias *is* the grant (ADR 0101), so requiring access to the
    targets as well would remove the only thing an alias is for. None on
    `is_active` or the provider either (the planner classifies each row
    with :func:`alias_unavailable_reason`, so an unavailable target is skipped
    for a reason rather than missing), and **one statement** for the whole
    route, carrying each target's provider and prices — `test_query_counts.py`
    pins the alias path at a single extra round trip however many targets there
    are. The joined prices are a to-many, so the caller reads the result through
    ``.unique()``.
    """
    return (
        select(ModelDef, ModelRouteTarget.position, ModelRouteTarget.weight)
        .join(ModelRouteTarget, ModelRouteTarget.target_model_id == ModelDef.id)
        .where(ModelRouteTarget.alias_id == alias.id)
        .order_by(ModelRouteTarget.position)
        .options(joinedload(ModelDef.prices), joinedload(ModelDef.provider))
    )
