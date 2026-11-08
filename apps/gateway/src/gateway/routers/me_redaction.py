"""What redaction does to a user's requests, and what a user may add to it (ADR 0112).

``/api/me/redaction/*`` is for the console (a cookie session). The routes:

* the user's **own rules**, which can only tighten what the administrators set;
* the rules **in force**, the administrators' read-only, so a user can see what
  already applies to them;
* a **test box**: text in, what would be hidden and by which rule out;
* the **log** of requests where something was hidden, with no values in it.

Two things this module is careful about, because the data is the user's own:

* **The test box's text is never logged and never echoed.** It returns types,
  offsets and the deciding rule, and nothing that was in the text. It is the
  same discipline as the administrators' preview box, and for the same reason:
  within a week it holds the prompt that came back wrong.
* **The log holds no values** (``redaction.report``), so there is nothing here
  that a serializer could leak.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from typing import Any, cast

from fastapi import APIRouter, Request, status
from llmp_shared import EntitySpan
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from gateway.config import EffectivePolicy, EntityMode, RedactionPolicy
from gateway.deps import AdminUserDep, ManagementUserDep, SessionDep, SettingsDep
from gateway.errors import BadRequestError, GatewayError, NotFoundError, TooManyRequestsError
from gateway.models import (
    ApiKey,
    Group,
    Membership,
    ModelDef,
    Provider,
    RedactionRule,
    RedactionScope,
    UsageRecord,
    User,
    UserRedactionRule,
)
from gateway.pagination import Page, PageDep, count_of
from gateway.redaction.http import (
    eligible_for,
    resolve_overlaps,
)
from gateway.redaction.resolver import RedactionResolver
from gateway.redaction.user_rules import (
    MAX_RULES_PER_USER,
    UserRule,
    clean_label,
    tightens,
    validate_pattern,
)
from gateway.schemas import (
    AdminRuleView,
    MyRedactionInForce,
    RedactionEntityView,
    RedactionEventEntity,
    RedactionEventRow,
    RedactionPatternView,
    RedactionTestRequest,
    RedactionTestResponse,
    RedactionTestRule,
    RedactionTestSpan,
    UserRedactionRuleRequest,
    UserRedactionRuleResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/me/redaction", tags=["self-service"])
admin_router = APIRouter(prefix="/api/admin/redaction", tags=["administration"])


#: How many test-box runs one user may make a minute.
TEST_CALLS_PER_MINUTE = 30


class ConflictError(GatewayError):
    status_code = status.HTTP_409_CONFLICT
    error_type = "invalid_request_error"
    code = "conflict"


def _resolver(request: Request) -> RedactionResolver | None:
    resolver: RedactionResolver | None = getattr(request.app.state, "redaction", None)
    return resolver


def _rule_of(row: UserRedactionRule) -> UserRule | None:
    """A stored row as the fold reads it; None for one that no longer reads."""
    try:
        return UserRule(
            id=row.id,
            kind=row.kind,
            name=row.name,
            regex=row.regex,
            mode=EntityMode(row.mode),
        )
    except ValueError:
        return None


def _admin_view(resolver: RedactionResolver | None, user: User) -> Any:
    """The administrators' policy for this user, without their own rules."""
    if resolver is None:
        return None
    return resolver.admin_policy_for(group_id=user.default_billing_group_id, user_id=user.id)


