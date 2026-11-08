"""``/api/admin/privacy``: pipelines, redaction policies, profiles and the test box (ADR 0113).

Two objects, one job each. A **policy** is what to hide (patterns, entity types,
a mode for each) and has no scope. A **pipeline** is when and how: a kind (read,
embed, chat), a scope (who, which target), an ordered list of rules where the
first match wins, and a fixed last rule. Every pipeline in scope runs and the
results combine (``gateway.privacy.combine``).

Everything an administrator changes is audited like any other admin change, in
the same transaction as the change. After a write the worker that served it
reloads its state at once; the others catch up on their next poll (ten seconds).

Saving never blocks on a *warning* (a rule that cannot fire, a profile that is
down): warnings come back with the pipeline and from ``/pipelines/check`` so the
console can say them while the pipeline is being written. Only an *error* (a
value of the wrong shape, an action the kind does not have, an id that does not
exist) is a 422.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Request, status
from sqlalchemy import func, select

from gateway.admin_audit import record_audit
from gateway.deps import (
    AdminUserDep,
    ControlHttpDep,
    SessionDep,
    SettingsDep,
)
from gateway.errors import BadRequestError, GatewayError, NotFoundError
from gateway.models import (
    ApiKey,
    DetectorProfile,
    Group,
    ModelDef,
    PrivacyPipeline,
    Provider,
    RedactionPolicyRecord,
    User,
    UserPolicy,
)
from gateway.pagination import MAX_LIMIT, Page
from gateway.privacy import rules as rulelib
from gateway.privacy import service
from gateway.privacy.pipelines import Pipeline, pipeline_from_row, validate_pipeline
from gateway.privacy.schemas import (
    ActionBody,
    DryRunRequest,
    DryRunResponse,
    IssueView,
    ModelOption,
    NamedId,
    OptionsResponse,
    PipelineCheckRequest,
    PipelineCheckResponse,
    PipelineCreateRequest,
    PipelineResponse,
    PipelineUpdateRequest,
    PolicyCreateRequest,
    PolicyResponse,
    PolicyUpdateRequest,
    ProfileCreateRequest,
    ProfileResponse,
    ProfileUpdateRequest,
    ReaderOption,
    ReorderRequest,
    SettingsRequest,
    SettingsResponse,
)
from gateway.privacy.state import engine_available, state_of
from gateway.rate_limit import SlidingWindowLimiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/privacy", tags=["administration"])


class UnprocessableError(GatewayError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_type = "invalid_request_error"
    code = "invalid_rule"


class ConflictError(GatewayError):
    status_code = status.HTTP_409_CONFLICT
    error_type = "invalid_request_error"
    code = "conflict"


async def _reload(request: Request) -> None:
    """Make this worker see the change now; the others poll."""
    await state_of(request).refresh()


@router.get("/status")
async def privacy_status(admin: AdminUserDep, request: Request) -> dict[str, Any]:
    """Whether this worker has loaded the pipelines, and any row it had to skip."""
    state = state_of(request)
    return {
        "loaded": state.loaded,
        "load_error": state.load_error,
        "pipelines": len(state.pipelines),
        "policies": len(state.policies),
        "unreadable": list(state.bad_rows),
    }


# -- options -----------------------------------------------------------------------


@router.get("/options", response_model=OptionsResponse)
async def options(admin: AdminUserDep, session: SessionDep, request: Request) -> OptionsResponse:
    """What the editors may pick: scope subjects, targets, readers, policies, profiles."""
    users = (
        await session.execute(select(User.id, User.email).order_by(User.email).limit(500))
    ).all()
    groups = (await session.execute(select(Group.id, Group.name).order_by(Group.name))).all()
    keys = (
        await session.execute(select(ApiKey.id, ApiKey.name).order_by(ApiKey.name).limit(500))
    ).all()
    providers = (
        await session.execute(
            select(Provider.id, Provider.name)
            .where(Provider.owner_id.is_(None))
            .order_by(Provider.name)
        )
    ).all()
    models = (
        await session.execute(
            select(ModelDef.id, ModelDef.name, ModelDef.provider_id, Provider.name, ModelDef.kind)
            .join(Provider, Provider.id == ModelDef.provider_id, isouter=True)
            .where(ModelDef.owner_id.is_(None), ModelDef.is_alias.is_(False))
            .order_by(ModelDef.name)
        )
    ).all()
    readers = await service.deployment_readers(session)
    policies = [p for p in await service.policy_rows(session) if p.is_active]
    profiles = await service.profile_rows(session)
    settings_row = await service.settings_row(session)
    return OptionsResponse(
        users=[NamedId(id=i, name=n or "") for i, n in users],
        groups=[NamedId(id=i, name=n) for i, n in groups],
        keys=[NamedId(id=i, name=n or "(unnamed key)") for i, n in keys],
        providers=[NamedId(id=i, name=n) for i, n in providers],
        models=[
            ModelOption(
                id=i,
                name=n,
                provider_id=pid,
                provider=pn,
                kind=k.value if hasattr(k, "value") else str(k),
            )
            for i, n, pid, pn, k in models
        ],
        readers=[ReaderOption(id=rulelib.BUILTIN_READER, name="Built-in extractor", builtin=True)]
        + [
            ReaderOption(id=name, name=name, builtin=builtin)
            for name, (_, builtin) in readers.items()
        ],
        policies=[NamedId(id=p.id, name=p.name) for p in policies],
        own_policies=[],
        profiles=[NamedId(id=p.id, name=p.name) for p in profiles],
        entity_types=settings_row.user_entity_types,
        engines=list(rulelib.ENGINES),
    )


# -- pipelines ---------------------------------------------------------------------


async def _admin_rows(session: SessionDep, kind: str | None = None) -> list[PrivacyPipeline]:
    rows = await service.pipeline_rows(session, owner=None)
    return [r for r in rows if kind is None or r.kind == kind]


async def _load(session: SessionDep, pipeline_id: uuid.UUID) -> PrivacyPipeline:
    row = await session.get(PrivacyPipeline, pipeline_id)
    if row is None or row.owner_id is not None:
        raise NotFoundError("No such pipeline.")
    return row


async def _check_subjects(session: SessionDep, who: dict[str, Any], target: dict[str, Any]) -> None:
    """Every id named in the scope must exist: a scope on nothing looks like a live one."""

    async def exists(column: Any, ids: list[str], noun: str) -> None:
        if not ids:
            return
        wanted = {uuid.UUID(i) for i in ids}
        found = set(
            (await session.execute(select(column).where(column.in_(wanted)))).scalars().all()
        )
        if found != wanted:
            raise UnprocessableError(f"One of the {noun} named does not exist.")

    mode = who["mode"]
    if mode == "users":
        await exists(User.id, who["ids"], "users")
    elif mode == "groups":
        await exists(Group.id, who["ids"], "groups")
    elif mode == "keys":
        await exists(ApiKey.id, who["ids"], "keys")
    await exists(Provider.id, target["providers"], "providers")
    await exists(ModelDef.id, target["models"], "models")


async def _clean(
    session: SessionDep,
    *,
    kind: str,
    name: str,
    who: Any,
    target: Any,
    rules: list[dict[str, Any]],
    otherwise: dict[str, Any] | None,
) -> dict[str, Any]:
    profiles, policies, readers = await service.validation_context(session, owner=None)
    try:
        clean = validate_pipeline(
            kind,
            name,
            who,
            target,
            rules,
            otherwise,
            profile_ids=profiles,
            policy_ids=policies,
            readers=readers,
        )
    except rulelib.RuleError as exc:
        raise UnprocessableError(str(exc)) from exc
    await _check_subjects(session, clean["who"], clean["target"])
    return clean


async def _renumber(session: SessionDep, ordered: list[PrivacyPipeline]) -> None:
    for index, row in enumerate(ordered):
        if row.position != index:
            row.position = index
    await session.flush()


async def _response(session: SessionDep, row: PrivacyPipeline) -> PipelineResponse:
    return (await service.views_for(session, [row]))[0]


@router.get("/pipelines", response_model=Page[PipelineResponse])
async def list_pipelines(
    admin: AdminUserDep, session: SessionDep, kind: str | None = None
) -> Page[PipelineResponse]:
    """Every administrator pipeline, by kind and in order, each with its warnings."""
    items = await service.views_for(session, await _admin_rows(session, kind))
    return Page(items=items, total=len(items), limit=max(1, min(MAX_LIMIT, len(items))), offset=0)


@router.get("/pipelines/{pipeline_id}", response_model=PipelineResponse)
async def get_pipeline(
    pipeline_id: uuid.UUID, admin: AdminUserDep, session: SessionDep
) -> PipelineResponse:
    return await _response(session, await _load(session, pipeline_id))


def _other_action(payload: ActionBody | None) -> dict[str, Any] | None:
    return payload.clean() if payload is not None else None


@router.post("/pipelines", response_model=PipelineResponse, status_code=status.HTTP_201_CREATED)
async def create_pipeline(
    payload: PipelineCreateRequest, admin: AdminUserDep, session: SessionDep, request: Request
) -> PipelineResponse:
    clean = await _clean(
        session,
        kind=payload.kind,
        name=payload.name,
        who=payload.who.model_dump(mode="json"),
        target=payload.target.model_dump(mode="json"),
        rules=[r.clean() for r in payload.rules],
        otherwise=_other_action(payload.otherwise),
    )
    siblings = await _admin_rows(session, payload.kind)
    if any(r.name.lower() == clean["name"].lower() for r in siblings):
        raise ConflictError(f"A {payload.kind} pipeline named '{clean['name']}' already exists.")
    row = PrivacyPipeline(
        id=uuid.uuid4(),
        owner_id=None,
        kind=payload.kind,
        name=clean["name"],
        enabled=payload.enabled,
        position=len(siblings),
        who=clean["who"],
        target=clean["target"],
        rules=clean["rules"],
        otherwise=clean["otherwise"],
        created_by=admin.id,
    )
    session.add(row)
    ordered = list(siblings)
    index = min(payload.position, len(siblings)) if payload.position is not None else len(siblings)
    ordered.insert(index, row)
    await _renumber(session, ordered)
    record_audit(
        session,
        admin,
        "privacy_pipeline.create",
        "privacy_pipeline",
        row.id,
        row.name,
        after=service.pipeline_view_json(row),
    )
    await session.commit()
    await _reload(request)
    return await _response(session, row)


@router.patch("/pipelines/{pipeline_id}", response_model=PipelineResponse)
async def update_pipeline(
    pipeline_id: uuid.UUID,
    payload: PipelineUpdateRequest,
    admin: AdminUserDep,
    session: SessionDep,
    request: Request,
) -> PipelineResponse:
    row = await _load(session, pipeline_id)
    before = service.pipeline_view_json(row)
    fields = payload.model_fields_set
    clean = await _clean(
        session,
        kind=row.kind,
        name=payload.name if "name" in fields and payload.name else row.name,
        who=payload.who.model_dump(mode="json") if payload.who is not None else row.who,
        target=payload.target.model_dump(mode="json") if payload.target is not None else row.target,
        rules=(
            [r.clean() for r in payload.rules]
            if payload.rules is not None
            else [dict(r) for r in row.rules or []]
        ),
        otherwise=(
            _other_action(payload.otherwise)
            if payload.otherwise is not None
            else dict(row.otherwise or {})
        ),
    )
    if clean["name"].lower() != row.name.lower():
        taken = [r for r in await _admin_rows(session, row.kind) if r.id != row.id]
        if any(r.name.lower() == clean["name"].lower() for r in taken):
            raise ConflictError(f"A {row.kind} pipeline named '{clean['name']}' already exists.")
    row.name = clean["name"]
    row.who = clean["who"]
    row.target = clean["target"]
    row.rules = clean["rules"]
    row.otherwise = clean["otherwise"]
    if payload.enabled is not None:
        row.enabled = payload.enabled
    record_audit(
        session,
        admin,
        "privacy_pipeline.update",
        "privacy_pipeline",
        row.id,
        row.name,
        before=before,
        after=service.pipeline_view_json(row),
    )
    await session.commit()
    await _reload(request)
    return await _response(session, row)


@router.delete("/pipelines/{pipeline_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pipeline(
    pipeline_id: uuid.UUID, admin: AdminUserDep, session: SessionDep, request: Request
) -> None:
    row = await _load(session, pipeline_id)
    before = service.pipeline_view_json(row)
    kind, name = row.kind, row.name
    await session.delete(row)
    await session.flush()
    await _renumber(session, await _admin_rows(session, kind))
    record_audit(
        session,
        admin,
        "privacy_pipeline.delete",
        "privacy_pipeline",
        pipeline_id,
        name,
        before=before,
    )
    await session.commit()
    await _reload(request)


@router.post("/pipelines/reorder", response_model=Page[PipelineResponse])
async def reorder_pipelines(
    payload: ReorderRequest, admin: AdminUserDep, session: SessionDep, request: Request
) -> Page[PipelineResponse]:
    """Put a kind's administrator pipelines in a new order. Must name every one once."""
    rows = await _admin_rows(session, payload.kind)
    by_id = {r.id: r for r in rows}
    if sorted(payload.order, key=str) != sorted(by_id, key=str):
        raise UnprocessableError("The order must list every pipeline of this kind exactly once.")
    before = [r.name for r in rows]
    ordered = [by_id[i] for i in payload.order]
    await _renumber(session, ordered)
    record_audit(
        session,
        admin,
        "privacy_pipeline.reorder",
        "privacy_pipeline",
        None,
        f"{payload.kind} pipelines",
        before={"order": before},
        after={"order": [r.name for r in ordered]},
    )
    await session.commit()
    await _reload(request)
    return await list_pipelines(admin, session)


