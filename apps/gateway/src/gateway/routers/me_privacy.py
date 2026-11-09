"""``/api/me/privacy``: a user's own pipelines and policies (ADR 0113, section 0).

A user may add protection to their own requests and never remove any. The rule
is structural, and every part of it is enforced here, on the server:

* **Who is fixed.** A user's pipeline has its owner as the only who, whatever the
  body says (``privacy.pipelines.validate_scope``); the request-time scope check
  holds it again.
* **Actions only tighten.** Read with a reader, refuse, redact with a policy, an
  engine (``rules.clean_action(user=True)``). The combination takes the highest
  level on the ladder and folds policies by strictest mode, so no pipeline of a
  user's can lower what an administrator's did. A pipeline row that loosens is
  ignored when read (``pipeline_from_row``).
* **Readers are the user's grants.** "Read with" names only a reader the user may
  use; the request path resolves it again and fails closed when it cannot.
* **Policies are the administrators' (the ones marked shared) or the user's own.**
  Never another user's, and "classified as" is the administrators' alone. An own
  policy uses custom patterns and only the entity types an administrator made
  available.

The test box is locked to the caller. Its text and file are never logged.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Request, status
from sqlalchemy import func, or_, select

from gateway.access import accessible_models
from gateway.deps import ControlHttpDep, ManagementUserDep, SessionDep, SettingsDep
from gateway.errors import BadRequestError, GatewayError, NotFoundError, TooManyRequestsError
from gateway.models import (
    ApiKey,
    ModelDef,
    PrivacyPipeline,
    Provider,
    User,
    UserPolicy,
)
from gateway.pagination import MAX_LIMIT, Page
from gateway.privacy import rules as rulelib
from gateway.privacy import service
from gateway.privacy.engines import (
    NO_NER_MESSAGE,
    effective_engines,
    has_ner,
    ner_type_names,
    usable,
)
from gateway.privacy.pipelines import Pipeline, pipeline_from_row, validate_pipeline
from gateway.privacy.schemas import (
    DryRunRequest,
    DryRunResponse,
    EnginesResponse,
    IssueView,
    ModelOption,
    NamedId,
    OptionsResponse,
    PipelineCheckRequest,
    PipelineCheckResponse,
    PipelineCreateRequest,
    PipelineResponse,
    PipelineUpdateRequest,
    ReaderOption,
    UserEntryView,
    UserPolicyRequest,
    UserPolicyResponse,
)
from gateway.privacy.state import state_of

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/me/privacy", tags=["self-service"])

#: How many pipelines and policies one user may keep. They are held in memory on
#: every worker (the request path stays query-free), so they are bounded.
MAX_PIPELINES_PER_USER = 25
MAX_POLICIES_PER_USER = 10
TEST_CALLS_PER_MINUTE = 30


class ConflictError(GatewayError):
    status_code = status.HTTP_409_CONFLICT
    error_type = "invalid_request_error"
    code = "conflict"


class UnprocessableError(GatewayError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_type = "invalid_request_error"
    code = "invalid_rule"


async def _reload(request: Request) -> None:
    await state_of(request).refresh()


async def _mine(session: SessionDep, user: User, pipeline_id: uuid.UUID) -> PrivacyPipeline:
    row = await session.get(PrivacyPipeline, pipeline_id)
    # Another user's pipeline answers exactly as one that does not exist.
    if row is None or row.owner_id != user.id:
        raise NotFoundError("No such pipeline.")
    return row


async def _check_targets(session: SessionDep, user: User, target: dict[str, Any]) -> None:
    """The targets named must be ones this user can see: deployment's or their own."""
    for column, owner, ids, noun in (
        (Provider.id, Provider.owner_id, target["providers"], "providers"),
        (ModelDef.id, ModelDef.owner_id, target["models"], "models"),
    ):
        if not ids:
            continue
        wanted = {uuid.UUID(i) for i in ids}
        found = set(
            (
                await session.execute(
                    select(column).where(column.in_(wanted), or_(owner.is_(None), owner == user.id))
                )
            )
            .scalars()
            .all()
        )
        if found != wanted:
            raise UnprocessableError(f"One of the {noun} named does not exist.")


