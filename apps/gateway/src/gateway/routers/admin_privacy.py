"""``/api/admin/privacy``: pipelines, policies, classifiers and the test box (ADR 0113).

Two objects, one job each. A **policy** is what to find and what to do about it
(patterns, entity types, a mode for each; see section 0c) and has no scope.
A **pipeline** is when and how: a kind (read, embed, chat), a scope (who, which
target), an ordered list of rules where the first match wins, and a fixed last rule.
Every pipeline in scope runs and the results combine (``gateway.privacy.combine``).

Everything an administrator changes is audited like any other admin change, in
the same transaction as the change. After a write the worker that served it
reloads its state at once; the others catch up on their next poll (ten seconds).

Saving never blocks on a *warning* (a rule that cannot fire, a policy whose
engine is down): warnings come back with the pipeline and from ``/pipelines/check`` so the
console can say them while the pipeline is being written. Only an *error* (a
value of the wrong shape, an action the kind does not have, an id that does not
exist) is a 422.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from typing import Any

from fastapi import APIRouter, Request, status
from sqlalchemy import func, select

from gateway.admin_audit import record_audit
from gateway.config import EntityMode, RedactionPolicy
from gateway.deps import (
    AdminUserDep,
    ControlHttpDep,
    SessionDep,
    SettingsDep,
)
from gateway.errors import BadRequestError, GatewayError, NotFoundError
from gateway.models import (
    ApiKey,
    Group,
    ModelDef,
    ModelKind,
    PrivacyClassifier,
    PrivacyPipeline,
    Provider,
    RedactionPolicyRecord,
    User,
)
from gateway.pagination import MAX_LIMIT, Page
from gateway.privacy import rules as rulelib
from gateway.privacy import service
from gateway.privacy.engines import (
    effective_engines,
    engine_problem,
    ner_state,
    no_ner_message,
    usable,
)
from gateway.privacy.pipelines import Pipeline, pipeline_from_row, validate_pipeline
from gateway.privacy.schemas import (
    ActionBody,
    ChatModelOption,
    ClassifierCreateRequest,
    ClassifierListResponse,
    ClassifierResponse,
    ClassifierUpdateRequest,
    DryRunRequest,
    DryRunResponse,
    EnginesResponse,
    FoundItem,
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
    PolicyTryRequest,
    PolicyTryResponse,
    PolicyUpdateRequest,
    ReaderOption,
    ReorderRequest,
    SettingsRequest,
    SettingsResponse,
)
from gateway.privacy.state import state_of
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
async def options(
    admin: AdminUserDep,
    session: SessionDep,
    request: Request,
    control_http: ControlHttpDep,
    settings: SettingsDep,
) -> OptionsResponse:
    """What the editors may pick: scope subjects, targets, readers, policies, classifiers."""
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
    # The built-in extractor's models are offered only when it can run them now.
    gone = await service.unavailable_readers(session, control_http, settings)
    readers = {
        n: local
        for n, local in (await service.deployment_readers(session)).items()
        if n not in gone
    }
    policies = [p for p in await service.policy_rows(session) if p.is_active]
    classifiers = await service.classifier_rows(session)
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
        readers=[ReaderOption(id=name, name=name, local=local) for name, local in readers.items()],
        policies=[NamedId(id=p.id, name=p.name) for p in policies],
        own_policies=[],
        classifiers=[NamedId(id=c.id, name=c.name) for c in classifiers],
        entity_types=settings_row.user_entity_types,
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
    context = await service.validation_context(session, owner=None)
    try:
        clean = validate_pipeline(
            kind,
            name,
            who,
            target,
            rules,
            otherwise,
            finds_policy_ids=context.finds_policy_ids,
            classifier_ids=context.classifier_ids,
            policy_ids=context.policy_ids,
            readers=context.readers,
        )
    except rulelib.ContentOnReadError as exc:
        raise BadRequestError(str(exc)) from exc
    except rulelib.RuleError as exc:
        raise UnprocessableError(str(exc)) from exc
    await _check_subjects(session, clean["who"], clean["target"])
    return clean


async def _renumber(session: SessionDep, ordered: list[PrivacyPipeline]) -> None:
    for index, row in enumerate(ordered):
        if row.position != index:
            row.position = index
    await session.flush()


async def _response(
    session: SessionDep, row: PrivacyPipeline, request: Request
) -> PipelineResponse:
    return (await service.views_for(session, [row], state_of(request)))[0]


@router.get("/pipelines", response_model=Page[PipelineResponse])
async def list_pipelines(
    admin: AdminUserDep, session: SessionDep, request: Request, kind: str | None = None
) -> Page[PipelineResponse]:
    """Every administrator pipeline, by kind and in order, each with its warnings."""
    items = await service.views_for(session, await _admin_rows(session, kind), state_of(request))
    return Page(items=items, total=len(items), limit=max(1, min(MAX_LIMIT, len(items))), offset=0)


@router.get("/pipelines/{pipeline_id}", response_model=PipelineResponse)
async def get_pipeline(
    pipeline_id: uuid.UUID, admin: AdminUserDep, session: SessionDep, request: Request
) -> PipelineResponse:
    return await _response(session, await _load(session, pipeline_id), request)


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
    return await _response(session, row, request)


@router.patch("/pipelines/{pipeline_id}", response_model=PipelineResponse)
async def update_pipeline(
    pipeline_id: uuid.UUID,
    payload: PipelineUpdateRequest,
    admin: AdminUserDep,
    session: SessionDep,
    request: Request,
) -> PipelineResponse:
    row = await _load(session, pipeline_id)
    payload.require_same_kind(row.kind)
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
    return await _response(session, row, request)


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
    return await list_pipelines(admin, session, request)


@router.post("/pipelines/check", response_model=PipelineCheckResponse)
async def check_pipeline(
    payload: PipelineCheckRequest,
    admin: AdminUserDep,
    session: SessionDep,
    request: Request,
    settings: SettingsDep,
    control_http: ControlHttpDep,
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
    found = service.issues_of(
        draft,
        state_of(request),
        usable(await effective_engines(session, control_http, settings)),
        await service.unavailable_readers(session, control_http, settings),
    )
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
    """policy or classifier id -> names of the pipelines that apply it or count its items."""
    used: dict[str, list[str]] = {}
    for row in (await session.execute(select(PrivacyPipeline))).scalars().all():
        name = row.name if row.owner_id is None else f"{row.name} (user)"
        rules = list(row.rules or [])
        ids = {
            str(a["redact_with"])
            for a in [dict(r.get("action") or {}) for r in rules] + [dict(row.otherwise or {})]
            if a.get("redact_with")
        }
        for rule in rules:
            for condition in rule.get("conditions") or []:
                if condition.get("key") in rulelib.CONTENT_KEYS and condition.get("values"):
                    ids.add(str(condition["values"][0]))
        for ident in ids:
            used.setdefault(ident, []).append(name)
    return used


def _policy_response(
    row: RedactionPolicyRecord,
    used: dict[str, list[str]],
    email: str | None,
    engines: EnginesResponse | None = None,
) -> PolicyResponse:

    # A stored policy that no longer parses is not replaced with an empty one
    # here: the listing is where an operator would find out the state is
    # ignoring it, so it must raise rather than lie.
    policy = RedactionPolicy.model_validate(row.policy or {})
    available: bool | None = None
    if engines is not None and engines.reachable and policy.engine is not None:
        available = engine_problem(engines, policy.engine, False) is None
    return PolicyResponse(
        id=row.id,
        name=row.name,
        policy=policy,
        is_active=row.is_active,
        shared_with_users=row.shared_with_users,
        engine_available=available,
        reason=row.reason,
        used_by=used.get(str(row.id), []),
        created_by=row.created_by,
        created_by_email=email,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _policy_page(
    session: SessionDep, engines: EnginesResponse | None = None
) -> list[PolicyResponse]:
    used = await _used_by(session)
    rows = (
        await session.execute(
            select(RedactionPolicyRecord, User.email)
            .outerjoin(User, User.id == RedactionPolicyRecord.created_by)
            .order_by(RedactionPolicyRecord.name)
        )
    ).all()
    return [_policy_response(row, used, email, engines) for row, email in rows]


async def _load_policy(session: SessionDep, policy_id: uuid.UUID) -> RedactionPolicyRecord:
    row = await session.get(RedactionPolicyRecord, policy_id)
    if row is None:
        raise NotFoundError(f"No redaction policy with id {policy_id}.")
    return row


def _check_engine(policy: Any, engines: EnginesResponse) -> None:
    """The engine a policy names must be one the service reports available, and fit its items.

    A policy that names no engine (the deployment default) is never refused for
    that, even with the service down: it is what every policy did before. Entity
    and format names are checked against the service's own lists when it answers.
    """
    problem = engine_problem(engines, policy.engine, bool(policy.custom_entities))
    if problem is not None:
        raise UnprocessableError(problem)
    if not engines.reachable:
        return
    chosen = next(
        (e for e in engines.engines if e.name == policy.engine),
        next((e for e in engines.engines if e.default), None),
    )
    needs_ner = bool(policy.entities or policy.custom_entities)
    if chosen is not None and needs_ner and not (chosen.available and chosen.entities):
        # The engine in force (named, or the default when none is named) cannot find entities:
        # switched off, not installed, or a pattern-only image. Every entity item would be
        # unserved, and a condition that counts them could never answer.
        if not chosen.enabled:
            raise UnprocessableError(
                f"The engine '{chosen.name}' is switched off on the Detectors tab."
            )
        state = ner_state(engines)
        if state in ("none", "off"):
            raise UnprocessableError(no_ner_message(engines))
        raise UnprocessableError(
            f"The engine '{chosen.name}' reports no entity types "
            f"(it has no NER model), so it cannot find: "
            f"{', '.join(sorted({*policy.entities, *(c.name for c in policy.custom_entities)}))}. "
            "Choose an engine that has one."
        )
    if chosen is not None and chosen.entities:
        unknown = sorted(set(policy.entities) - {e.upper() for e in chosen.entities})
        if unknown:
            raise UnprocessableError(
                f"The engine '{chosen.name}' does not find: {', '.join(unknown)}."
            )
    if chosen is not None and chosen.labels:
        taken = sorted(
            c.label for c in policy.custom_entities if c.label.casefold() in chosen.labels
        )
        if taken:
            raise UnprocessableError(
                f"The label {', '.join(repr(t) for t in taken)} is one the engine '{chosen.name}' "
                "already uses for its own types: choose another description."
            )
    if engines.pattern_entities or not engines.patterns_enabled:
        # A format the service no longer lists (AGE, ID, EMAIL were advertised by the
        # spaCy recogniser's label map, never by a pattern) may stay in a policy as
        # long as it is off: kept and visible, never silently dropped, and not asked.
        known = {e.upper() for e in engines.pattern_entities}
        unknown = sorted(
            name
            for name, entry in policy.formats.items()
            if name not in known and entry.mode is not EntityMode.OFF
        )
        if unknown:
            raise UnprocessableError(
                f"The redaction service has no built-in format: {', '.join(unknown)}. "
                "Set it to Off to keep it in the policy, or remove it."
            )


async def _check_label_budget(
    session: SessionDep,
    policy: Any,
    engines: EnginesResponse,
    ignore: uuid.UUID | None = None,
) -> None:
    """Refuse custom entity types that could not be asked of an engine together.

    The active policies of one engine are asked of it in one call, so across them:

    * at most ``MAX_CUSTOM_LABELS`` distinct types (the service takes no more);
    * a name stands for one label and a label for one name (the service keys the answer
      by label, the gateway by name, so a clash would give one policy the other's matches).

    Run on a save of an active policy and whenever a policy is turned on, so the request
    path does not meet these as errors. ``policy`` is a model or the stored dict.
    """
    from llmp_shared import MAX_CUSTOM_LABELS

    default = next((e.name for e in engines.engines if e.default), None)

    def key(engine: str | None) -> str | None:
        return engine or default

    def customs(raw: Any) -> set[tuple[str, str]]:
        items = raw.get("custom_entities") if isinstance(raw, dict) else raw.custom_entities
        return {
            (str(c.get("name")), str(c.get("label"))) if isinstance(c, dict) else (c.name, c.label)
            for c in items or []
        }

    mine = customs(policy)
    if not mine:
        return
    mine_engine = policy.get("engine") if isinstance(policy, dict) else policy.engine
    rows = (
        await session.execute(
            select(RedactionPolicyRecord).where(RedactionPolicyRecord.is_active.is_(True))
        )
    ).scalars()
    together = set(mine)
    for row in rows:
        if row.id == ignore:
            continue
        other = dict(row.policy or {})
        if key(other.get("engine")) != key(mine_engine):
            continue
        together |= customs(other)
    labels: dict[str, set[str]] = {}
    names: dict[str, set[str]] = {}
    for name, label in together:
        labels.setdefault(label.casefold(), set()).add(name)
        names.setdefault(name.casefold(), set()).add(label)
    my_labels = {label.casefold() for _, label in mine}
    my_names = {name.casefold() for name, _ in mine}
    clash = sorted(label for label, n in labels.items() if len(n) > 1 and label in my_labels)
    if clash:
        raise UnprocessableError(
            f"The description {', '.join(repr(c) for c in clash)} is used for different "
            "custom entity names in active policies of this engine: give each name its own "
            "description."
        )
    split = sorted(name for name, ls in names.items() if len(ls) > 1 and name in my_names)
    if split:
        raise UnprocessableError(
            f"The custom entity name {', '.join(repr(n) for n in split)} has different "
            "descriptions in active policies of this engine: use the same description."
        )
    if len({label.casefold() for _, label in together}) > MAX_CUSTOM_LABELS:
        raise UnprocessableError(
            f"With the other active policies of this engine, {len(together)} custom entity "
            f"types could be asked of the engine at once; the service takes {MAX_CUSTOM_LABELS}. "
            "Remove some or turn a policy off."
        )


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
        "shared_with_users": row.shared_with_users,
        "policy": dict(row.policy or {}),
    }


@router.get("/engines", response_model=EnginesResponse)
async def engines(
    admin: AdminUserDep, session: SessionDep, settings: SettingsDep, control_http: ControlHttpDep
) -> EnginesResponse:
    """What the redaction service offers: pattern formats and the NER engines (spaCy, GLiNER)."""
    return await effective_engines(session, control_http, settings)


@router.get("/policies", response_model=Page[PolicyResponse])
async def list_policies(
    admin: AdminUserDep, session: SessionDep, settings: SettingsDep, control_http: ControlHttpDep
) -> Page[PolicyResponse]:
    items = await _policy_page(
        session, usable(await effective_engines(session, control_http, settings))
    )
    return Page(items=items, total=len(items), limit=max(1, min(MAX_LIMIT, len(items))), offset=0)


@router.post("/policies", response_model=PolicyResponse, status_code=status.HTTP_201_CREATED)
async def create_policy(
    payload: PolicyCreateRequest,
    admin: AdminUserDep,
    session: SessionDep,
    request: Request,
    settings: SettingsDep,
    control_http: ControlHttpDep,
) -> PolicyResponse:
    name = payload.name.strip()
    if not name:
        raise UnprocessableError("Give the policy a name.")
    await _name_free(session, name)
    report = usable(await effective_engines(session, control_http, settings))
    _check_engine(payload.policy, report)
    if payload.is_active:
        await _check_label_budget(session, payload.policy, report)
    row = RedactionPolicyRecord(
        id=uuid.uuid4(),
        name=name,
        policy=payload.policy.model_dump(mode="json"),
        is_active=payload.is_active,
        shared_with_users=payload.shared_with_users,
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
    return _policy_response(row, {}, admin.email, report)


@router.patch("/policies/{policy_id}", response_model=PolicyResponse)
async def update_policy(
    policy_id: uuid.UUID,
    payload: PolicyUpdateRequest,
    admin: AdminUserDep,
    session: SessionDep,
    request: Request,
    settings: SettingsDep,
    control_http: ControlHttpDep,
) -> PolicyResponse:
    row = await _load_policy(session, policy_id)
    before = _policy_audit(row)
    report = usable(await effective_engines(session, control_http, settings))
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise UnprocessableError("Give the policy a name.")
        await _name_free(session, name, ignore=row.id)
        row.name = name
    if payload.policy is not None:
        _check_engine(payload.policy, report)
        row.policy = payload.policy.model_dump(mode="json")
    if payload.is_active is None and payload.policy is not None and row.is_active:
        await _check_label_budget(session, dict(row.policy or {}), report, ignore=row.id)
    if payload.shared_with_users is not None:
        if payload.shared_with_users is False and row.shared_with_users:
            used_by = (await _used_by(session)).get(str(row.id), [])
            users = [n for n in used_by if n.endswith(" (user)")]
            if users:
                raise ConflictError(
                    f"'{row.name}' is used by users' pipelines: {', '.join(users)}. They would "
                    "silently stop being protected."
                )
        row.shared_with_users = payload.shared_with_users
    if payload.is_active is not None:
        if payload.is_active is False and row.is_active:
            users = (await _used_by(session)).get(str(row.id), [])
            if users:
                raise ConflictError(
                    f"'{row.name}' is applied by: {', '.join(users)}. Change those pipelines "
                    "first, or it would silently stop hiding what they were written to hide."
                )
        if payload.is_active and not row.is_active:
            # Turning a policy on checks it against what the service offers now: it was saved
            # against what it offered then.
            _check_engine(RedactionPolicy.model_validate(row.policy or {}), report)
            # Turning a policy on brings its custom types in with the others'.
            await _check_label_budget(session, dict(row.policy or {}), report, ignore=row.id)
        elif payload.is_active and payload.policy is not None:
            await _check_label_budget(session, dict(row.policy or {}), report, ignore=row.id)
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
    return _policy_response(row, used, email, report)


@router.delete("/policies/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_policy(
    policy_id: uuid.UUID, admin: AdminUserDep, session: SessionDep, request: Request
) -> None:
    """Remove a policy that no pipeline applies or counts items of.

    Refused while one does: a pipeline that names nothing would silently stop
    hiding what it was written to hide. Nothing in the ledger points at a policy
    (``usage_records.redaction_rule_id`` is a stamp, not a foreign key), so
    deleting one cannot erase the record of what it did.
    """
    row = await _load_policy(session, policy_id)
    users = (await _used_by(session)).get(str(row.id), [])
    if users:
        raise ConflictError(
            f"'{row.name}' is used by: {', '.join(users)}. Change those pipelines first."
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


@router.post("/policies/try", response_model=PolicyTryResponse)
async def try_policy(
    payload: PolicyTryRequest,
    admin: AdminUserDep,
    request: Request,
    settings: SettingsDep,
) -> PolicyTryResponse:
    """Run a policy as it is being edited over a text: the real detection, no side effects.

    The same code a "finds at least" condition and the Test tab run
    (``gateway.privacy.finds.find_in``), with every item mode but ``off`` counted,
    and what the redaction would do to the text. Nothing is stored or logged; the
    text came from the box and is shown back only to the administrator who typed
    it. Rate limited with the test box.
    """
    from llmp_shared import PlaceholderMap

    from gateway.errors import ContentBlockedError, ServiceUnavailableError
    from gateway.privacy.finds import find_in
    from gateway.redaction.http import EngineUnavailableError, apply_spans

    limiter = getattr(request.app.state, "privacy_test_limiter", None)
    if limiter is not None and not limiter.allow(("privacy-test", str(admin.id))):
        raise BadRequestError("Slow down: the test box is limited to 30 runs a minute.")
    redactor = getattr(request.app.state, "redactor", None)
    policy = payload.policy
    result = await find_in(
        policy,
        [payload.text],
        redactor=redactor,
        default_threshold=settings.redaction.score_threshold,
    )
    items = [
        FoundItem(
            group=i.group,
            type=i.type,
            engine=i.engine,
            score=i.score,
            mode=policy.mode_for(i.type).value,
            text=i.value,
        )
        for i in result.items
    ]
    redacted: str | None = None
    blocked = False
    if policy.is_active() and result.unavailable is None:
        # The same spans the redaction would act on: detect_only items are left alone.
        detect = getattr(redactor, "detect", None)
        spans = []
        if detect is not None and getattr(redactor, "name", "noop") != "noop":
            try:
                spans = (
                    await detect([payload.text], policy=policy, asks=policy.ner_asks(), strict=True)
                )[0]
            except EngineUnavailableError as exc:
                raise ServiceUnavailableError(
                    f"The NER engine {exc.engine or 'chosen by default'} cannot run: "
                    f"{exc.reason or 'it is not available'}."
                ) from exc
        try:
            redacted, _ = apply_spans(
                payload.text,
                spans,
                # A throwaway key: what comes back is a preview, never a placeholder anyone
                # could match against a real request's.
                key=secrets.token_bytes(32),
                placeholders=PlaceholderMap(),
                policy=policy,
                default_threshold=settings.redaction.score_threshold,
            )
        except ContentBlockedError:
            blocked = True
    return PolicyTryResponse(
        items=items, unavailable=result.unavailable, redacted_text=redacted, blocked=blocked
    )


# -- classifiers -------------------------------------------------------------------


def _classifier_view(
    row: PrivacyClassifier, state: Any, used: dict[str, list[str]]
) -> ClassifierResponse:
    ref = state.classifiers.get(str(row.id))
    return ClassifierResponse(
        id=row.id,
        name=row.name,
        topic=row.topic,
        model_id=row.model_id,
        model_name=ref.model_name if ref is not None else None,
        provider_name=(ref.provider.name if ref is not None and ref.provider is not None else None),
        available=bool(ref and ref.available),
        why_not="" if ref is not None and ref.available else (ref.why if ref else "not loaded yet"),
        rules_using=len(used.get(str(row.id), [])),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _chat_models(session: SessionDep) -> list[ChatModelOption]:
    rows = (
        await session.execute(
            select(ModelDef.id, ModelDef.name, Provider.name)
            .join(Provider, Provider.id == ModelDef.provider_id)
            .where(
                ModelDef.kind == ModelKind.CHAT,
                ModelDef.is_active.is_(True),
                ModelDef.owner_id.is_(None),
                ModelDef.is_alias.is_(False),
                Provider.is_active.is_(True),
                Provider.owner_id.is_(None),
            )
            .order_by(ModelDef.name)
        )
    ).all()
    return [ChatModelOption(id=i, name=n, provider=p) for i, n, p in rows]


async def _check_classifier_model(session: SessionDep, model_id: uuid.UUID | None) -> None:
    if model_id is None:
        return
    if model_id not in {m.id for m in await _chat_models(session)}:
        raise UnprocessableError("The model must be an active chat model of a configured provider.")


async def _classifier_name_free(
    session: SessionDep, name: str, ignore: uuid.UUID | None = None
) -> None:
    if any(
        c.name.lower() == name.lower() and c.id != ignore
        for c in await service.classifier_rows(session)
    ):
        raise ConflictError(f"A classifier named '{name}' already exists.")


@router.get("/classifiers", response_model=ClassifierListResponse)
async def list_classifiers(
    admin: AdminUserDep, session: SessionDep, request: Request
) -> ClassifierListResponse:
    """The decision models: a model and a topic in plain words. Scaffolding, no model bundled."""
    used = await _used_by(session)
    state = state_of(request)
    return ClassifierListResponse(
        items=[_classifier_view(c, state, used) for c in await service.classifier_rows(session)],
        models=await _chat_models(session),
    )


@router.post("/classifiers", response_model=ClassifierResponse, status_code=status.HTTP_201_CREATED)
async def create_classifier(
    payload: ClassifierCreateRequest, admin: AdminUserDep, session: SessionDep, request: Request
) -> ClassifierResponse:
    name = payload.name.strip()
    if not name:
        raise UnprocessableError("Give the classifier a name.")
    await _classifier_name_free(session, name)
    await _check_classifier_model(session, payload.model_id)
    row = PrivacyClassifier(
        id=uuid.uuid4(), name=name, topic=payload.topic.strip(), model_id=payload.model_id
    )
    session.add(row)
    record_audit(
        session,
        admin,
        "privacy_classifier.create",
        "privacy_classifier",
        row.id,
        row.name,
        after={"name": row.name, "topic": row.topic, "model_id": str(row.model_id or "")},
    )
    await session.commit()
    await _reload(request)
    return _classifier_view(row, state_of(request), {})


@router.patch("/classifiers/{classifier_id}", response_model=ClassifierResponse)
async def update_classifier(
    classifier_id: uuid.UUID,
    payload: ClassifierUpdateRequest,
    admin: AdminUserDep,
    session: SessionDep,
    request: Request,
) -> ClassifierResponse:
    row = await session.get(PrivacyClassifier, classifier_id)
    if row is None:
        raise NotFoundError("No such classifier.")
    before = {"name": row.name, "topic": row.topic, "model_id": str(row.model_id or "")}
    fields = payload.model_fields_set
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise UnprocessableError("Give the classifier a name.")
        await _classifier_name_free(session, name, ignore=row.id)
        row.name = name
    if payload.topic is not None:
        row.topic = payload.topic.strip()
    if "model_id" in fields:
        await _check_classifier_model(session, payload.model_id)
        row.model_id = payload.model_id
    record_audit(
        session,
        admin,
        "privacy_classifier.update",
        "privacy_classifier",
        row.id,
        row.name,
        before=before,
        after={"name": row.name, "topic": row.topic, "model_id": str(row.model_id or "")},
    )
    await session.commit()
    await _reload(request)
    return _classifier_view(row, state_of(request), await _used_by(session))


@router.delete("/classifiers/{classifier_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_classifier(
    classifier_id: uuid.UUID, admin: AdminUserDep, session: SessionDep, request: Request
) -> None:
    row = await session.get(PrivacyClassifier, classifier_id)
    if row is None:
        raise NotFoundError("No such classifier.")
    using = (await _used_by(session)).get(str(row.id), [])
    if using:
        raise ConflictError(
            f"'{row.name}' is named by: {', '.join(using)}. Change those pipelines first."
        )
    record_audit(
        session,
        admin,
        "privacy_classifier.delete",
        "privacy_classifier",
        classifier_id,
        row.name,
        before={"name": row.name, "topic": row.topic},
    )
    await session.delete(row)
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
    scratch.classifier_backend = state_of(request).classifier_backend
    scratch.load(*await read_all(session))
    return scratch