@router.post("/pipelines/check", response_model=PipelineCheckResponse)
async def check_pipeline(
    payload: PipelineCheckRequest, admin: AdminUserDep, session: SessionDep
) -> PipelineCheckResponse:
    """The warnings a draft would get if saved now, for the editor to show as it is written."""
    try:
        clean = await _clean(
            session,
            kind=payload.kind,
            name=payload.name,
            who=payload.who.model_dump(mode="json"),
            target=payload.target.model_dump(mode="json"),
            rules=[r.clean() for r in payload.rules],
            otherwise=_other_action(payload.otherwise),
        )
    except UnprocessableError as exc:
        return PipelineCheckResponse(error=exc.message, issues=[], soft_cap=rulelib.SOFT_CAP)
    draft = _draft(payload.kind, clean, payload.enabled)
    found = service.issues_of(draft, await service.profile_rows(session))
    issues: list[IssueView] = []
    for rule_issues in found.values():
        issues.extend(service.issue_views(rule_issues))
    return PipelineCheckResponse(issues=issues, soft_cap=rulelib.SOFT_CAP)


def _draft(kind: str, clean: dict[str, Any], enabled: bool) -> Pipeline:
    row = PrivacyPipeline(
        id=uuid.uuid4(),
        owner_id=None,
        kind=kind,
        name=clean["name"],
        enabled=enabled,
        position=0,
        who=clean["who"],
        target=clean["target"],
        rules=clean["rules"],
        otherwise=clean["otherwise"],
    )
    return pipeline_from_row(row)


