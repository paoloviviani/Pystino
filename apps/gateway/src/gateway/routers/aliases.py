"""`/api/admin/aliases`: the admin surface for model aliases (ADR 0101).

A separate module from `routers/admin.py` for one reason: aliases are **not**
models in this surface. They share the `models` table so that a caller can use
one like a model — one name namespace, one access mechanism, one catalogue — and
the whole point of an alias is that an administrator, not a caller, is what
touches it. A model has a provider, an upstream name and a price; an alias has
none of those and one thing instead: a target.

The routes here own the two rules that make the design true, and both are
**save-path** rules rather than database ones:

* **exactly one target.** `model_route_targets` is a list because routing will
  make it one (ADR 0101, "Designed for routing"), and a unique index on
  `(alias_id, position)` cannot say "one" — a second target at position 1
  satisfies it. So every write here deletes and rewrites the alias's route rows,
  which is also what makes a retarget take effect on the next request with
  nothing to invalidate.
* **no chains.** An alias cannot target an alias. Refused here so the
  administrator gets a message naming both names, and again at resolution as a
  backstop for a row that arrived some other way.

Every create, retarget and delete writes an `identity_events` row. Model CRUD
does not — this is the ADR asking for it explicitly, and a retarget is the one
model-affecting change whose consequences are invisible by design: every client
keeps sending the same name and a different model answers, which is precisely
the change nobody could otherwise reconstruct.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, status
from sqlalchemy import Row, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from gateway.access import AliasUnavailable, alias_unavailable_reason
from gateway.admin_audit import alias_view, record_audit
from gateway.deps import AdminUserDep, SessionDep
from gateway.errors import BadRequestError
from gateway.identity_events import record_event
from gateway.models import (
    Group,
    GroupModelAccess,
    IdentityEventAction,
    IdentityEventActor,
    ModelDef,
    ModelKind,
    ModelRouteTarget,
    RouteSession,
    User,
    UserModelAccess,
)
from gateway.routers.admin import ConflictError, NotFoundError
from gateway.routing import RULES
from gateway.schemas import (
    AliasCreateRequest,
    AliasResponse,
    AliasTargetInput,
    AliasTargetResponse,
    AliasUpdateRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["administration"])

#: Everything an alias needs before it can be answered for: its route row, the
#: target it names, that target's provider (which decides
#: ``target_provider_inactive``) and its prices.
#:
#: Eager, and chained, on purpose. `ModelDef.route_targets` and
#: `ModelRouteTarget.target_model` are both `lazy="raise"` precisely so that a
#: metered request cannot issue an unaccounted query by touching one — which
#: means an admin screen that forgot to load them would get an
#: `InvalidRequestError` rather than silently working. Loading them here costs
#: three queries for the whole page, because `selectinload` batches by primary
#: key, not one per alias.
_ALIAS_LOADERS = (
    selectinload(ModelDef.route_targets)
    .selectinload(ModelRouteTarget.target_model)
    .selectinload(ModelDef.provider)
)


def _grant_names(rows: Sequence[Row[Any]]) -> dict[uuid.UUID, list[str]]:
    """Two-column grant rows into a name list per model id."""
    out: dict[uuid.UUID, list[str]] = {}
    for model_id, label in rows:
        out.setdefault(model_id, []).append(label)
    return out


async def _grants(
    session: SessionDep, model_ids: Sequence[uuid.UUID]
) -> tuple[dict[uuid.UUID, list[str]], dict[uuid.UUID, list[str]]]:
    """Group and personal grants, narrowed to the aliases being rendered.

    The same two queries, over the same tables and with the same labels, as the
    model page runs: an alias uses `group_model_access` and `user_model_access`
    exactly as a model does (ADR 0101, decision 1), which is what lets the
    console reuse the model's visibility controls instead of building a second
    set. Spelling the label the same way matters too — an administrator
    comparing "Research can use `fast`" with "Research can use `gpt-4o`" must
    not be looking at two renderings of one grant.
    """
    if not model_ids:
        return {}, {}
    group_rows = (
        await session.execute(
            select(GroupModelAccess.model_id, Group.name)
            .join(Group, Group.id == GroupModelAccess.group_id)
            .where(GroupModelAccess.model_id.in_(model_ids))
        )
    ).all()
    user_rows = (
        await session.execute(
            select(UserModelAccess.model_id, func.coalesce(User.email, User.subject))
            .join(User, User.id == UserModelAccess.user_id)
            .where(UserModelAccess.model_id.in_(model_ids))
        )
    ).all()
    return _grant_names(group_rows), _grant_names(user_rows)


def _response(
    alias: ModelDef,
    grants: dict[uuid.UUID, list[str]],
    user_grants: dict[uuid.UUID, list[str]],
) -> AliasResponse:
    """One alias, with its target and its availability.

    The unavailable states are computed from the target already in hand rather
    than by a query each, and classified by the same function the request path
    uses (`alias_unavailable_reason`) — so the console cannot say "Available"
    while every request 503s, which is the failure three surfaces disagreeing
    about invites.
    """
    targets: list[AliasTargetResponse] = []
    reasons = []
    for row in alias.route_targets:
        target = row.target_model
        reason = alias_unavailable_reason(alias, target)
        reasons.append(reason)
        targets.append(
            AliasTargetResponse(
                id=target.id,
                name=target.name,
                kind=target.kind.value,
                is_active=target.is_active,
                position=row.position,
                weight=row.weight,
                available=reason is None,
                unavailable_reason=None if reason is None else reason.value,
            )
        )
    # Available while any target can serve (issue P1); with none at all it is
    # the ADR 0101 "target deleted" state. When every target is down the reason
    # shown is the first one's, the one the operator listed as most preferred.
    if not targets:
        status_reason: str | None = AliasUnavailable.TARGET_DELETED.value
    elif any(reason is None for reason in reasons):
        status_reason = None
    else:
        first = reasons[0]
        status_reason = first.value if first is not None else None
    return AliasResponse(
        id=alias.id,
        name=alias.name,
        display_name=alias.display_name,
        description=alias.description,
        kind=alias.kind.value,
        is_active=alias.is_active,
        is_public=alias.is_public,
        target=targets[0] if targets else None,
        targets=targets,
        route_rule=alias.route_rule,
        route_sticky_ttl_seconds=alias.route_sticky_ttl_seconds,
        status="unavailable" if status_reason is not None else "available",
        unavailable_reason=status_reason,
        granted_to=sorted(grants.get(alias.id, [])),
        granted_to_users=sorted(user_grants.get(alias.id, [])),
        created_at=alias.created_at,
    )


async def _one_alias_response(session: SessionDep, alias_id: uuid.UUID) -> AliasResponse:
    """The response for one alias: re-read, then its grants.

    Re-read rather than reusing the in-session object, because a retarget
    replaces the route row and the identity map still holds the old one. Through
    the same loaders ``_load_alias`` uses — a
    `refresh(attribute_names=["route_targets"])` reloads the route rows
    *without* the target and provider they are chained to, so the very next
    `alias.target` raises instead of returning the model that was just set.
    """
    # `expire_all` before the re-read, not just a re-query: a select returns the
    # identity-mapped object, and after a retarget that object still holds the
    # route row that was deleted — so the re-read would faithfully return the
    # old target. An admin write is not a hot path and this is the only place
    # that has to think about it.
    session.expire_all()
    alias = await _load_alias(session, alias_id)
    grants, user_grants = await _grants(session, [alias.id])
    return _response(alias, grants, user_grants)


@router.get("/aliases", response_model=list[AliasResponse])
async def list_aliases(admin: AdminUserDep, session: SessionDep) -> list[AliasResponse]:
    """Every alias, available or not.

    The unavailable ones are listed rather than hidden, because fixing them is
    the administrator's job here: an alias whose target was deleted still holds
    grants and still answers to a name callers are sending, and a list that
    quietly omitted it would make that impossible to find.
    """
    aliases = list(
        (
            await session.execute(
                select(ModelDef)
                .where(ModelDef.is_alias.is_(True))
                .options(_ALIAS_LOADERS)
                .order_by(ModelDef.name)
            )
        )
        .scalars()
        .all()
    )
    grants, user_grants = await _grants(session, [alias.id for alias in aliases])
    return [_response(alias, grants, user_grants) for alias in aliases]


async def _load_alias(session: SessionDep, alias_id: uuid.UUID) -> ModelDef:
    """One alias by id, with its target loaded.

    The `is_alias` test is in the query rather than after it, so a **model** id
    passed here is a plain 404 rather than something that half-works: this route
    edits a target, and a model has no route to edit.
    """
    alias = (
        await session.execute(
            select(ModelDef)
            .where(ModelDef.id == alias_id, ModelDef.is_alias.is_(True))
            .options(_ALIAS_LOADERS)
        )
    ).scalar_one_or_none()
    if alias is None:
        raise NotFoundError(f"No alias with id {alias_id}.")
    return alias


async def _load_target(session: SessionDep, model_id: uuid.UUID, *, alias_name: str) -> ModelDef:
    """The model an alias may point at, or a 400 saying why not.

    Three refusals, each with a different fix (ADR 0101):
    * no such model — the id came from somewhere stale;
    * it is an alias — a chain, refused because "what serves this name?" would
      then need walking the table, and because a caller granted `fast` could
      reach a model nobody granted them;
    * it is inactive, or its provider is — an alias onto either is unavailable
      from the instant it is saved, which is a confusing way to create one.

    A new alias's `kind` is set to the target's by `_write_target`, so the two
    agree from the start: the request path checks the **target's** kind, and a
    row where they disagreed would be a second source of truth for a fact only
    one side of is ever read.
    """
    target = (
        await session.execute(
            select(ModelDef).where(ModelDef.id == model_id).options(selectinload(ModelDef.provider))
        )
    ).scalar_one_or_none()
    if target is None:
        raise BadRequestError(
            f"No model with id {model_id} to point {alias_name!r} at.", code="unknown_target"
        )
    if target.is_alias:
        raise BadRequestError(
            f"{target.name!r} is itself an alias. An alias points at a model, not at "
            "another alias.",
            code="alias_target_is_alias",
        )
    if not target.is_active:
        raise BadRequestError(
            f"{target.name!r} is inactive, so an alias pointing at it could never serve "
            "a request. Activate it first.",
            code="alias_target_inactive",
        )
    if target.provider is None or not target.provider.is_active:
        raise BadRequestError(
            f"{target.name!r} sits behind an inactive provider, so an alias pointing at "
            "it could never serve a request.",
            code="alias_target_provider_inactive",
        )
    return target


def _names(targets: Sequence[ModelDef]) -> str:
    """The route as one label for the audit trail: names, in order."""
    return ", ".join(t.name for t in targets)


def _check_rule(rule: str) -> None:
    if rule not in RULES:
        raise BadRequestError(
            f"Unknown routing rule {rule!r}. Known: {', '.join(sorted(RULES))}.",
            code="unknown_route_rule",
        )


async def _load_targets(
    session: SessionDep, entries: Sequence[AliasTargetInput], *, alias_name: str
) -> list[tuple[ModelDef, int]]:
    """Each entry through `_load_target`, refusing the same model twice.

    A duplicate would give the model two shares under `weighted` and two
    attempts under fallback, neither of which anybody means by listing it twice.
    """
    seen: set[uuid.UUID] = set()
    loaded: list[tuple[ModelDef, int]] = []
    for entry in entries:
        if entry.model_id in seen:
            raise BadRequestError(
                f"The same model is listed twice for {alias_name!r}.",
                code="alias_duplicate_target",
            )
        seen.add(entry.model_id)
        loaded.append(
            (await _load_target(session, entry.model_id, alias_name=alias_name), entry.weight)
        )
    return loaded


def _check_same_kind(
    alias_name: str, kind: ModelKind, targets: Sequence[tuple[ModelDef, int]]
) -> None:
    """Targets of one alias must serve the same API surfaces (ADR 0101)."""
    for target, _ in targets:
        if target.kind != kind:
            raise BadRequestError(
                f"{alias_name!r} is a {kind.value} alias and {target.name!r} is a "
                f"{target.kind.value} model. An alias can only point at a model that "
                "serves the same API surfaces.",
                code="alias_kind_mismatch",
            )


async def _write_targets(
    session: SessionDep,
    alias: ModelDef,
    targets: Sequence[tuple[ModelDef, int]],
    *,
    adopt_kind: bool,
) -> None:
    """Leave this alias with exactly these route targets, in this order.

    Delete-then-insert rather than an update, and that is what makes a change
    immediate and atomic: there is no cache to invalidate because resolution
    reads this table on every request, and the commit covers both statements, so
    a crash between them leaves the alias with its old route rather than with
    none. Position is the index in ``targets``. Sessions pinned to a target that
    is no longer in the route are not touched: they are stale, not wrong — the
    planner only honours a pin whose target is still in the list.
    """
    await session.execute(delete(ModelRouteTarget).where(ModelRouteTarget.alias_id == alias.id))
    for position, (target, weight) in enumerate(targets):
        session.add(
            ModelRouteTarget(
                alias_id=alias.id,
                target_model_id=target.id,
                position=position,
                weight=weight,
            )
        )
    if adopt_kind:
        alias.kind = targets[0][0].kind


@router.post("/aliases", response_model=AliasResponse, status_code=status.HTTP_201_CREATED)
async def create_alias(
    payload: AliasCreateRequest, admin: AdminUserDep, session: SessionDep
) -> AliasResponse:
    """Create an alias and point it at one model.

    Grants are **not** set here. An alias row uses `group_model_access`,
    `user_model_access` and `is_public` exactly as a model does, and those
    already have endpoints for models — so the console drives the same controls
    and there is one implementation of "who may use this name" rather than two
    that drift apart.
    """
    _check_rule(payload.route_rule)
    entries = payload.targets or [AliasTargetInput(model_id=payload.target_model_id)]
    targets = await _load_targets(session, entries, alias_name=payload.name)
    target = targets[0][0]
    _check_same_kind(payload.name, target.kind, targets)

    alias = ModelDef(
        name=payload.name,
        # An alias has no provider and no upstream model:
        # `ck_models_alias_or_served` requires both to be null, which is what
        # keeps it out of every query that assumed a served model. They are left
        # unwritten rather than written empty, because NULL and "" are different
        # claims — "" would match a provider that published an empty id.
        provider_id=None,
        upstream_model=None,
        is_alias=True,
        kind=target.kind,
        display_name=payload.display_name,
        description=payload.description,
        is_active=payload.is_active,
        is_public=payload.is_public,
        route_rule=payload.route_rule,
        route_sticky_ttl_seconds=payload.route_sticky_ttl_seconds,
    )
    session.add(alias)
    try:
        # The flush is *inside* the try on purpose. It is what emits the INSERT,
        # and so what raises the unique-name violation — one name namespace
        # across models and aliases (ADR 0101). Catching it only at commit would
        # have let a clashing name through as a 500.
        await session.flush()
        await _write_targets(session, alias, targets, adopt_kind=True)
        names = ", ".join(t.name for t, _ in targets)
        await record_event(
            session,
            actor_type=IdentityEventActor.USER,
            actor_user_id=admin.id,
            actor_label=admin.email or "",
            action=IdentityEventAction.ALIAS_CREATE,
            target_label=payload.name,
            detail={"target": names},
        )
        record_audit(
            session,
            admin,
            "alias.create",
            "alias",
            alias.id,
            alias.name,
            after=alias_view(alias, names),
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        # A 409 naming the name, not a 500: the operator can go and look at
        # what holds it, on the model page or the Routing tab.
        raise ConflictError(
            f"The name {payload.name!r} is already used by a model or another alias."
        ) from exc

    return await _one_alias_response(session, alias.id)


@router.patch("/aliases/{alias_id}", response_model=AliasResponse)
async def update_alias(
    alias_id: uuid.UUID,
    payload: AliasUpdateRequest,
    admin: AdminUserDep,
    session: SessionDep,
) -> AliasResponse:
    """Change an alias: rename it, edit its text, retarget it, or toggle it.

    A retarget takes effect on the next request — there is no cache to
    invalidate, because resolution reads the route table every time. That also
    means there is nothing to coordinate across requests: the change is one
    committed delete-and-insert, and a request either saw the old target or the
    new one.

    Renaming is allowed here, unlike a model's, because nothing foreign-keys an
    alias name: `usage_records.requested_model` is a copy of a string precisely
    so that a rename cannot rewrite history, and an alias whose name cannot be
    corrected is its own kind of bug.
    """
    alias = await _load_alias(session, alias_id)
    audit_before = alias_view(alias, _names(alias.targets))
    fields = payload.model_dump(exclude_unset=True)
    changed: list[str] = []

    if (rule := fields.get("route_rule")) is not None:
        _check_rule(rule)
    # `None` for a field that has no "unset" meaning is never a change.
    for field in ("route_rule", "route_sticky_ttl_seconds"):
        if field in fields and fields[field] is None:
            del fields[field]

    entries: list[AliasTargetInput] | None = None
    if (target_id := fields.pop("target_model_id", None)) is not None:
        entries = [AliasTargetInput(model_id=target_id)]
    if payload.targets is not None:
        fields.pop("targets", None)
        entries = payload.targets
    fields.pop("targets", None)

    names_after = _names(alias.targets)
    if entries is not None:
        targets = await _load_targets(session, entries, alias_name=alias.name)
        # Interchangeability (ADR 0101, "Designed for routing"): the targets of
        # one alias must be substitutable for one another, and the only thing
        # that can be checked here is the kind. Retargeting across kinds would
        # leave the alias promising chat to a caller who then gets a 400 naming
        # a model they never asked for.
        _check_same_kind(alias.name, alias.kind, targets)
        await _write_targets(session, alias, targets, adopt_kind=False)
        names_after = _names([t for t, _ in targets])
        changed.append("target")

    for field, value in fields.items():
        setattr(alias, field, value)
        changed.append(field)

    # The targets the alias has *after* the change: the one fact a retarget
    # leaves invisible everywhere else, because the alias card, the request and
    # the ledger all keep saying the same name.
    try:
        # `record_event` flushes, so it is inside the try for the same reason
        # the create path's flush is: that flush is what emits the UPDATE, and
        # so what raises the unique-name violation. Catching only at commit
        # would have left a rename onto a taken name as a 500.
        await record_event(
            session,
            actor_type=IdentityEventActor.USER,
            actor_user_id=admin.id,
            actor_label=admin.email or "",
            action=IdentityEventAction.ALIAS_UPDATE,
            target_label=alias.name,
            detail={"fields": sorted(changed), "target": names_after},
        )
        record_audit(
            session,
            admin,
            "alias.update",
            "alias",
            alias.id,
            alias.name,
            before=audit_before,
            after=alias_view(alias, names_after),
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            f"The name {payload.name!r} is already used by a model or another alias."
            if payload.name is not None
            else "That change would leave the model without a provider and an upstream model."
        ) from exc

    return await _one_alias_response(session, alias.id)


@router.delete("/aliases/{alias_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_alias(alias_id: uuid.UUID, admin: AdminUserDep, session: SessionDep) -> None:
    """Delete an alias. Its grants and its route row go with it.

    Safe against the ledger by the same construction a model's delete is:
    `usage_records.requested_model` is a string, not a foreign key, so every row
    that was served through this name still reads as served through it after the
    name is gone.
    """
    alias = await _load_alias(session, alias_id)
    target_name = _names(alias.targets)
    await record_event(
        session,
        actor_type=IdentityEventActor.USER,
        actor_user_id=admin.id,
        actor_label=admin.email or "",
        action=IdentityEventAction.ALIAS_DELETE,
        target_label=alias.name,
        detail={"target": target_name},
    )
    record_audit(
        session,
        admin,
        "alias.delete",
        "alias",
        alias.id,
        alias.name,
        before=alias_view(alias, target_name or None),
    )
    # Explicit child deletes rather than ORM cascade, matching `delete_model`:
    # the same outcome, and these name what goes.
    await session.execute(delete(RouteSession).where(RouteSession.alias_id == alias.id))
    await session.execute(delete(ModelRouteTarget).where(ModelRouteTarget.alias_id == alias.id))
    await session.execute(delete(GroupModelAccess).where(GroupModelAccess.model_id == alias.id))
    await session.execute(delete(UserModelAccess).where(UserModelAccess.model_id == alias.id))
    await session.execute(delete(ModelDef).where(ModelDef.id == alias.id))
    await session.commit()
