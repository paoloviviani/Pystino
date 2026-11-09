"""What the admin and user privacy APIs share (ADR 0113, section 0).

Views and labels, the validation context, saving a pipeline, the options an
editor may pick from, the one-line summaries clients show, and the dry run (the
Test tab): the real combination path over facts the caller supplies, with no
side effects.

A user's pipeline and an administrator's are the same object. Everything that
makes a user's safe is enforced here, server-side, and not in the console:

* its who is the user, whatever the request says;
* its actions only tighten (``rules.clean_action(user=True)``);
* it may apply an administrator's policy or the user's **own**, never another
  user's;
* it may name only readers the user is granted;
* an own policy uses custom patterns and only the entity types the administrator
  has made available;
* it may name an administrator's policy only if it is marked shared, in "apply" and in
  "finds at least"; "classified as" is the administrators' alone (it sends text to
  a provider).
"""

from __future__ import annotations

import base64
import binascii
import uuid
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, cast

import httpx
from llmp_shared import EntitySpan
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway import extraction
from gateway.access import accessible_models
from gateway.config import EntityMode, Settings
from gateway.errors import BadRequestError, ServiceUnavailableError
from gateway.models import (
    ApiKey,
    Group,
    ModelDef,
    ModelKind,
    PrivacyClassifier,
    PrivacyPipeline,
    PrivacySettings,
    Provider,
    RedactionPolicyRecord,
    User,
    UserPolicy,
)
from gateway.privacy import attribution
from gateway.privacy import rules as rulelib
from gateway.privacy.combine import Combined, PipelineRun, Retarget, plan
from gateway.privacy.evaluate import Facts
from gateway.privacy.pipelines import Pipeline, pipeline_from_row, summarize
from gateway.privacy.schemas import (
    ActionView,
    ConditionView,
    DryRunHop,
    DryRunItem,
    DryRunLevel,
    DryRunPipeline,
    DryRunPolicyType,
    DryRunRequest,
    DryRunResponse,
    DryRunSkipped,
    DryRunStep,
    IssueView,
    PipelineResponse,
    RuleView,
    TargetView,
    WhoView,
)
from gateway.privacy.state import PrivacyState
from gateway.redaction.http import EngineUnavailableError
from gateway.redaction.user_rules import UserRule

LEVEL_LABELS = {
    "default": "Default reader",
    "read_with": "Read with…",
    "refuse": "Refuse",
    "pass": "Pass",
}


# -- rows --------------------------------------------------------------------------


async def pipeline_rows(
    session: AsyncSession, *, owner: uuid.UUID | None = None
) -> list[PrivacyPipeline]:
    stmt = select(PrivacyPipeline).order_by(PrivacyPipeline.kind, PrivacyPipeline.position)
    stmt = stmt.where(
        PrivacyPipeline.owner_id.is_(None) if owner is None else PrivacyPipeline.owner_id == owner
    )
    return list((await session.execute(stmt)).scalars().all())


async def classifier_rows(session: AsyncSession) -> list[PrivacyClassifier]:
    return list(
        (await session.execute(select(PrivacyClassifier).order_by(PrivacyClassifier.name)))
        .scalars()
        .all()
    )


async def policy_rows(session: AsyncSession) -> list[RedactionPolicyRecord]:
    return list(
        (await session.execute(select(RedactionPolicyRecord).order_by(RedactionPolicyRecord.name)))
        .scalars()
        .all()
    )


async def user_policy_rows(session: AsyncSession, user_id: uuid.UUID) -> list[UserPolicy]:
    return list(
        (
            await session.execute(
                select(UserPolicy).where(UserPolicy.user_id == user_id).order_by(UserPolicy.name)
            )
        )
        .scalars()
        .all()
    )


async def settings_row(session: AsyncSession) -> PrivacySettings:
    row = await session.get(PrivacySettings, 1)
    if row is None:
        row = PrivacySettings(id=1, user_entity_types=None)
        session.add(row)
        await session.flush()
    return row


def pipeline_view_json(row: PrivacyPipeline) -> dict[str, Any]:
    """What the audit log keeps about a pipeline: names and shapes, no values."""
    return {
        "name": row.name,
        "kind": row.kind,
        "enabled": row.enabled,
        "position": row.position,
        "owner_id": str(row.owner_id) if row.owner_id else None,
        "who": dict(row.who or {}),
        "target": dict(row.target or {}),
        "rules": [dict(r) for r in row.rules or []],
        "otherwise": dict(row.otherwise or {}),
    }