def _response(
    row: UserRedactionRule, resolver: RedactionResolver | None, user: User
) -> UserRedactionRuleResponse:
    rule = _rule_of(row)
    admin = _admin_view(resolver, user)
    covered = rule is not None and admin is not None and not tightens(admin.policy, rule)
    admin_mode = None
    if admin is not None and row.kind == "mode":
        admin_mode = admin.policy.mode_for(row.name).value
    return UserRedactionRuleResponse(
        id=row.id,
        kind=cast("Any", row.kind),
        name=row.name,
        regex=row.regex,
        mode=row.mode,
        status="covered" if covered else "adds_protection",
        admin_mode=admin_mode,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _my_rows(session: SessionDep, user_id: uuid.UUID) -> list[UserRedactionRule]:
    return list(
        (
            await session.execute(
                select(UserRedactionRule)
                .where(UserRedactionRule.user_id == user_id)
                .order_by(UserRedactionRule.created_at, UserRedactionRule.id)
            )
        )
        .scalars()
        .all()
    )


def _publish(resolver: RedactionResolver | None, user_id: uuid.UUID, rows: Sequence[Any]) -> None:
    """Take the edit on this worker at once; the others learn on their poll."""
    if resolver is not None:
        resolver.replace_user_rules(user_id, rows)


def _checked(body: UserRedactionRuleRequest) -> tuple[str, str | None]:
    """The label and regex a request names, under the administrators' own validation."""
    try:
        name = clean_label(body.name)
        mode = EntityMode(body.mode)
        if body.kind == "pattern":
            assert body.regex is not None
            pattern = validate_pattern(name, body.regex, mode)
            return pattern.name, pattern.regex
        return name, None
    except ValueError as exc:
        # Pydantic's message carries RE2's own wording ("this pattern cannot be
        # used: ..."), which says which construct was refused.
        raise BadRequestError(_message_of(exc), code="invalid_rule") from exc


def _message_of(exc: ValueError) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        parts = [str(item.get("msg", "")).removeprefix("Value error, ") for item in errors()]
        return "; ".join(part for part in parts if part) or str(exc)
    return str(exc)


@router.get("/rules", response_model=Page[UserRedactionRuleResponse])
async def list_my_rules(
    user: ManagementUserDep, session: SessionDep, request: Request, page: PageDep
) -> Page[UserRedactionRuleResponse]:
    """The caller's own rules. The user is the caller's; there is no way to name another."""
    resolver = _resolver(request)
    stmt = (
        select(UserRedactionRule)
        .where(UserRedactionRule.user_id == user.id)
        .order_by(UserRedactionRule.created_at, UserRedactionRule.id)
    )
    total = await count_of(session, stmt)
    rows = (await session.execute(page.apply(stmt))).scalars().all()
    return page.page([_response(row, resolver, user) for row in rows], total)


@router.post("/rules", response_model=UserRedactionRuleResponse, status_code=201)
async def create_my_rule(
    body: UserRedactionRuleRequest, user: ManagementUserDep, session: SessionDep, request: Request
) -> UserRedactionRuleResponse:
    name, regex = _checked(body)
    # Serialise this user's creates on their own row, so two at once cannot both
    # pass the count below. The lock is held until the commit.
    await session.execute(select(User.id).where(User.id == user.id).with_for_update())
    existing = int(
        (
            await session.execute(
                select(func.count())
                .select_from(UserRedactionRule)
                .where(UserRedactionRule.user_id == user.id)
            )
        ).scalar_one()
    )
    if existing >= MAX_RULES_PER_USER:
        raise BadRequestError(
            f"You can keep at most {MAX_RULES_PER_USER} rules. Remove one to add another.",
            code="too_many_rules",
        )
    row = UserRedactionRule(user_id=user.id, kind=body.kind, name=name, regex=regex, mode=body.mode)
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            f"You already have a {'pattern' if body.kind == 'pattern' else 'rule for the type'} "
            f"named {name}."
        ) from exc
    await session.refresh(row)
    resolver = _resolver(request)
    _publish(resolver, user.id, await _my_rows(session, user.id))
    return _response(row, resolver, user)


