"""``/api/admin/privacy``: routing rules, detector profiles, and the test box (ADR 0113).

Rules are an ordered list per kind of request (read, embed; chat in phase 2).
Everything an administrator changes is audited like any other admin change, in
the same transaction as the change. After a write the worker that served it
reloads its rules at once; the others catch up on their next poll (ten seconds).

Saving never blocks on a *warning* (a rule that cannot fire, a profile that is
down): warnings come back with the rule and from ``/rules/check`` so the console
can say them while the rule is being written. Only an *error* (a value of the
wrong shape, an action the kind does not have) is a 422.
"""

from __future__ import annotations

import base64
import binascii
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Request, status
from sqlalchemy import select

from gateway import extraction
from gateway.admin_audit import record_audit
from gateway.deps import AdminUserDep, ControlHttpDep, SessionDep, SettingsDep
from gateway.errors import BadRequestError, GatewayError, NotFoundError
from gateway.models import DetectorProfile, PrivacyRule
from gateway.pagination import MAX_LIMIT, Page
from gateway.privacy import attribution
from gateway.privacy import rules as rulelib
from gateway.privacy.evaluate import Evaluation, Facts
from gateway.privacy.schemas import (
    ActionView,
    ConditionView,
    FiredRule,
    IssueView,
    ProfileCreateRequest,
    ProfileResponse,
    ProfileUpdateRequest,
    ReorderRequest,
    RuleCheckRequest,
    RuleCheckResponse,
    RuleCreateRequest,
    RuleResponse,
    RuleTestRequest,
    RuleTestResponse,
    RuleTestStep,
    RuleUpdateRequest,
)
from gateway.privacy.state import PrivacyState, engine_available, rule_from_row, state_of

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


# -- views -------------------------------------------------------------------------

RULE_FIELDS = ("name", "kind", "enabled", "observe_only", "position", "conditions", "action")


def _rule_view(row: PrivacyRule) -> dict[str, Any]:
    return {
        "name": row.name,
        "kind": row.kind,
        "enabled": row.enabled,
        "observe_only": row.observe_only,
        "position": row.position,
        "conditions": [dict(c) for c in row.conditions or []],
        "action": dict(row.action or {}),
    }


def _profile_view(row: DetectorProfile) -> dict[str, Any]:
    return {
        "name": row.name,
        "description": row.description,
        "engine": row.engine,
        "labels": list(row.labels or []),
        "fallback": row.fallback,
        "fallback_profile_id": str(row.fallback_profile_id) if row.fallback_profile_id else None,
    }


async def _rules(session: SessionDep) -> list[PrivacyRule]:
    return list(
        (
            await session.execute(
                select(PrivacyRule).order_by(PrivacyRule.kind, PrivacyRule.position)
            )
        )
        .scalars()
        .all()
    )


async def _profiles(session: SessionDep) -> list[DetectorProfile]:
    return list(
        (await session.execute(select(DetectorProfile).order_by(DetectorProfile.name)))
        .scalars()
        .all()
    )


def _check_context(
    profiles: list[DetectorProfile], *, has_local_embedding: bool
) -> rulelib.CheckContext:
    return rulelib.CheckContext(
        profiles={
            str(p.id): rulelib.ProfileInfo(
                id=p.id,
                name=p.name,
                available=engine_available(p.engine),
                fallback=p.fallback,
                can_decide=engine_available(p.engine),
            )
            for p in profiles
        },
        has_local_embedding=has_local_embedding,
    )


async def _local_embedding_exists(request: Request) -> bool:
    return bool(state_of(request).local_embedding_models)


def _issues(found: list[rulelib.Issue]) -> list[IssueView]:
    return [
        IssueView(code=i.code, message=i.message, rule_id=i.rule_id, rule_name=i.rule_name)
        for i in found
    ]