# -- checks and views --------------------------------------------------------------


def check_context(
    state: PrivacyState, engines: Any = None, unavailable_readers: Collection[str] = ()
) -> rulelib.CheckContext:
    """What the save-time checks may know: which policies and classifiers can answer now.

    *engines* is an ``EnginesResponse`` when the caller has just asked the service;
    without it only what no live call is needed for is judged (a classifier with no
    model, a policy that is off).
    """
    from gateway.privacy.engines import engine_problem

    content: dict[str, rulelib.ContentInfo] = {}
    for pid, ref in state.policies.items():
        why = ""
        if engines is not None and ref.policy.engine is not None:
            why = engine_problem(engines, ref.policy.engine, bool(ref.policy.custom_entities)) or ""
        content[pid] = rulelib.ContentInfo(ref.name, not why, why)
    for pid, name in state.own_policy_names().items():
        content[pid] = rulelib.ContentInfo(name)
    for cid, classifier in state.classifiers.items():
        content[cid] = rulelib.ContentInfo(classifier.name, classifier.available, classifier.why)
    return rulelib.CheckContext(content=content, unavailable_readers=tuple(unavailable_readers))


def issues_of(
    pipeline: Pipeline,
    state: PrivacyState,
    engines: Any = None,
    unavailable_readers: Collection[str] = (),
) -> dict[uuid.UUID, list[rulelib.Issue]]:
    return rulelib.check(
        list(pipeline.rules),
        pipeline.otherwise,
        check_context(state, engines, unavailable_readers),
    )


def issue_views(found: Iterable[rulelib.Issue]) -> list[IssueView]:
    return [
        IssueView(code=i.code, message=i.message, rule_id=i.rule_id, rule_name=i.rule_name)
        for i in found
    ]


def _rule_view(rule: rulelib.Rule, issues: Sequence[rulelib.Issue]) -> RuleView:
    return RuleView(
        id=rule.id,
        name=rule.name,
        enabled=rule.enabled,
        observe_only=rule.observe_only,
        conditions=[
            ConditionView(key=c.key, values=list(c.values), negate=c.negate, count=c.count)
            for c in rule.conditions
        ],
        action=ActionView.model_validate(
            {
                **dict(rule.action),
                "redact_with": rule.action.get("redact_with"),
            }
        ),
        issues=issue_views(issues),
        note=rule.note,
    )


def content_ids(pipelines: Sequence[Pipeline]) -> tuple[set[str], set[str]]:
    """The policy and the classifier ids the conditions of these pipelines name."""
    finds: set[str] = set()
    classified: set[str] = set()
    for p in pipelines:
        for rule in p.all_rules():
            for c in rule.conditions:
                if c.values and c.key == "finds":
                    finds.add(c.values[0])
                elif c.values and c.key == "classified":
                    classified.add(c.values[0])
    return finds, classified


async def labels_for(session: AsyncSession, pipelines: Sequence[Pipeline]) -> dict[str, str]:
    """id -> name for every user, group, key, provider, model and policy the pipelines name."""
    users: set[str] = set()
    groups: set[str] = set()
    keys: set[str] = set()
    providers: set[str] = set()
    models: set[str] = set()
    policies: set[str] = set()
    named_finds, named_classifiers = content_ids(pipelines)
    policies |= named_finds
    for p in pipelines:
        {"users": users, "groups": groups, "keys": keys}.get(p.who_mode, set()).update(p.who_ids)
        providers.update(p.providers)
        models.update(p.models)
        for rule in p.all_rules():
            if rule.action.get("redact_with"):
                policies.add(str(rule.action["redact_with"]))
    out: dict[str, str] = {}

    def ids(values: set[str]) -> list[uuid.UUID]:
        return [uuid.UUID(v) for v in values]

    async def fill(statement: Any) -> None:
        for ident, label in (await session.execute(statement)).all():
            out[str(ident)] = str(label)

    if users:
        await fill(select(User.id, User.email).where(User.id.in_(ids(users))))
    if groups:
        await fill(select(Group.id, Group.name).where(Group.id.in_(ids(groups))))
    if keys:
        await fill(select(ApiKey.id, ApiKey.name).where(ApiKey.id.in_(ids(keys))))
    if providers:
        await fill(select(Provider.id, Provider.name).where(Provider.id.in_(ids(providers))))
    if models:
        await fill(select(ModelDef.id, ModelDef.name).where(ModelDef.id.in_(ids(models))))
    if policies:
        await fill(
            select(RedactionPolicyRecord.id, RedactionPolicyRecord.name).where(
                RedactionPolicyRecord.id.in_(ids(policies))
            )
        )
        await fill(select(UserPolicy.id, UserPolicy.name).where(UserPolicy.id.in_(ids(policies))))
    if named_classifiers:
        await fill(
            select(PrivacyClassifier.id, PrivacyClassifier.name).where(
                PrivacyClassifier.id.in_(ids(named_classifiers))
            )
        )
    return out