async def _mine(session: SessionDep, user: User, rule_id: uuid.UUID) -> UserRedactionRule:
    row = (
        await session.execute(
            select(UserRedactionRule).where(
                UserRedactionRule.id == rule_id, UserRedactionRule.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        # Another user's rule is "not found", never "forbidden": whether it
        # exists is not for this caller to learn.
        raise NotFoundError(f"No rule with id {rule_id}.")
    return row


@router.put("/rules/{rule_id}", response_model=UserRedactionRuleResponse)
async def update_my_rule(
    rule_id: uuid.UUID,
    body: UserRedactionRuleRequest,
    user: ManagementUserDep,
    session: SessionDep,
    request: Request,
) -> UserRedactionRuleResponse:
    row = await _mine(session, user, rule_id)
    name, regex = _checked(body)
    row.kind, row.name, row.regex, row.mode = body.kind, name, regex, body.mode
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"You already have a rule named {name}.") from exc
    await session.refresh(row)
    resolver = _resolver(request)
    _publish(resolver, user.id, await _my_rows(session, user.id))
    return _response(row, resolver, user)


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_rule(
    rule_id: uuid.UUID, user: ManagementUserDep, session: SessionDep, request: Request
) -> None:
    row = await _mine(session, user, rule_id)
    await session.delete(row)
    await session.commit()
    _publish(_resolver(request), user.id, await _my_rows(session, user.id))


# -- the rules in force --------------------------------------------------------


@router.get("/in-force", response_model=MyRedactionInForce)
async def rules_in_force(
    user: ManagementUserDep, session: SessionDep, settings: SettingsDep, request: Request
) -> MyRedactionInForce:
    """The administrators' rules that can apply to this user, read-only.

    The rules that name everyone, this user, one of their groups or keys, and
    every provider and model rule (those apply by where a request goes). Types
    and modes only: not the regexes of an administrator's patterns, nor the
    values on an allow-list.
    """
    redactor = getattr(request.app.state, "redactor", None)
    engine = getattr(redactor, "name", None) or settings.redaction.engine

    groups = (
        (await session.execute(select(Membership.group_id).where(Membership.user_id == user.id)))
        .scalars()
        .all()
    )
    keys = (
        (await session.execute(select(ApiKey.id).where(ApiKey.user_id == user.id))).scalars().all()
    )
    rules = list(
        (
            await session.execute(
                select(RedactionRule)
                .where(
                    RedactionRule.is_active.is_(True),
                    or_(
                        RedactionRule.scope == RedactionScope.ALL,
                        # Where a request goes: deployment providers and models,
                        # and this user's own, never another user's private ones.
                        (RedactionRule.scope == RedactionScope.PROVIDER)
                        & RedactionRule.scope_id.in_(
                            select(Provider.id).where(
                                or_(Provider.owner_id.is_(None), Provider.owner_id == user.id)
                            )
                        ),
                        (RedactionRule.scope == RedactionScope.MODEL)
                        & RedactionRule.scope_id.in_(
                            select(ModelDef.id).where(
                                or_(ModelDef.owner_id.is_(None), ModelDef.owner_id == user.id)
                            )
                        ),
                        (RedactionRule.scope == RedactionScope.USER)
                        & (RedactionRule.scope_id == user.id),
                        (RedactionRule.scope == RedactionScope.GROUP)
                        & RedactionRule.scope_id.in_(list(groups)),
                        (RedactionRule.scope == RedactionScope.API_KEY)
                        & RedactionRule.scope_id.in_(list(keys)),
                    ),
                )
                .order_by(RedactionRule.scope, RedactionRule.created_at)
            )
        )
        .scalars()
        .all()
    )

    labels = await _labels(session, rules)
    views: list[AdminRuleView] = []
    for rule in rules:
        try:
            policy = RedactionPolicy.model_validate(rule.policy or {})
        except ValueError:
            continue
        views.append(
            AdminRuleView(
                id=rule.id,
                name=rule.name,
                scope=cast("Any", rule.scope.value),
                subject_label=labels.get((rule.scope, rule.scope_id)),
                default_mode=policy.default_mode.value,
                entities=[
                    RedactionEntityView(entity_type=name, mode=entry.mode.value)
                    for name, entry in sorted(policy.entities.items())
                ],
                patterns=[
                    RedactionPatternView(name=pattern.name, mode=pattern.mode.value)
                    for pattern in policy.patterns
                ],
                exceptions=len(policy.allow_list),
            )
        )
    return MyRedactionInForce(
        engine=engine,
        engine_active=engine != "noop",
        rules=views,
        max_user_rules=MAX_RULES_PER_USER,
    )


async def _labels(
    session: SessionDep, rules: Sequence[RedactionRule]
) -> dict[tuple[RedactionScope, uuid.UUID | None], str]:
    """Names for the groups, models and providers the rules attach to.

    Only those three: the user's own rule and their own keys need no name, and
    another user's email is never shown here.
    """
    wanted: dict[RedactionScope, set[uuid.UUID]] = {}
    for rule in rules:
        if rule.scope_id is not None and rule.scope in (
            RedactionScope.GROUP,
            RedactionScope.MODEL,
            RedactionScope.PROVIDER,
        ):
            wanted.setdefault(rule.scope, set()).add(rule.scope_id)
    columns = {
        RedactionScope.GROUP: (Group.id, Group.name),
        RedactionScope.MODEL: (ModelDef.id, ModelDef.name),
        RedactionScope.PROVIDER: (Provider.id, Provider.name),
    }
    labels: dict[tuple[RedactionScope, uuid.UUID | None], str] = {}
    for scope, ids in wanted.items():
        id_column, name_column = columns[scope]
        for subject_id, label in (
            await session.execute(select(id_column, name_column).where(id_column.in_(ids)))
        ).all():
            labels[(scope, subject_id)] = str(label)
    return labels


# -- the test box --------------------------------------------------------------


@router.post("/test", response_model=RedactionTestResponse)
async def test_my_rules(
    body: RedactionTestRequest,
    user: ManagementUserDep,
    settings: SettingsDep,
    request: Request,
) -> RedactionTestResponse:
    """What would be hidden in this text, by which rule. Offsets, never the text.

    Runs the real path (detection, then the same eligibility and overlap rules a
    request uses) under the policy this user's requests run under: the
    administrators' rules for them and their default billing group, then their
    own. A blocked request is a 200 with ``blocked: true``. **Nothing here is
    logged**: not the text, not the spans.
    """
    # Each call is a full detector run, which no quota meters. Per user and per
    # worker, like the announce limiter: it damps a loop, it is not a ceiling.
    if not request.app.state.redaction_test_limiter.allow(("redaction-test", str(user.id))):
        raise TooManyRequestsError(
            f"You can try at most {TEST_CALLS_PER_MINUTE} texts a minute. "
            "Wait a moment and try again."
        )
    resolver = _resolver(request)
    redactor: object = getattr(request.app.state, "redactor", None)
    engine = getattr(redactor, "name", None) or settings.redaction.engine
    if engine == "noop" or not hasattr(redactor, "detect"):
        return RedactionTestResponse(
            engine_active=False,
            spans=[],
            note="Redaction is switched off on this deployment, so nothing is hidden "
            "whatever rules exist.",
        )

    effective: EffectivePolicy = (
        resolver.policy_for(group_id=user.default_billing_group_id, user_id=user.id)
        if resolver is not None
        else EffectivePolicy(policy=settings.redaction.policy)
    )
    policy = effective.policy
    detector = cast("Any", redactor)
    detected: list[EntitySpan] = (await detector.detect([body.text], policy=policy))[0]
    eligible = eligible_for(
        body.text,
        detected,
        policy,
        settings.redaction.score_threshold,
        effective.user_pattern_keys(),
    )
    blocked = any(policy.mode_for(span.entity_type) is EntityMode.BLOCK for span in eligible)
    chosen = resolve_overlaps(body.text, eligible)
    described: list[RedactionTestSpan] = []
    for span in chosen:
        decider = effective.decider_for(span.entity_type)
        described.append(
            RedactionTestSpan(
                entity_type=span.entity_type,
                start=span.start,
                end=span.end,
                mode=policy.mode_for(span.entity_type).value,
                rule=(
                    RedactionTestRule(
                        id=decider.id, name=decider.name, owner=cast("Any", decider.owner)
                    )
                    if decider is not None
                    else None
                ),
            )
        )
    return RedactionTestResponse(engine_active=True, spans=described, blocked=blocked)


# -- the log ---------------------------------------------------------------------


def _event_row(r: Any, *, with_user: bool) -> RedactionEventRow:
    detail = r.redaction_detail or {}
    return RedactionEventRow(
        request_id=r.request_id,
        created_at=r.created_at,
        user_id=r.user_id if with_user else None,
        user_label=r.user_label if with_user else None,
        model=r.model_name,
        provider=r.provider_name,
        byok=bool(r.byok),
        blocked=bool(detail.get("blocked")),
        entities=[RedactionEventEntity.model_validate(e) for e in detail.get("entities", [])],
    )


def _events_query(
    *, user_id: uuid.UUID | None, request_id: str | None, own_keys: bool | None
) -> Any:
    stmt = (
        select(
            UsageRecord.request_id,
            UsageRecord.created_at,
            UsageRecord.user_id,
            func.coalesce(User.email, User.subject).label("user_label"),
            UsageRecord.model_name,
            Provider.name.label("provider_name"),
            UsageRecord.byok,
            UsageRecord.redaction_detail,
        )
        .select_from(UsageRecord)
        .outerjoin(User, User.id == UsageRecord.user_id)
        .outerjoin(ModelDef, ModelDef.id == UsageRecord.model_id)
        .outerjoin(Provider, Provider.id == ModelDef.provider_id)
        .where(UsageRecord.redaction_detail.is_not(None))
    )
    if user_id is not None:
        stmt = stmt.where(UsageRecord.user_id == user_id)
    if request_id:
        stmt = stmt.where(UsageRecord.request_id == request_id)
    if own_keys is not None:
        stmt = stmt.where(UsageRecord.byok.is_(own_keys))
    return stmt.order_by(UsageRecord.created_at.desc(), UsageRecord.id.desc())


@router.get("/events", response_model=Page[RedactionEventRow])
async def my_redaction_events(
    user: ManagementUserDep,
    session: SessionDep,
    page: PageDep,
    request_id: str | None = None,
) -> Page[RedactionEventRow]:
    """Requests of the caller's where redaction hid something or refused, newest first.

    The caller's own requests only, **including the ones on their own provider
    keys** (BYOK; flagged ``byok``): those requests are theirs, and nobody else
    reads them. No parameter names another user. The entries carry no values.
    """
    stmt = _events_query(user_id=user.id, request_id=request_id, own_keys=None)
    total = await count_of(session, stmt)
    rows = (await session.execute(page.apply(stmt))).all()
    return page.page([_event_row(r, with_user=False) for r in rows], total)


@admin_router.get("/events", response_model=Page[RedactionEventRow])
async def all_redaction_events(
    admin: AdminUserDep,
    session: SessionDep,
    page: PageDep,
    request_id: str | None = None,
    user_id: uuid.UUID | None = None,
) -> Page[RedactionEventRow]:
    """Everyone's redaction log, newest first, for an administrator.

    **Never a user's own-key (BYOK) requests**, as every administrator read of
    the ledger: those belong to their owner. No values, as in the user's view.
    """
    stmt = _events_query(user_id=user_id, request_id=request_id, own_keys=False)
    total = await count_of(session, stmt)
    rows = (await session.execute(page.apply(stmt))).all()
    return page.page([_event_row(r, with_user=True) for r in rows], total)