def _response(row: PrivacyRule, issues: list[rulelib.Issue]) -> RuleResponse:
    return RuleResponse(
        id=row.id,
        kind=row.kind,
        name=row.name,
        enabled=row.enabled,
        observe_only=row.observe_only,
        position=row.position,
        conditions=[ConditionView.model_validate(c) for c in row.conditions or []],
        action=ActionView.model_validate(row.action or {}),
        issues=_issues(issues),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _validated(
    kind: str,
    name: str,
    conditions: list[dict[str, Any]],
    action: dict[str, Any],
    profiles: list[DetectorProfile],
) -> tuple[str, list[rulelib.Condition], dict[str, Any]]:
    try:
        return rulelib.validate(
            kind, name, conditions, action, profile_ids=[str(p.id) for p in profiles]
        )
    except rulelib.RuleError as exc:
        raise UnprocessableError(str(exc)) from exc


async def _renumber(session: SessionDep, kind: str, ordered: list[PrivacyRule]) -> None:
    for index, row in enumerate(ordered):
        if row.position != index:
            row.position = index
    await session.flush()
    del kind


async def _reload(request: Request) -> None:
    """Make this worker see the change now; the others poll."""
    await state_of(request).refresh()


async def _all_issues(
    session: SessionDep, request: Request
) -> tuple[list[PrivacyRule], dict[uuid.UUID, list[rulelib.Issue]]]:
    rows = await _rules(session)
    profiles = await _profiles(session)
    context = _check_context(profiles, has_local_embedding=await _local_embedding_exists(request))
    return rows, rulelib.check([rule_from_row(r) for r in rows], context)


# -- rules -------------------------------------------------------------------------


@router.get("/rules", response_model=Page[RuleResponse])
async def list_rules(
    admin: AdminUserDep, session: SessionDep, request: Request, kind: str | None = None
) -> Page[RuleResponse]:
    """Every rule, grouped by kind and in order, each with its warnings."""
    rows, issues = await _all_issues(session, request)
    shown = [r for r in rows if kind is None or r.kind == kind]
    items = [_response(r, issues.get(r.id, [])) for r in shown]
    return Page(items=items, total=len(items), limit=max(1, min(MAX_LIMIT, len(items))), offset=0)


@router.post("/rules", response_model=RuleResponse, status_code=status.HTTP_201_CREATED)
async def create_rule(
    payload: RuleCreateRequest, admin: AdminUserDep, session: SessionDep, request: Request
) -> RuleResponse:
    profiles = await _profiles(session)
    name, conditions, action = _validated(
        payload.kind,
        payload.name,
        [c.model_dump() for c in payload.conditions],
        payload.action.clean(),
        profiles,
    )
    siblings = [r for r in await _rules(session) if r.kind == payload.kind]
    if any(r.name.lower() == name.lower() for r in siblings):
        raise ConflictError(f"A {payload.kind} rule named '{name}' already exists.")
    row = PrivacyRule(
        id=uuid.uuid4(),
        kind=payload.kind,
        name=name,
        enabled=payload.enabled,
        observe_only=payload.observe_only,
        position=len(siblings),
        conditions=[rulelib.condition_to_json(c) for c in conditions],
        action=action,
    )
    session.add(row)
    ordered = list(siblings)
    index = min(payload.position, len(siblings)) if payload.position is not None else len(siblings)
    ordered.insert(index, row)
    await _renumber(session, payload.kind, ordered)
    record_audit(
        session,
        admin,
        "privacy_rule.create",
        "privacy_rule",
        row.id,
        row.name,
        after=_rule_view(row),
    )
    await session.commit()
    await _reload(request)
    _, issues = await _all_issues(session, request)
    return _response(row, issues.get(row.id, []))


async def _load_rule(session: SessionDep, rule_id: uuid.UUID) -> PrivacyRule:
    row = await session.get(PrivacyRule, rule_id)
    if row is None:
        raise NotFoundError("No such rule.")
    return row


@router.patch("/rules/{rule_id}", response_model=RuleResponse)
async def update_rule(
    rule_id: uuid.UUID,
    payload: RuleUpdateRequest,
    admin: AdminUserDep,
    session: SessionDep,
    request: Request,
) -> RuleResponse:
    row = await _load_rule(session, rule_id)
    before = _rule_view(row)
    profiles = await _profiles(session)
    fields = payload.model_fields_set
    name, conditions, action = _validated(
        row.kind,
        payload.name if "name" in fields and payload.name else row.name,
        (
            [c.model_dump() for c in payload.conditions]
            if "conditions" in fields and payload.conditions is not None
            else [dict(c) for c in row.conditions or []]
        ),
        (
            payload.action.clean()
            if "action" in fields and payload.action is not None
            else dict(row.action or {})
        ),
        profiles,
    )
    if name.lower() != row.name.lower():
        taken = [r for r in await _rules(session) if r.kind == row.kind and r.id != row.id]
        if any(r.name.lower() == name.lower() for r in taken):
            raise ConflictError(f"A {row.kind} rule named '{name}' already exists.")
    row.name = name
    row.conditions = [rulelib.condition_to_json(c) for c in conditions]
    row.action = action
    if payload.enabled is not None:
        row.enabled = payload.enabled
    if payload.observe_only is not None:
        row.observe_only = payload.observe_only
    record_audit(
        session,
        admin,
        "privacy_rule.update",
        "privacy_rule",
        row.id,
        row.name,
        before=before,
        after=_rule_view(row),
    )
    await session.commit()
    await _reload(request)
    _, issues = await _all_issues(session, request)
    return _response(row, issues.get(row.id, []))


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(
    rule_id: uuid.UUID, admin: AdminUserDep, session: SessionDep, request: Request
) -> None:
    row = await _load_rule(session, rule_id)
    before = _rule_view(row)
    kind, name = row.kind, row.name
    await session.delete(row)
    await session.flush()
    rest = [r for r in await _rules(session) if r.kind == kind]
    await _renumber(session, kind, rest)
    record_audit(
        session, admin, "privacy_rule.delete", "privacy_rule", rule_id, name, before=before
    )
    await session.commit()
    await _reload(request)


@router.post("/rules/reorder", response_model=Page[RuleResponse])
async def reorder_rules(
    payload: ReorderRequest, admin: AdminUserDep, session: SessionDep, request: Request
) -> Page[RuleResponse]:
    """Put a kind's rules in a new order. Must name every rule of the kind once."""
    rows = [r for r in await _rules(session) if r.kind == payload.kind]
    by_id = {r.id: r for r in rows}
    if sorted(payload.order, key=str) != sorted(by_id, key=str):
        raise UnprocessableError("The order must list every rule of this kind exactly once.")
    before = [r.name for r in rows]
    ordered = [by_id[i] for i in payload.order]
    await _renumber(session, payload.kind, ordered)
    after = [r.name for r in ordered]
    record_audit(
        session,
        admin,
        "privacy_rule.reorder",
        "privacy_rule",
        None,
        f"{payload.kind} rules",
        before={"order": before},
        after={"order": after},
    )
    await session.commit()
    await _reload(request)
    return await list_rules(admin, session, request, kind=None)


@router.post("/rules/check", response_model=RuleCheckResponse)
async def check_rule(
    payload: RuleCheckRequest, admin: AdminUserDep, session: SessionDep, request: Request
) -> RuleCheckResponse:
    """The warnings a draft would get if saved now, for the editor to show as it is written."""
    profiles = await _profiles(session)
    try:
        name, conditions, action = rulelib.validate(
            payload.kind,
            payload.name,
            [c.model_dump() for c in payload.conditions],
            payload.action.clean(),
            profile_ids=[str(p.id) for p in profiles],
        )
    except rulelib.RuleError as exc:
        return RuleCheckResponse(error=str(exc), issues=[], soft_cap=rulelib.SOFT_CAP)
    rows = await _rules(session)
    same = [r for r in rows if r.kind == payload.kind]
    others = [rule_from_row(r) for r in rows if r.id != payload.rule_id]
    editing = next((r for r in rows if r.id == payload.rule_id), None)
    draft_id = payload.rule_id or uuid.uuid4()
    draft = rulelib.Rule(
        id=draft_id,
        kind=payload.kind,
        name=name,
        enabled=payload.enabled,
        observe_only=payload.observe_only,
        position=editing.position if editing is not None else len(same),
        conditions=tuple(conditions),
        action=action,
    )
    context = _check_context(profiles, has_local_embedding=await _local_embedding_exists(request))
    issues = rulelib.check([*others, draft], context)
    return RuleCheckResponse(issues=_issues(issues.get(draft_id, [])), soft_cap=rulelib.SOFT_CAP)


# -- profiles ----------------------------------------------------------------------


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
        status=status_word,
        status_detail=detail,
        decides=available,
        cost_hint="instant" if available else "slow; arrives with model profiles",
        rules_using=using.get(str(row.id), 0),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _presidio_status(request: Request) -> tuple[str, str]:
    redactor = getattr(request.app.state, "redactor", None)
    name = getattr(redactor, "name", "noop")
    if name == "noop":
        return "available", "Redaction is off; verdicts still use the quick check."
    return "available", "The detection service is the one in use for redaction."


def _usage(rows: list[PrivacyRule]) -> dict[str, int]:
    count: dict[str, int] = {}
    for row in rows:
        for c in row.conditions or []:
            if c.get("decide") and c["decide"] != rulelib.PATTERNS:
                count[str(c["decide"])] = count.get(str(c["decide"]), 0) + 1
    return count


@router.get("/profiles", response_model=Page[ProfileResponse])
async def list_profiles(
    admin: AdminUserDep, session: SessionDep, request: Request
) -> Page[ProfileResponse]:
    profiles = await _profiles(session)
    names = {p.id: p.name for p in profiles}
    using = _usage(await _rules(session))
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
    existing = await _profiles(session)
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
    return await _profile_response(request, row, names, _usage(await _rules(session)))


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
        if any(p.name.lower() == name.lower() and p.id != row.id for p in await _profiles(session)):
            raise ConflictError(f"A profile named '{name}' already exists.")
        row.name = name
    if payload.description is not None:
        row.description = payload.description.strip()
    if payload.labels is not None:
        row.labels = [label.strip().upper() for label in payload.labels if label.strip()]
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
    profiles = await _profiles(session)
    return await _profile_response(
        request, row, {p.id: p.name for p in profiles}, _usage(await _rules(session))
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
    if _usage(await _rules(session)).get(str(row.id)):
        raise ConflictError("A rule still decides with this profile. Change those rules first.")
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


# -- the test box ------------------------------------------------------------------


def _outcome(ev: Evaluation) -> tuple[str, str]:
    if ev.pending is not None:
        return (
            "pending",
            "A rule is waiting to learn whether the file is a scan. Give a file to read, or say "
            "whether it is one.",
        )
    fired = ev.fired
    if fired is None:
        return "default", "No rule applies: the request is handled as it is today."
    action = fired.action_type
    if action == "refuse":
        return "refuse", f"Refused with a 403 naming '{fired.name}'."
    if action == "reader":
        return "reader", f"Read with {fired.action.get('reader')}, whatever model was named."
    if action == "local":
        return "local", "Embedded on a local model; refused if none is available."
    return action, f"'{fired.name}' applies."


@router.post("/test", response_model=RuleTestResponse)
async def test_rules(
    payload: RuleTestRequest,
    admin: AdminUserDep,
    session: SessionDep,
    request: Request,
    settings: SettingsDep,
    control_http: ControlHttpDep,
) -> RuleTestResponse:
    """Which rule would fire for these facts, why the earlier ones did not, and the outcome.

    Reads the rules as saved. The text and the file are judged in memory and never
    stored or logged; the answer carries labels and names, not content. Rate limited.
    """
    limiter = getattr(request.app.state, "redaction_test_limiter", None)
    if limiter is not None and not limiter.allow(("privacy-test", str(admin.id))):
        raise BadRequestError("Slow down: the test box is limited to 30 runs a minute.")
    privacy = state_of(request)
    rows = await _rules(session)
    profiles = await _profiles(session)
    # The saved list, not this worker's last poll: an administrator who just saved
    # must see it applied.
    scratch = PrivacyState()
    scratch.cache = privacy.cache
    scratch.load(rows, profiles, list(privacy.local_embedding_models), [])

    filename = attribution.clean_filename(payload.filename)
    mime = payload.mime
    is_scan = payload.is_scan
    extractor: str | None = None
    fallback_reason: str | None = None
    if payload.file_base64:
        try:
            data = base64.b64decode(payload.file_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise BadRequestError("The file is not valid base64.") from exc
        try:
            read = await extraction.extract_document(
                control_http,
                settings.extractor.endpoint,
                data,
                media_type=mime,
                filename=filename,
                timeout_seconds=settings.extractor.timeout_seconds,
            )
        except Exception as exc:  # the local extractor may simply not be there
            raise BadRequestError(f"The file could not be read here: {exc}") from exc
        extractor, fallback_reason = read.extractor or None, read.fallback_reason or None
        is_scan = attribution.is_scan_from(read.extractor, read.kind.value, bool(read.page_images))
    facts = Facts(
        kind=payload.kind,
        user_email=(payload.user or None),
        groups=tuple(payload.groups),
        filename=filename,
        mime=mime,
        filetype=rulelib.filetype_of(mime, filename),
        is_scan=is_scan,
        target_model=payload.target_model,
        target_provider=payload.target_provider,
        target_local=payload.target_local,
        text=payload.text if scratch.uses_verdicts(payload.kind) else None,
    )
    ev = await scratch.evaluator.evaluate(scratch.rules_of(payload.kind), facts)
    outcome, text = _outcome(ev)
    verdicts = {k: v for k, v in ev.verdicts.items() if not k.startswith("_")}
    shown_facts: dict[str, Any] = {
        "groups": list(facts.groups),
        "user": facts.user_email,
        "filename": facts.filename,
        "mime": facts.mime,
        "filetype": facts.filetype,
        "is_scan": facts.is_scan,
        "target_model": facts.target_model,
        "target_provider": facts.target_provider,
        "target_local": facts.target_local,
        "text_checked": facts.text is not None,
    }

    def fired_of(rule: rulelib.Rule) -> FiredRule:
        return FiredRule(
            id=rule.id, name=rule.name, action=ActionView.model_validate(dict(rule.action))
        )

    return RuleTestResponse(
        kind=payload.kind,
        outcome=outcome,
        outcome_text=text,
        fired=fired_of(ev.fired) if ev.fired else None,
        observed=[fired_of(r) for r in ev.observed],
        steps=[
            RuleTestStep(
                rule_id=s.rule.id,
                name=s.rule.name,
                position=s.rule.position,
                result=s.result,
                reasons=s.reasons,
                degraded=s.degraded,
            )
            for s in ev.steps
        ],
        facts=shown_facts,
        verdicts=verdicts,
        fallbacks=ev.fallbacks,
        degraded=ev.degraded,
        needs=ev.pending,
        extractor=extractor,
        fallback_reason=fallback_reason,
    )