def pipeline_response(
    row: PrivacyPipeline,
    pipeline: Pipeline,
    labels: dict[str, str],
    issues: dict[uuid.UUID, list[rulelib.Issue]],
    reader_names: dict[str, str] | None = None,
) -> PipelineResponse:
    # Only the names this pipeline itself uses: the labels were looked up for a
    # whole list, and a pipeline must not carry the names of another's scope.
    used = {*pipeline.who_ids, *pipeline.providers, *pipeline.models}
    for rule in pipeline.all_rules():
        if rule.action.get("redact_with"):
            used.add(str(rule.action["redact_with"]))
        used.update(
            c.values[0] for c in rule.conditions if c.key in rulelib.CONTENT_KEYS and c.values
        )
    labels = {k: v for k, v in labels.items() if k in used}
    return PipelineResponse(
        id=row.id,
        owner_id=row.owner_id,
        kind=cast("Any", row.kind),
        name=row.name,
        enabled=row.enabled,
        position=row.position,
        who=WhoView(
            mode=cast("Any", pipeline.who_mode), ids=[uuid.UUID(i) for i in pipeline.who_ids]
        ),
        target=TargetView(
            mode=cast("Any", pipeline.target_mode),
            providers=[uuid.UUID(i) for i in pipeline.providers],
            models=[uuid.UUID(i) for i in pipeline.models],
        ),
        rules=[_rule_view(r, issues.get(r.id, [])) for r in pipeline.rules],
        otherwise=_rule_view(pipeline.otherwise, issues.get(pipeline.otherwise.id, [])),
        labels=labels,
        summary=summarize(pipeline, labels, reader_names),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def views_for(
    session: AsyncSession, rows: Sequence[PrivacyPipeline], state: PrivacyState, engines: Any = None
) -> list[PipelineResponse]:
    pipelines = [pipeline_from_row(r, order=i) for i, r in enumerate(rows)]
    labels = await labels_for(session, pipelines)
    return [
        pipeline_response(row, p, labels, issues_of(p, state, engines))
        for row, p in zip(rows, pipelines, strict=True)
    ]


# -- the validation context ----------------------------------------------------------


async def deployment_readers(session: AsyncSession) -> dict[str, bool]:
    """Active deployment OCR models: name -> is it served by the built-in extractor."""
    rows = (
        await session.execute(
            select(ModelDef.name, Provider.plugin)
            .join(Provider, Provider.id == ModelDef.provider_id)
            .where(
                ModelDef.is_active.is_(True),
                ModelDef.owner_id.is_(None),
                ModelDef.kind == ModelKind.OCR,
                Provider.is_active.is_(True),
                Provider.owner_id.is_(None),
            )
            .order_by(ModelDef.name)
        )
    ).all()
    return {str(n): p == "extractor" for n, p in rows}


async def granted_readers(
    session: AsyncSession, user: User, *, group_ids: Sequence[uuid.UUID] | None = None
) -> dict[str, bool]:
    """The OCR models this user may use: name -> is it served by the built-in extractor."""
    groups = (
        list(group_ids) if group_ids is not None else ([g] if (g := request_group_id(user)) else [])
    )
    models = (
        (
            await session.execute(
                accessible_models(user_id=user.id, group_ids=groups).where(
                    ModelDef.kind == ModelKind.OCR
                )
            )
        )
        .unique()
        .scalars()
        .all()
    )
    plugins: dict[uuid.UUID, str | None] = {
        row[0]: row[1]
        for row in (
            await session.execute(
                select(Provider.id, Provider.plugin).where(
                    Provider.id.in_({m.provider_id for m in models if m.provider_id})
                )
            )
        ).all()
    }
    return {
        m.name: (plugins.get(m.provider_id) if m.provider_id else None) == "extractor"
        for m in models
    }


async def unavailable_readers(
    session: AsyncSession, http: httpx.AsyncClient, settings: Settings
) -> set[str]:
    """Names of the built-in extractor's models that cannot run now.

    A model is the engine it reads with, and an engine is available when the
    extractor says so (``/healthz``). Not stored: an image change or an outage
    changes it, and a stored flag would be wrong the moment it was written. An
    extractor that cannot be asked leaves every model but the default reader out
    (``extraction.model_availability``), which is also what the model listing does.
    """
    rows = (
        await session.execute(
            select(ModelDef.name, ModelDef.upstream_model, Provider.base_url)
            .join(Provider, Provider.id == ModelDef.provider_id)
            .where(
                ModelDef.is_active.is_(True),
                ModelDef.owner_id.is_(None),
                ModelDef.kind == ModelKind.OCR,
                Provider.is_active.is_(True),
                Provider.owner_id.is_(None),
                Provider.plugin == "extractor",
            )
        )
    ).all()
    reported: dict[str, frozenset[str] | None] = {}
    gone: set[str] = set()
    for name, upstream, base_url in rows:
        if extraction.engine_of_model(upstream or name) is None:
            continue  # the default reader runs on every build: nothing to ask
        endpoint = (base_url or "").strip() or settings.extractor.endpoint
        if endpoint not in reported:
            reported[endpoint] = await extraction.reported_engines(http, endpoint)
        if extraction.model_availability(upstream or name, reported[endpoint]) is not True:
            gone.add(str(name))
    return gone


@dataclass(slots=True)
class ValidationContext:
    """What a rule may name: policies to apply, policies to count, classifiers, readers."""

    #: Policies "redact with" may name.
    policy_ids: set[str]
    #: Policies "finds at least" may name (the same set).
    finds_policy_ids: set[str]
    classifier_ids: set[str]
    readers: set[str]


async def validation_context(session: AsyncSession, *, owner: User | None) -> ValidationContext:
    """An administrator may name every active policy; a user the shared ones and their own."""
    admin = [p for p in await policy_rows(session) if p.is_active]
    if owner is None:
        policies = {str(p.id) for p in admin}
        readers = set(await deployment_readers(session))
        classifiers = {str(c.id) for c in await classifier_rows(session)}
    else:
        policies = {str(p.id) for p in admin if p.shared_with_users}
        policies |= {str(p.id) for p in await user_policy_rows(session, owner.id)}
        readers = set(await granted_readers(session, owner))
        classifiers = set()
    return ValidationContext(policies, set(policies), classifiers, readers)


# -- user policies --------------------------------------------------------------------


def clean_entries(
    entries: Sequence[Any],
    *,
    allowed_types: Sequence[str] | None,
    reserved: Collection[str] = (),
) -> list[dict[str, Any]]:
    """A user's policy entries, clean, under the administrators' own validation.

    Raises ``ValueError`` with a message for the user. A mode for an entity
    type must be one an administrator made available; a pattern's label is a
    pattern, not a type, so it is not limited, except that it may not be *named* like a
    type (``reserved`` and the detectors' own names): labels and types share one name
    space, and a pattern called PERSON would stand for persons.
    """
    from gateway.redaction import user_rules

    taken = {t.upper() for t in reserved} | user_rules.RESERVED_TYPES

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    allowed = {t.upper() for t in allowed_types} if allowed_types is not None else None
    for entry in entries:
        name = user_rules.clean_label(entry.name)
        try:
            mode = EntityMode(entry.mode)
        except ValueError as exc:
            raise ValueError(f"'{entry.mode}' is not a mode.") from exc
        if mode is EntityMode.OFF:
            raise ValueError("a rule of yours can only add protection, so it cannot be 'off'")
        if entry.kind == "pattern":
            if not entry.regex:
                raise ValueError("A pattern needs a regular expression.")
            if name in taken:
                raise ValueError(
                    f"'{name}' is the name of an entity type; give your pattern another name."
                )
            user_rules.validate_pattern(name, entry.regex, mode)
            regex: str | None = entry.regex
        else:
            if allowed is not None and name not in allowed:
                raise ValueError(
                    f"'{name}' is not an entity type your administrator makes available."
                )
            regex = None
        key = (entry.kind, name)
        if key in seen:
            raise ValueError(f"'{name}' is listed twice.")
        seen.add(key)
        out.append({"kind": entry.kind, "name": name, "regex": regex, "mode": mode.value})
    if len(out) > user_rules.MAX_RULES_PER_USER:
        raise ValueError(f"A policy has at most {user_rules.MAX_RULES_PER_USER} entries.")
    return out


def request_group_id(user: User) -> uuid.UUID | None:
    """The group a request of this user is billed to: the same resolution requests use."""
    from gateway.deps import resolve_billing_group_or_none

    try:
        found = resolve_billing_group_or_none(user)
    except Exception:  # a relationship not loaded here: fall back to the stored default
        return user.default_billing_group_id
    return found.id if found is not None else None


def _visible_to(pipeline: Pipeline, user: User, group: uuid.UUID | None, keys: set[str]) -> bool:
    if pipeline.is_user:
        return pipeline.owner_id == user.id
    if pipeline.who_mode == "users":
        return str(user.id) in pipeline.who_ids
    if pipeline.who_mode == "groups":
        return group is not None and str(group) in pipeline.who_ids
    if pipeline.who_mode == "keys":
        return bool(pipeline.who_ids & keys)
    return True


# -- the dry run -----------------------------------------------------------------------


def _scope_words(pipeline: Pipeline, labels: dict[str, str]) -> list[str]:
    def names(ids: Iterable[str]) -> str:
        return ", ".join(sorted(labels.get(i, "?") for i in ids))

    who = {
        "all": "all users",
        "users": f"users: {names(pipeline.who_ids)}",
        "groups": f"groups: {names(pipeline.who_ids)}",
        "keys": f"keys: {names(pipeline.who_ids)}",
    }[pipeline.who_mode]
    if pipeline.is_user:
        who = "only you"
    if pipeline.target_mode == "all":
        target = "all targets"
    else:
        listed = names({*pipeline.providers, *pipeline.models})
        target = f"only {listed}" if pipeline.target_mode == "only" else f"all except {listed}"
    return [who, target]


def _action_text(rule: rulelib.Rule, labels: dict[str, str]) -> str:
    parts = [LEVEL_LABELS.get(rule.level, rule.level)]
    if rule.level == rulelib.LEVEL_READ_WITH:
        parts = [f"Read with {rule.action.get('reader') or rulelib.DEFAULT_READER}"]
    if rule.action.get("redact_with"):
        parts.append(f"redact with {labels.get(str(rule.action['redact_with']), 'a policy')}")
    return ", ".join(parts)


def _policy_modes(
    state: PrivacyState, combined: Combined
) -> tuple[dict[str, list[tuple[str, str]]], list[str]]:
    """entity type -> [(policy name, mode)] for the policies this request applies."""
    table: dict[str, list[tuple[str, str]]] = {}
    names: list[str] = []
    for use in combined.policy_uses():
        ref = state.policies.get(use.policy_id)
        if ref is not None:
            names.append(ref.name)
            types = ref.policy.service_types()
            for entity, entry in types.items():
                if entry.mode is not EntityMode.OFF:
                    table.setdefault(entity, []).append((ref.name, entry.mode.value))
            for pattern in ref.policy.patterns:
                if pattern.mode is not EntityMode.OFF and pattern.name not in types:
                    table.setdefault(pattern.name, []).append((ref.name, pattern.mode.value))
            for keywords in ref.policy.keywords:
                if keywords.mode is not EntityMode.OFF and keywords.name not in types:
                    table.setdefault(keywords.name, []).append((ref.name, keywords.mode.value))
            continue
        mine = state.user_policies.get(use.policy_id)
        if mine is not None:
            names.append(mine.name)
            rule: UserRule
            for rule in mine.rules:
                table.setdefault(rule.name, []).append((mine.name, rule.mode.value))
    return table, names


async def dry_run(
    session: AsyncSession,
    state: PrivacyState,
    payload: DryRunRequest,
    *,
    user: User | None,
    settings: Settings,
    control_http: Any,
    redactor: object | None,
    viewer_only: bool = False,
) -> DryRunResponse:
    """The real combination over supplied facts: no side effects, nothing logged.

    ``viewer_only`` is a user's own box: other users' pipelines are dropped
    entirely, and so are the administrator's whose who-scope excludes the caller,
    so the answer never names a pipeline (or an owner) that is not theirs.
    """
    # --- facts ---
    model: ModelDef | None = None
    provider: Provider | None = None
    if payload.model_id is not None:
        model = await session.get(ModelDef, payload.model_id)
        if model is None:
            raise BadRequestError("No such model.")
        provider = await session.get(Provider, model.provider_id) if model.provider_id else None
    elif payload.provider_id is not None:
        provider = await session.get(Provider, payload.provider_id)
    order = list(EntityMode)

    filename = attribution.clean_filename(payload.filename)
    mime = payload.mime
    is_scan = payload.is_scan
    extractor: str | None = None
    fallback_reason: str | None = None
    if payload.kind == "read" and payload.file_base64:
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
        except Exception as exc:  # the built-in extractor may simply not be there
            raise BadRequestError(f"The file could not be read here: {exc}") from exc
        extractor, fallback_reason = read.extractor or None, read.fallback_reason or None
        is_scan = attribution.is_scan_from(read.extractor, read.kind.value, bool(read.page_images))
    group = request_group_id(user) if user is not None else None

    def facts_for(m: ModelDef | None, p: Provider | None, scan: bool | None) -> Facts:
        return Facts(
            kind=payload.kind,
            user_id=str(user.id) if user is not None else None,
            billing_group_id=str(group) if group else None,
            filename=filename,
            mime=mime,
            filetype=rulelib.filetype_of(mime, filename),
            is_scan=scan,
            target_model=m.name if m is not None else None,
            target_provider=p.name if p is not None else None,
            provider_id=str(p.id) if p is not None else None,
            model_id=str(m.id) if m is not None else None,
            # Reading decides from the file's facts; only embed and chat judge text.
            text=payload.text
            if payload.kind != "read" and state.uses_verdicts(payload.kind)
            else None,
            judge=judge,
        )

    judge = state.judge(
        user_id=str(user.id) if user is not None else None,
        redactor=redactor,
        default_threshold=settings.redaction.score_threshold,
    )
    facts = facts_for(model, provider, is_scan)

    # --- readers (reading only): what a reroute would reach for this caller ---
    granted: dict[str, bool] = (
        await granted_readers(session, user)
        if user is not None
        else await deployment_readers(session)
    )
    # A reader the extractor cannot run now is not one a reroute can reach.
    gone = await unavailable_readers(session, control_http, settings)
    holder: dict[str, Any] = {"model": model}

    async def retarget(reader: str) -> Retarget:
        current: ModelDef | None = holder["model"]
        if current is not None and current.name == reader:
            return Retarget(None, same=True)
        if reader in granted and reader not in gone:
            target = (
                await session.execute(
                    select(ModelDef).where(ModelDef.name == reader, ModelDef.owner_id.is_(None))
                )
            ).scalar_one_or_none()
            if target is not None:
                tp = await session.get(Provider, target.provider_id) if target.provider_id else None
                holder["model"] = target
                return Retarget(facts_for(target, tp, facts.is_scan))
        return Retarget(
            None, impossible=f"it must be read with '{reader}', which is not available here."
        )

    pipelines = state.pipelines_of(payload.kind)
    if viewer_only and user is not None:
        own_keys = {
            str(k)
            for k in (await session.execute(select(ApiKey.id).where(ApiKey.user_id == user.id)))
            .scalars()
            .all()
        }
        pipelines = tuple(p for p in pipelines if _visible_to(p, user, group, own_keys))
    combined = await plan(
        state.evaluator,
        pipelines,
        facts,
        retarget=retarget if payload.kind == "read" else None,
    )

    # --- policy table ---
    table, policy_names = _policy_modes(state, combined)
    detected_types: set[str] | None = None
    blocked_by: str | None = None
    if (
        payload.text
        and payload.kind != "read"
        and not combined.refused
        and hasattr(redactor, "detect")
        and getattr(redactor, "name", "noop") != "noop"
    ):
        from gateway.redaction.http import eligible_for

        effective = state.effective_policy(combined.policy_uses(), settings.redaction.policy)
        detector = cast("Any", redactor)
        try:
            spans: list[EntitySpan] = (
                await detector.detect(
                    [payload.text], policy=effective.policy, asks=effective.asks(), strict=True
                )
            )[0]
        except EngineUnavailableError as exc:
            raise ServiceUnavailableError(
                f"The NER engine {exc.engine or 'chosen by default'} cannot run: "
                f"{exc.reason or 'it is not available'}."
            ) from exc
        eligible = eligible_for(
            payload.text,
            spans,
            effective.policy,
            settings.redaction.score_threshold,
            effective.user_pattern_keys(),
        )
        detected_types = {s.entity_type for s in eligible}
        for span in eligible:
            if effective.policy.mode_for(span.entity_type) is EntityMode.BLOCK:
                blocked_by = span.entity_type
                break

    types: list[DryRunPolicyType] = []
    for entity, per in sorted(table.items()):
        best = max(per, key=lambda item: order.index(EntityMode(item[1])))
        types.append(
            DryRunPolicyType(
                entity_type=entity,
                mode=best[1],
                source=best[0],
                per_policy=[f"{name}: {mode}" for name, mode in per],
                found=(entity in detected_types) if detected_types is not None else None,
            )
        )

    # --- labels and views ---
    everything = [r.pipeline for r in combined.runs] + [s.pipeline for s in combined.skipped]
    labels = await labels_for(session, everything)

    def run_view(run: PipelineRun) -> DryRunPipeline:
        steps = [
            DryRunStep(
                rule_id=s.rule.id,
                name=s.rule.name,
                position=s.rule.position,
                result=s.result,
                reasons=s.reasons,
                degraded=s.degraded,
                items=[
                    DryRunItem(
                        policy=name,
                        group=item.group,
                        type=item.type,
                        engine=item.engine,
                        score=item.score,
                        text=None if viewer_only else item.value,
                    )
                    for name, found in s.items.items()
                    for item in found
                ],
                note=s.rule.note,
            )
            for s in run.evaluation.steps
        ]
        steps.append(
            DryRunStep(
                rule_id=run.pipeline.otherwise.id,
                name="Everything else",
                position=run.pipeline.otherwise.position,
                result="not_reached"
                if not run.is_otherwise
                else ("pending" if run.pending else "fired"),
                reasons=[],
                degraded=[],
                is_otherwise=True,
            )
        )
        return DryRunPipeline(
            id=run.pipeline.id,
            name=run.pipeline.name,
            owner="user" if run.pipeline.is_user else "admin",
            hop=run.hop,
            scope=_scope_words(run.pipeline, labels),
            steps=steps,
            fired_rule_id=None if run.pending else run.rule.id,
            fired_rule=None if run.pending or run.is_otherwise else run.rule.name,
            fired_otherwise=run.is_otherwise and not run.pending,
            level="pending" if run.pending else run.level,
            action_text=_action_text(run.rule, labels),
            summary=summarize(run.pipeline, labels),
        )

    ladder_levels = rulelib.LADDER[payload.kind]
    asked: dict[str, list[str]] = {}
    for run in combined.runs:
        if not run.pending and run.level != ladder_levels[0]:
            asked.setdefault(run.level, []).append(run.pipeline.name)
    reader_label = combined.reader
    ladder = [
        DryRunLevel(
            level=level,
            label=(
                f"Read with {reader_label}"
                if level == "read_with" and combined.reader
                else LEVEL_LABELS[level]
            ),
            asked_by=asked.get(level, []),
            winning=level == combined.level,
        )
        for level in ladder_levels
    ]

    verdicts: dict[str, Any] = {}
    for run in combined.runs:
        verdicts.update(run.evaluation.verdicts)
    degraded = [d for run in combined.runs for d in run.evaluation.degraded]

    # --- outcome ---
    winner = combined.winner
    outcome, headline, text = (
        "pass",
        "Passes unchanged",
        "No pipeline is in scope: handled as today.",
    )
    if combined.pending:
        outcome, headline = "pending", "Needs the file read"
        text = (
            "A rule is waiting to learn whether the file is a scan. Give a file to read, or say "
            "whether it is one."
        )
    elif combined.impossible:
        outcome, headline = "refuse", "Refused (403)"
        named = combined.reader_run.rule.name if combined.reader_run else ""
        text = f"Named rule: {named}. {combined.impossible}"
    elif combined.refused and winner is not None:
        outcome, headline = "refuse", "Refused (403)"
        text = (
            f"Named rule: {winner.rule.name} in pipeline {winner.pipeline.name}. The message names "
            "the rule, never the content."
        )
    elif blocked_by is not None:
        outcome, headline = "refuse", "Refused (403)"
        text = f"Type '{blocked_by}' is set to block and was found in the text."
    elif payload.kind == "read":
        if combined.level == rulelib.LEVEL_READ_WITH:
            moved = bool(combined.hops)
            outcome = "read_with"
            headline = f"Read by {reader_label}"
            text = (
                "The file goes whole to the reader chosen, instead of the one the caller named. "
                if moved
                else "The file is read by the reader chosen. "
            )
        else:
            outcome = "read_default"
            headline = "Read by the reader the caller named"
            text = "The file leaves as it is; redaction only screens the text that comes back. "
        if policy_names:
            text += f"Text that comes back is screened with {' + '.join(policy_names)}."
    elif combined.runs:
        if policy_names:
            outcome = "redact"
            headline = "Goes on after hiding"
            text = (
                f"Policies: {' + '.join(policy_names)}. "
                "Types found in the text are hidden by the strictest mode."
            )
        else:
            headline, text = "Passes unchanged", "The pipelines in scope add no policy."

    return DryRunResponse(
        kind=payload.kind,
        outcome=outcome,
        outcome_headline=headline,
        outcome_text=text,
        level=combined.level,
        ladder=ladder,
        pipelines=[run_view(r) for r in combined.runs],
        skipped=[
            DryRunSkipped(
                id=s.pipeline.id,
                name=s.pipeline.name,
                owner="user" if s.pipeline.is_user else "admin",
                why=s.why,
            )
            for s in combined.skipped
        ],
        hops=[
            DryRunHop(
                reader=h.reader,
                reader_name=h.reader,
                added=[r.pipeline.name for r in h.added],
            )
            for h in combined.hops
        ],
        winner_rule=None if winner is None else winner.rule.name,
        winner_pipeline=None if winner is None else winner.pipeline.name,
        winner_owner=None if winner is None else ("user" if winner.pipeline.is_user else "admin"),
        reader=reader_label if combined.level == "read_with" else None,
        reader_by=(
            None
            if combined.reader_run is None
            else f"{'your' if combined.reader_run.pipeline.is_user else 'administrator'} "
            f"pipeline {combined.reader_run.pipeline.name}"
        ),
        reader_overridden=[
            f"{r.pipeline.name} asked for {r.rule.action.get('reader') or rulelib.DEFAULT_READER}"
            for r in combined.overridden
        ],
        impossible=combined.impossible,
        policies=policy_names,
        policy_types=types,
        blocked_by=blocked_by,
        facts={
            "filename": filename,
            "mime": mime,
            "filetype": facts.filetype,
            "is_scan": facts.is_scan,
            "target_model": facts.target_model,
            "target_provider": facts.target_provider,
            "text_checked": facts.text is not None,
        },
        verdicts=verdicts,
        degraded=list(dict.fromkeys(degraded)),
        needs="scan" if combined.pending else None,
        extractor=extractor,
        fallback_reason=fallback_reason,
        estimate_ms=None,
    )


# -- the up-front summary for clients -------------------------------------------------


async def summary_for(
    session: AsyncSession,
    state: PrivacyState,
    user: User,
    group: uuid.UUID | None,
    key_id: uuid.UUID | None = None,
) -> tuple[
    list[tuple[ModelDef, list[str], list[str]]], list[tuple[ModelDef, list[str], list[str]]]
]:
    """Per embedding model and per chat model the user may use: the pipelines in scope.

    Short words only, from the pipelines that cover this user and that model's
    provider and model ids. The user's own pipelines are included (they are
    theirs), other users' never are. Observe-only rules are not mentioned: they
    change nothing for the caller.
    """
    models = list(
        (
            await session.execute(
                accessible_models(user_id=user.id, group_ids=[group] if group else [])
                .where(ModelDef.kind.in_([ModelKind.EMBEDDING, ModelKind.CHAT]))
                .order_by(ModelDef.name)
            )
        )
        .unique()
        .scalars()
        .all()
    )
    from gateway.privacy.pipelines import not_in_scope

    names = {pid: ref.name for pid, ref in state.policies.items()}
    names.update({pid: ref.name for pid, ref in state.user_policies.items()})
    names.update({cid: ref.name for cid, ref in state.classifiers.items()})
    out: dict[str, list[tuple[ModelDef, list[str], list[str]]]] = {"embed": [], "chat": []}
    for model in models:
        kind = "embed" if model.kind is ModelKind.EMBEDDING else "chat"
        words: list[str] = []
        used: list[str] = []
        for p in state.pipelines_of(kind):
            why = not_in_scope(
                p,
                user_id=str(user.id),
                group_id=str(group) if group else None,
                key_id=str(key_id) if key_id else None,
                provider_id=str(model.provider_id) if model.provider_id else None,
                model_id=str(model.id),
            )
            if why:
                # A key-scoped pipeline is in scope only for calls made with that key.
                continue
            said = summarize(p, names)
            if said:
                used.append(p.name)
                words.extend(said)
        out[kind].append((model, list(dict.fromkeys(words)), used))
    return out["embed"], out["chat"]