async def _clean(
    session: SessionDep,
    user: User,
    *,
    kind: str,
    name: str,
    target: Any,
    rules: list[dict[str, Any]],
    otherwise: dict[str, Any] | None,
) -> dict[str, Any]:
    context = await service.validation_context(session, owner=user)
    rules, otherwise = service.normalise_readers(rules, otherwise, context.builtin_aliases)
    try:
        clean = validate_pipeline(
            kind,
            name,
            None,
            target,
            rules,
            otherwise,
            owner_id=user.id,
            finds_policy_ids=context.finds_policy_ids,
            classifier_ids=context.classifier_ids,
            policy_ids=context.policy_ids,
            readers=context.readers,
        )
    except rulelib.ContentOnReadError as exc:
        raise BadRequestError(str(exc)) from exc
    except rulelib.RuleError as exc:
        raise UnprocessableError(str(exc)) from exc
    await _check_targets(session, user, clean["target"])
    return clean


async def _name_free(
    session: SessionDep, user: User, kind: str, name: str, ignore: uuid.UUID | None = None
) -> None:
    rows = await service.pipeline_rows(session, owner=user.id)
    if any(r.kind == kind and r.name.lower() == name.lower() and r.id != ignore for r in rows):
        raise ConflictError(f"You already have a {kind} pipeline named '{name}'.")


# -- options -----------------------------------------------------------------------


@router.get("/options", response_model=OptionsResponse)
async def options(
    user: ManagementUserDep,
    session: SessionDep,
    settings: SettingsDep,
    control_http: ControlHttpDep,
) -> OptionsResponse:
    """What this user may pick: their readers, policies, targets and types."""
    group = service.request_group_id(user)
    models = list(
        (
            await session.execute(
                accessible_models(user_id=user.id, group_ids=[group] if group else []).order_by(
                    ModelDef.name
                )
            )
        )
        .unique()
        .scalars()
        .all()
    )
    providers = {
        m.provider_id: m.provider.name for m in models if m.provider_id and m.provider is not None
    }
    readers = await service.granted_readers(session, user)
    admin_policies = [
        p for p in await service.policy_rows(session) if p.is_active and p.shared_with_users
    ]
    mine = await service.user_policy_rows(session, user.id)
    settings_row = await service.settings_row(session)
    builtin = any(readers.values())
    report = usable(await effective_engines(session, control_http, settings))
    ner = has_ner(report)
    entity_types = settings_row.user_entity_types
    if ner is False and entity_types is not None:
        # No model, so a type only a model finds could never be served: not offered.
        blocked = ner_type_names(report)
        entity_types = [t for t in entity_types if t.upper() not in blocked]
    return OptionsResponse(
        users=[],
        groups=[],
        keys=[],
        providers=[NamedId(id=i, name=n) for i, n in sorted(providers.items(), key=lambda x: x[1])],
        models=[
            ModelOption(
                id=m.id,
                name=m.name,
                provider_id=m.provider_id,
                provider=m.provider.name if m.provider is not None else None,
                kind=m.kind.value,
            )
            for m in models
            if not m.is_alias
        ],
        readers=(
            [ReaderOption(id=rulelib.BUILTIN_READER, name="Built-in extractor", builtin=True)]
            if builtin
            else []
        )
        + [ReaderOption(id=n, name=n, builtin=False) for n, b in sorted(readers.items()) if not b],
        policies=[NamedId(id=p.id, name=p.name) for p in admin_policies],
        own_policies=[NamedId(id=p.id, name=p.name) for p in mine],
        classifiers=[],
        entity_types=entity_types,
        ner_available=ner,
        ner_note=NO_NER_MESSAGE if ner is False else None,
        engines=list(rulelib.ENGINES),
    )


# -- my pipelines --------------------------------------------------------------------


@router.get("/pipelines", response_model=Page[PipelineResponse])
async def list_my_pipelines(
    user: ManagementUserDep, session: SessionDep, request: Request
) -> Page[PipelineResponse]:
    items = await service.views_for(
        session, await service.pipeline_rows(session, owner=user.id), state_of(request)
    )
    return Page(items=items, total=len(items), limit=max(1, min(MAX_LIMIT, len(items))), offset=0)