# -- policies ----------------------------------------------------------------------


async def _used_by(session: SessionDep) -> dict[str, list[str]]:
    """policy id -> names of the pipelines (administrators' and users') that apply it."""
    used: dict[str, list[str]] = {}
    for row in (await session.execute(select(PrivacyPipeline))).scalars().all():
        actions = [dict(r.get("action") or {}) for r in row.rules or []] + [
            dict(row.otherwise or {})
        ]
        for pid in {str(a["redact_with"]) for a in actions if a.get("redact_with")}:
            used.setdefault(pid, []).append(
                row.name if row.owner_id is None else f"{row.name} (user)"
            )
    return used


def _policy_response(
    row: RedactionPolicyRecord,
    used: dict[str, list[str]],
    profiles: dict[uuid.UUID, str],
    email: str | None,
) -> PolicyResponse:
    from gateway.config import RedactionPolicy

    return PolicyResponse(
        id=row.id,
        name=row.name,
        # A stored policy that no longer parses is not replaced with an empty one
        # here: the listing is where an operator would find out the state is
        # ignoring it, so it must raise rather than lie.
        policy=RedactionPolicy.model_validate(row.policy or {}),
        profile_id=row.profile_id,
        profile_name=profiles.get(row.profile_id) if row.profile_id else None,
        is_active=row.is_active,
        reason=row.reason,
        used_by=used.get(str(row.id), []),
        created_by=row.created_by,
        created_by_email=email,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _policy_page(session: SessionDep) -> list[PolicyResponse]:
    used = await _used_by(session)
    profiles = {p.id: p.name for p in await service.profile_rows(session)}
    rows = (
        await session.execute(
            select(RedactionPolicyRecord, User.email)
            .outerjoin(User, User.id == RedactionPolicyRecord.created_by)
            .order_by(RedactionPolicyRecord.name)
        )
    ).all()
    return [_policy_response(row, used, profiles, email) for row, email in rows]


async def _load_policy(session: SessionDep, policy_id: uuid.UUID) -> RedactionPolicyRecord:
    row = await session.get(RedactionPolicyRecord, policy_id)
    if row is None:
        raise NotFoundError(f"No redaction policy with id {policy_id}.")
    return row


async def _check_profile(session: SessionDep, profile_id: uuid.UUID | None) -> None:
    if profile_id is not None and await session.get(DetectorProfile, profile_id) is None:
        raise UnprocessableError("The detector profile does not exist.")


async def _name_free(session: SessionDep, name: str, ignore: uuid.UUID | None = None) -> None:
    rows = (
        (
            await session.execute(
                select(RedactionPolicyRecord.id).where(
                    func.lower(RedactionPolicyRecord.name) == name.lower()
                )
            )
        )
        .scalars()
        .all()
    )
    if any(r != ignore for r in rows):
        raise ConflictError(f"A redaction policy named '{name}' already exists.")


def _policy_audit(row: RedactionPolicyRecord) -> dict[str, Any]:
    return {
        "name": row.name,
        "is_active": row.is_active,
        "profile_id": str(row.profile_id) if row.profile_id else None,
        "policy": dict(row.policy or {}),
    }


@router.get("/policies", response_model=Page[PolicyResponse])
async def list_policies(admin: AdminUserDep, session: SessionDep) -> Page[PolicyResponse]:
    items = await _policy_page(session)
    return Page(items=items, total=len(items), limit=max(1, min(MAX_LIMIT, len(items))), offset=0)


@router.post("/policies", response_model=PolicyResponse, status_code=status.HTTP_201_CREATED)
async def create_policy(
    payload: PolicyCreateRequest, admin: AdminUserDep, session: SessionDep, request: Request
) -> PolicyResponse:
    name = payload.name.strip()
    if not name:
        raise UnprocessableError("Give the policy a name.")
    await _name_free(session, name)
    await _check_profile(session, payload.profile_id)
    row = RedactionPolicyRecord(
        id=uuid.uuid4(),
        name=name,
        policy=payload.policy.model_dump(mode="json"),
        profile_id=payload.profile_id,
        is_active=payload.is_active,
        reason=payload.reason.strip(),
        created_by=admin.id,
    )
    session.add(row)
    record_audit(
        session,
        admin,
        "redaction_policy.create",
        "redaction_policy",
        row.id,
        row.name,
        after=_policy_audit(row),
    )
    await session.commit()
    await _reload(request)
    await session.refresh(row)
    return _policy_response(
        row, {}, {p.id: p.name for p in await service.profile_rows(session)}, admin.email
    )


@router.patch("/policies/{policy_id}", response_model=PolicyResponse)
async def update_policy(
    policy_id: uuid.UUID,
    payload: PolicyUpdateRequest,
    admin: AdminUserDep,
    session: SessionDep,
    request: Request,
) -> PolicyResponse:
    row = await _load_policy(session, policy_id)
    before = _policy_audit(row)
    fields = payload.model_fields_set
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise UnprocessableError("Give the policy a name.")
        await _name_free(session, name, ignore=row.id)
        row.name = name
    if payload.policy is not None:
        row.policy = payload.policy.model_dump(mode="json")
    if "profile_id" in fields:
        await _check_profile(session, payload.profile_id)
        row.profile_id = payload.profile_id
    if payload.is_active is not None:
        row.is_active = payload.is_active
    if payload.reason is not None:
        row.reason = payload.reason.strip()
    record_audit(
        session,
        admin,
        "redaction_policy.update",
        "redaction_policy",
        row.id,
        row.name,
        before=before,
        after=_policy_audit(row),
    )
    await session.commit()
    await _reload(request)
    await session.refresh(row)
    used = await _used_by(session)
    email = (
        await session.execute(select(User.email).where(User.id == row.created_by))
    ).scalar_one_or_none()
    return _policy_response(
        row, used, {p.id: p.name for p in await service.profile_rows(session)}, email
    )


@router.delete("/policies/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_policy(
    policy_id: uuid.UUID, admin: AdminUserDep, session: SessionDep, request: Request
) -> None:
    """Remove a policy that no pipeline applies.

    Refused while one does: a pipeline that names nothing would silently stop
    hiding what it was written to hide. Nothing in the ledger points at a policy
    (``usage_records.redaction_rule_id`` is a stamp, not a foreign key), so
    deleting one cannot erase the record of what it did.
    """
    row = await _load_policy(session, policy_id)
    users = (await _used_by(session)).get(str(row.id), [])
    if users:
        raise ConflictError(
            f"'{row.name}' is applied by: {', '.join(users)}. Change those pipelines first."
        )
    before = _policy_audit(row)
    await session.delete(row)
    record_audit(
        session,
        admin,
        "redaction_policy.delete",
        "redaction_policy",
        policy_id,
        row.name,
        before=before,
    )
    await session.commit()
    await _reload(request)


# -- profiles ----------------------------------------------------------------------


def _profile_view(row: DetectorProfile) -> dict[str, Any]:
    return {
        "name": row.name,
        "description": row.description,
        "engine": row.engine,
        "labels": list(row.labels or []),
        "fallback": row.fallback,
        "fallback_profile_id": str(row.fallback_profile_id) if row.fallback_profile_id else None,
        "user_available": row.user_available,
    }


def _presidio_status(request: Request) -> tuple[str, str]:
    redactor = getattr(request.app.state, "redactor", None)
    name = getattr(redactor, "name", "noop")
    if name == "noop":
        return "available", "Redaction is off; verdicts still use the quick check."
    return "available", "The detection service is the one in use for redaction."


async def _profile_use(session: SessionDep) -> dict[str, int]:
    """How many rules decide with each profile, and how many policies use it."""
    count: dict[str, int] = {}
    for row in (await session.execute(select(PrivacyPipeline))).scalars().all():
        for rule in row.rules or []:
            for c in rule.get("conditions") or []:
                if c.get("decide") and c["decide"] != rulelib.PATTERNS:
                    count[str(c["decide"])] = count.get(str(c["decide"]), 0) + 1
    for pid in (
        await session.execute(
            select(RedactionPolicyRecord.profile_id).where(
                RedactionPolicyRecord.profile_id.is_not(None)
            )
        )
    ).scalars():
        count[str(pid)] = count.get(str(pid), 0) + 1
    for pid in (
        await session.execute(
            select(UserPolicy.profile_id).where(UserPolicy.profile_id.is_not(None))
        )
    ).scalars():
        count[str(pid)] = count.get(str(pid), 0) + 1
    return count


async def _profile_response(
    request: Request, row: DetectorProfile, names: dict[uuid.UUID, str], using: dict[str, int]
) -> ProfileResponse:
    available = engine_available(row.engine)
    if row.engine == "presidio":
        status_word, detail = _presidio_status(request)
    elif available:
        status_word, detail = "available", "Built in; nothing to install."
    else:
        status_word, detail = "not_installed", "This engine is not installed here."
    return ProfileResponse(
        id=row.id,
        name=row.name,
        description=row.description,
        engine=row.engine,
        labels=list(row.labels or []),
        fallback=row.fallback,
        fallback_profile_id=row.fallback_profile_id,
        fallback_profile_name=names.get(row.fallback_profile_id)
        if row.fallback_profile_id
        else None,
        is_default=row.is_default,
        user_available=row.user_available,
        status=status_word,
        status_detail=detail,
        decides=available,
        cost_hint="instant" if available else "slow; arrives with model profiles",
        rules_using=using.get(str(row.id), 0),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/profiles", response_model=Page[ProfileResponse])
async def list_profiles(
    admin: AdminUserDep, session: SessionDep, request: Request
) -> Page[ProfileResponse]:
    profiles = await service.profile_rows(session)
    names = {p.id: p.name for p in profiles}
    using = await _profile_use(session)
    items = [await _profile_response(request, p, names, using) for p in profiles]
    return Page(items=items, total=len(items), limit=max(1, len(items)), offset=0)


async def _check_fallback(
    session: SessionDep, row_id: uuid.UUID | None, fallback: str, target: uuid.UUID | None
) -> None:
    if fallback == "fallback_to":
        if target is None:
            raise UnprocessableError("Choose the profile to fall back to.")
        if target == row_id:
            raise UnprocessableError("A profile cannot fall back to itself.")
        if await session.get(DetectorProfile, target) is None:
            raise UnprocessableError("The fallback profile does not exist.")
        # No cycles: walk the chain from the target; reaching this row would loop.
        seen: set[uuid.UUID] = set()
        current: uuid.UUID | None = target
        while current is not None and current not in seen:
            if current == row_id:
                raise UnprocessableError("That fallback would loop back to this profile.")
            seen.add(current)
            node = await session.get(DetectorProfile, current)
            current = node.fallback_profile_id if node and node.fallback == "fallback_to" else None
    elif target is not None:
        raise UnprocessableError(
            "A fallback profile only applies when the fallback is 'fall back'."
        )


@router.post("/profiles", response_model=ProfileResponse, status_code=status.HTTP_201_CREATED)
async def create_profile(
    payload: ProfileCreateRequest, admin: AdminUserDep, session: SessionDep, request: Request
) -> ProfileResponse:
    name = payload.name.strip()
    if not name:
        raise UnprocessableError("Give the profile a name.")
    existing = await service.profile_rows(session)
    if any(p.name.lower() == name.lower() for p in existing):
        raise ConflictError(f"A profile named '{name}' already exists.")
    await _check_fallback(session, None, payload.fallback, payload.fallback_profile_id)
    row = DetectorProfile(
        id=uuid.uuid4(),
        name=name,
        description=payload.description.strip(),
        engine=payload.engine,
        labels=[label.strip().upper() for label in payload.labels if label.strip()],
        fallback=payload.fallback,
        fallback_profile_id=payload.fallback_profile_id,
        user_available=payload.user_available,
    )
    session.add(row)
    record_audit(
        session,
        admin,
        "privacy_profile.create",
        "privacy_profile",
        row.id,
        row.name,
        after=_profile_view(row),
    )
    await session.commit()
    await _reload(request)
    names = {p.id: p.name for p in [*existing, row]}
    return await _profile_response(request, row, names, await _profile_use(session))


@router.patch("/profiles/{profile_id}", response_model=ProfileResponse)
async def update_profile(
    profile_id: uuid.UUID,
    payload: ProfileUpdateRequest,
    admin: AdminUserDep,
    session: SessionDep,
    request: Request,
) -> ProfileResponse:
    row = await session.get(DetectorProfile, profile_id)
    if row is None:
        raise NotFoundError("No such profile.")
    before = _profile_view(row)
    fields = payload.model_fields_set
    fallback = payload.fallback if payload.fallback is not None else row.fallback
    target = (
        payload.fallback_profile_id if "fallback_profile_id" in fields else row.fallback_profile_id
    )
    if fallback == "refuse":
        target = None
    await _check_fallback(session, row.id, fallback, target)
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise UnprocessableError("Give the profile a name.")
        if any(
            p.name.lower() == name.lower() and p.id != row.id
            for p in await service.profile_rows(session)
        ):
            raise ConflictError(f"A profile named '{name}' already exists.")
        row.name = name
    if payload.description is not None:
        row.description = payload.description.strip()
    if payload.labels is not None:
        row.labels = [label.strip().upper() for label in payload.labels if label.strip()]
    if payload.user_available is not None:
        row.user_available = payload.user_available
    row.fallback = fallback
    row.fallback_profile_id = target
    record_audit(
        session,
        admin,
        "privacy_profile.update",
        "privacy_profile",
        row.id,
        row.name,
        before=before,
        after=_profile_view(row),
    )
    await session.commit()
    await _reload(request)
    profiles = await service.profile_rows(session)
    return await _profile_response(
        request, row, {p.id: p.name for p in profiles}, await _profile_use(session)
    )


@router.delete("/profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(
    profile_id: uuid.UUID, admin: AdminUserDep, session: SessionDep, request: Request
) -> None:
    row = await session.get(DetectorProfile, profile_id)
    if row is None:
        raise NotFoundError("No such profile.")
    if row.is_default:
        raise ConflictError("The default profile cannot be deleted.")
    if (await _profile_use(session)).get(str(row.id)):
        raise ConflictError("A rule or policy still uses this profile. Change those first.")
    before = _profile_view(row)
    await session.delete(row)
    record_audit(
        session,
        admin,
        "privacy_profile.delete",
        "privacy_profile",
        profile_id,
        row.name,
        before=before,
    )
    await session.commit()
    await _reload(request)


# -- what users may do ---------------------------------------------------------------


@router.get("/settings", response_model=SettingsResponse)
async def get_settings(admin: AdminUserDep, session: SessionDep) -> SettingsResponse:
    row = await service.settings_row(session)
    await session.commit()
    return SettingsResponse(user_entity_types=row.user_entity_types)


@router.put("/settings", response_model=SettingsResponse)
async def put_settings(
    payload: SettingsRequest, admin: AdminUserDep, session: SessionDep, request: Request
) -> SettingsResponse:
    """The entity types users' own policies may name (null: any the detector offers)."""
    row = await service.settings_row(session)
    before = {"user_entity_types": row.user_entity_types}
    types = (
        None
        if payload.user_entity_types is None
        else sorted({t.strip().upper() for t in payload.user_entity_types if t.strip()})
    )
    row.user_entity_types = types
    record_audit(
        session,
        admin,
        "privacy_settings.update",
        "privacy_settings",
        None,
        "Privacy settings",
        before=before,
        after={"user_entity_types": types},
    )
    await session.commit()
    await _reload(request)
    return SettingsResponse(user_entity_types=types)


# -- the test box ------------------------------------------------------------------


@router.post("/test", response_model=DryRunResponse)
async def test_pipelines(
    payload: DryRunRequest,
    admin: AdminUserDep,
    session: SessionDep,
    request: Request,
    settings: SettingsDep,
    control_http: ControlHttpDep,
) -> DryRunResponse:
    """Which pipelines are in scope, what each does, and the combined outcome.

    Reads the state as saved. The text and the file are judged in memory and never
    stored or logged; the answer carries labels and names, not content. Rate limited.
    """
    limiter = getattr(request.app.state, "privacy_test_limiter", None)
    if limiter is None:
        # Always limited: the box can run the slow engine and the extractor.
        limiter = request.app.state.privacy_test_limiter = SlidingWindowLimiter(
            max_calls=30, window_seconds=60
        )
    if not limiter.allow(("privacy-test", str(admin.id))):
        raise BadRequestError("Slow down: the test box is limited to 30 runs a minute.")
    state = await scratch_state(session, request)
    user = await session.get(User, payload.user_id) if payload.user_id else None
    if payload.user_id is not None and user is None:
        raise BadRequestError("No such user.")
    return await service.dry_run(
        session,
        state,
        payload,
        user=user,
        settings=settings,
        control_http=control_http,
        redactor=getattr(request.app.state, "redactor", None),
    )


async def scratch_state(session: SessionDep, request: Request) -> Any:
    """The saved state, not this worker's last poll: someone who just saved must see it applied."""
    from gateway.privacy.state import PrivacyState, read_all

    scratch = PrivacyState()
    scratch.cache = state_of(request).cache
    scratch.load(*await read_all(session))
    return scratch