@router.get("/pipelines/{pipeline_id}", response_model=PipelineResponse)
async def get_my_pipeline(
    pipeline_id: uuid.UUID, user: ManagementUserDep, session: SessionDep, request: Request
) -> PipelineResponse:
    mine = await _mine(session, user, pipeline_id)
    return (await service.views_for(session, [mine], state_of(request)))[0]


@router.post("/pipelines", response_model=PipelineResponse, status_code=status.HTTP_201_CREATED)
async def create_my_pipeline(
    payload: PipelineCreateRequest, user: ManagementUserDep, session: SessionDep, request: Request
) -> PipelineResponse:
    mine = await service.pipeline_rows(session, owner=user.id)
    if len(mine) >= MAX_PIPELINES_PER_USER:
        raise ConflictError(f"You can keep at most {MAX_PIPELINES_PER_USER} pipelines.")
    clean = await _clean(
        session,
        user,
        kind=payload.kind,
        name=payload.name,
        target=payload.target.model_dump(mode="json"),
        rules=[r.clean() for r in payload.rules],
        otherwise=payload.otherwise.clean() if payload.otherwise else None,
    )
    await _name_free(session, user, payload.kind, clean["name"])
    row = PrivacyPipeline(
        id=uuid.uuid4(),
        owner_id=user.id,
        kind=payload.kind,
        name=clean["name"],
        enabled=payload.enabled,
        position=len([r for r in mine if r.kind == payload.kind]),
        who=clean["who"],
        target=clean["target"],
        rules=clean["rules"],
        otherwise=clean["otherwise"],
        created_by=user.id,
    )
    session.add(row)
    await session.commit()
    await _reload(request)
    return (await service.views_for(session, [row], state_of(request)))[0]


@router.patch("/pipelines/{pipeline_id}", response_model=PipelineResponse)
async def update_my_pipeline(
    pipeline_id: uuid.UUID,
    payload: PipelineUpdateRequest,
    user: ManagementUserDep,
    session: SessionDep,
    request: Request,
) -> PipelineResponse:
    row = await _mine(session, user, pipeline_id)
    clean = await _clean(
        session,
        user,
        kind=row.kind,
        name=payload.name or row.name,
        target=payload.target.model_dump(mode="json") if payload.target is not None else row.target,
        rules=(
            [r.clean() for r in payload.rules]
            if payload.rules is not None
            else [dict(r) for r in row.rules or []]
        ),
        otherwise=(
            payload.otherwise.clean()
            if payload.otherwise is not None
            else dict(row.otherwise or {})
        ),
    )
    await _name_free(session, user, row.kind, clean["name"], ignore=row.id)
    row.name = clean["name"]
    row.who = clean["who"]
    row.target = clean["target"]
    row.rules = clean["rules"]
    row.otherwise = clean["otherwise"]
    if payload.enabled is not None:
        row.enabled = payload.enabled
    await session.commit()
    await _reload(request)
    return (await service.views_for(session, [row], state_of(request)))[0]


@router.delete("/pipelines/{pipeline_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_pipeline(
    pipeline_id: uuid.UUID, user: ManagementUserDep, session: SessionDep, request: Request
) -> None:
    row = await _mine(session, user, pipeline_id)
    await session.delete(row)
    await session.commit()
    await _reload(request)


@router.post("/pipelines/check", response_model=PipelineCheckResponse)
async def check_my_pipeline(
    payload: PipelineCheckRequest, user: ManagementUserDep, session: SessionDep, request: Request
) -> PipelineCheckResponse:
    try:
        clean = await _clean(
            session,
            user,
            kind=payload.kind,
            name=payload.name,
            target=payload.target.model_dump(mode="json"),
            rules=[r.clean() for r in payload.rules],
            otherwise=payload.otherwise.clean() if payload.otherwise else None,
        )
    except UnprocessableError as exc:
        return PipelineCheckResponse(error=exc.message, issues=[], soft_cap=rulelib.SOFT_CAP)
    row = PrivacyPipeline(
        id=uuid.uuid4(),
        owner_id=user.id,
        kind=payload.kind,
        name=clean["name"],
        enabled=payload.enabled,
        position=0,
        who=clean["who"],
        target=clean["target"],
        rules=clean["rules"],
        otherwise=clean["otherwise"],
    )
    draft: Pipeline = pipeline_from_row(row)
    found = service.issues_of(draft, state_of(request))
    issues: list[IssueView] = []
    for rule_issues in found.values():
        issues.extend(service.issue_views(rule_issues))
    return PipelineCheckResponse(issues=issues, soft_cap=rulelib.SOFT_CAP)


# -- my policies -----------------------------------------------------------------------


async def _policy_used_by(session: SessionDep, user: User) -> dict[str, list[str]]:
    used: dict[str, list[str]] = {}
    for row in await service.pipeline_rows(session, owner=user.id):
        actions = [dict(r.get("action") or {}) for r in row.rules or []] + [
            dict(row.otherwise or {})
        ]
        for pid in {str(a["redact_with"]) for a in actions if a.get("redact_with")}:
            used.setdefault(pid, []).append(row.name)
    return used


def _policy_response(row: UserPolicy, used: dict[str, list[str]]) -> UserPolicyResponse:
    return UserPolicyResponse(
        id=row.id,
        name=row.name,
        entries=[UserEntryView.model_validate(e) for e in row.rules or []],
        used_by=used.get(str(row.id), []),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _own_policy(session: SessionDep, user: User, policy_id: uuid.UUID) -> UserPolicy:
    row = await session.get(UserPolicy, policy_id)
    if row is None or row.user_id != user.id:
        raise NotFoundError("No such policy.")
    return row


async def _policy_fields(
    session: SessionDep,
    user: User,
    payload: UserPolicyRequest,
    ignore: uuid.UUID | None,
    report: EnginesResponse,
) -> tuple[str, list[dict[str, Any]]]:
    name = payload.name.strip()
    if not name:
        raise UnprocessableError("Give the policy a name.")
    taken = await service.user_policy_rows(session, user.id)
    if any(p.name.lower() == name.lower() and p.id != ignore for p in taken):
        raise ConflictError(f"You already have a policy named '{name}'.")
    settings_row = await service.settings_row(session)
    try:
        entries = service.clean_entries(
            payload.entries,
            allowed_types=settings_row.user_entity_types,
            no_ner_types=ner_type_names(report) if has_ner(report) is False else None,
        )
    except ValueError as exc:
        raise UnprocessableError(_message_of(exc)) from exc
    return name, entries


def _message_of(exc: ValueError) -> str:
    """A validation error as one sentence: pydantic's carries RE2's own message."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        found = errors()
        if found:
            return str(found[0].get("msg", exc)).removeprefix("Value error, ")
    return str(exc)


@router.get("/policies", response_model=Page[UserPolicyResponse])
async def list_my_policies(
    user: ManagementUserDep, session: SessionDep
) -> Page[UserPolicyResponse]:
    used = await _policy_used_by(session, user)
    items = [_policy_response(p, used) for p in await service.user_policy_rows(session, user.id)]
    return Page(items=items, total=len(items), limit=max(1, len(items)), offset=0)


@router.post("/policies", response_model=UserPolicyResponse, status_code=status.HTTP_201_CREATED)
async def create_my_policy(
    payload: UserPolicyRequest,
    user: ManagementUserDep,
    session: SessionDep,
    request: Request,
    settings: SettingsDep,
    control_http: ControlHttpDep,
) -> UserPolicyResponse:
    count = (
        await session.execute(
            select(func.count()).select_from(UserPolicy).where(UserPolicy.user_id == user.id)
        )
    ).scalar_one()
    if count >= MAX_POLICIES_PER_USER:
        raise ConflictError(f"You can keep at most {MAX_POLICIES_PER_USER} policies.")
    name, entries = await _policy_fields(
        session,
        user,
        payload,
        None,
        usable(await effective_engines(session, control_http, settings)),
    )
    row = UserPolicy(id=uuid.uuid4(), user_id=user.id, name=name, rules=entries)
    session.add(row)
    await session.commit()
    await _reload(request)
    return _policy_response(row, {})


@router.put("/policies/{policy_id}", response_model=UserPolicyResponse)
async def update_my_policy(
    policy_id: uuid.UUID,
    payload: UserPolicyRequest,
    user: ManagementUserDep,
    session: SessionDep,
    request: Request,
    settings: SettingsDep,
    control_http: ControlHttpDep,
) -> UserPolicyResponse:
    row = await _own_policy(session, user, policy_id)
    name, entries = await _policy_fields(
        session,
        user,
        payload,
        row.id,
        usable(await effective_engines(session, control_http, settings)),
    )
    row.name = name
    row.rules = entries
    await session.commit()
    await _reload(request)
    return _policy_response(row, await _policy_used_by(session, user))


@router.delete("/policies/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_policy(
    policy_id: uuid.UUID, user: ManagementUserDep, session: SessionDep, request: Request
) -> None:
    row = await _own_policy(session, user, policy_id)
    used = (await _policy_used_by(session, user)).get(str(row.id), [])
    if used:
        raise ConflictError(f"'{row.name}' is applied by: {', '.join(used)}. Change those first.")
    await session.delete(row)
    await session.commit()
    await _reload(request)


# -- what the administrator already set -------------------------------------------------


@router.get("/in-force", response_model=Page[PipelineResponse])
async def in_force(
    user: ManagementUserDep, session: SessionDep, request: Request
) -> Page[PipelineResponse]:
    """The administrators' pipelines that cover this user, read-only.

    Every pipeline whose who names everyone, this user, their billing group or
    one of their keys; where a request goes (its target) is shown, not judged
    here. Rule names and what each does are shown; another user's pipelines
    never are, and a policy is shown by name.
    """
    group = service.request_group_id(user)
    keys = {
        str(k)
        for k in (await session.execute(select(ApiKey.id).where(ApiKey.user_id == user.id)))
        .scalars()
        .all()
    }
    rows = []
    for row in await service.pipeline_rows(session, owner=None):
        who = row.who or {}
        mode, ids = who.get("mode", "all"), {str(i) for i in who.get("ids") or []}
        if (
            mode == "all"
            or (mode == "users" and str(user.id) in ids)
            or (mode == "groups" and group is not None and str(group) in ids)
            or (mode == "keys" and ids & keys)
        ) and row.enabled:
            rows.append(row)
    items = await service.views_for(session, rows, state_of(request))
    # Only the caller's own matches are shown: who else a scope names (other users,
    # other groups, other people's keys) is not theirs to read, neither the
    # ids nor the names behind them.
    mine = {"users": {str(user.id)}, "groups": {str(group)} if group else set(), "keys": keys}
    for item in items:
        if item.who.mode in mine:
            keep = [i for i in item.who.ids if str(i) in mine[item.who.mode]]
            for ident in item.who.ids:
                if ident not in keep:
                    item.labels.pop(str(ident), None)
            item.who.ids = keep
    return Page(items=items, total=len(items), limit=max(1, min(MAX_LIMIT, len(items))), offset=0)


# -- the test box (self only) -------------------------------------------------------------


@router.post("/test", response_model=DryRunResponse)
async def test_my_pipelines(
    payload: DryRunRequest,
    user: ManagementUserDep,
    session: SessionDep,
    request: Request,
    settings: SettingsDep,
    control_http: ControlHttpDep,
) -> DryRunResponse:
    """The pipelines in scope for *this user* and the combined outcome. Never logged.

    The caller is always the user: ``user_id`` in the body is ignored. Nothing here
    is stored, and the text and file are judged in memory.
    """
    limiter = request.app.state.redaction_test_limiter
    if not limiter.allow(("redaction-test", str(user.id))):
        raise TooManyRequestsError(
            f"You can try at most {TEST_CALLS_PER_MINUTE} times a minute. "
            "Wait a moment and try again."
        )
    from gateway.routers.admin_privacy import scratch_state

    state = await scratch_state(session, request)
    return await service.dry_run(
        session,
        state,
        payload.model_copy(update={"user_id": user.id}),
        user=user,
        settings=settings,
        control_http=control_http,
        redactor=getattr(request.app.state, "redactor", None),
        viewer_only=True,
    )
