"""The pipelines and policies in force on this worker, and how it learns they changed.

The same shape as the redaction resolver (ADR 0033), for the same reason: the
request path must stay query-free (``test_query_counts.py``), so pipelines,
policies, profiles and the names of readers arrive on a ten-second poll and a
request reads attributes. A failed poll changes nothing; an admin's own write
refreshes the worker that served it at once, and the others catch up within the
interval.

One state holds both objects: **policies** (what to hide) and **pipelines**
(when and how), because a request needs both from one consistent snapshot, and
because a pipeline names a policy.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.config import (
    Contributor,
    EffectivePolicy,
    EntityMode,
    RedactionPolicy,
)
from gateway.models import (
    ModelDef,
    ModelKind,
    PrivacyClassifier,
    PrivacyPipeline,
    PrivacySettings,
    Provider,
    RedactionPolicyRecord,
    UserPolicy,
)
from gateway.privacy import rules as rulelib
from gateway.privacy.classifiers import ClassifierRef
from gateway.privacy.combine import PolicyUse
from gateway.privacy.evaluate import Evaluator
from gateway.privacy.finds import ClassifierBackend, ContentJudge
from gateway.privacy.pipelines import Pipeline, pipeline_from_row
from gateway.redaction import user_rules
from gateway.redaction.user_rules import UserRule

logger = logging.getLogger(__name__)

DEFAULT_REFRESH_SECONDS = 10.0

#: Held in memory on every worker, so bounded (logged, never silently truncated).
MAX_PIPELINES = 5000

#: The deployment's own extractor: a provider row whose plugin is this.
LOCAL_EXTRACTOR = "extractor"


def _schema_missing(exc: Exception) -> bool:
    text = str(exc).lower()
    return "privacy_pipelines" in text and ("no such table" in text or "does not exist" in text)


@dataclass(frozen=True, slots=True)
class PolicyRef:
    """An administrator's policy, parsed once at load."""

    id: uuid.UUID
    name: str
    policy: RedactionPolicy
    #: Whether a user's own pipeline may name it.
    shared: bool = False


@dataclass(frozen=True, slots=True)
class UserPolicyRef:
    """A user's own policy: ordered entries the fold applies after the administrators'."""

    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    rules: tuple[UserRule, ...]


def user_rule_of(raw: dict[str, Any], rule_id: uuid.UUID) -> UserRule:
    """One stored entry, under the administrators' own validation. Raises ``ValueError``."""
    mode = EntityMode(str(raw.get("mode")))
    if mode is EntityMode.OFF:
        raise ValueError("a user rule cannot be off")
    kind = str(raw.get("kind"))
    name = str(raw.get("name") or "")
    regex = raw.get("regex")
    if kind == "pattern":
        if not isinstance(regex, str):
            raise ValueError("a pattern needs a regex")
        user_rules.validate_pattern(name, regex, mode)
    elif kind != "mode":
        raise ValueError("unknown kind")
    return UserRule(
        id=rule_id, kind=kind, name=name, regex=regex if kind == "pattern" else None, mode=mode
    )


def policy_as_user_rules(ref: PolicyRef) -> list[UserRule]:
    """An administrator's policy as tighten-only additions (a user picked it).

    Only what can add protection survives: a mode for a type, a pattern (keyword lists
    and custom entity types travel beside them, into ``user_rules.fold``). The
    thresholds, placeholder styles and the allow-list of the policy are not
    carried (they are administrator-only fields, and ``user_rules.fold`` has no
    place for them); its default mode is applied as a floor by the caller.
    """
    out: list[UserRule] = []
    for name, entry in ref.policy.service_types().items():
        if entry.mode is not EntityMode.OFF:
            out.append(UserRule(uuid.uuid5(ref.id, f"mode:{name}"), "mode", name, None, entry.mode))
    for pattern in ref.policy.patterns:
        if pattern.mode is not EntityMode.OFF:
            out.append(
                UserRule(
                    uuid.uuid5(ref.id, f"pattern:{pattern.name}:{pattern.regex}"),
                    "pattern",
                    pattern.name,
                    pattern.regex,
                    pattern.mode,
                )
            )
    return out


def user_policy_of(row: UserPolicy) -> UserPolicyRef:
    rules = tuple(
        user_rule_of(raw, uuid.uuid5(row.id, f"{index}:{raw.get('name')}"))
        for index, raw in enumerate(row.rules or [])
    )
    return UserPolicyRef(row.id, row.user_id, row.name, rules)


class PrivacyState:
    """Pipelines, policies, profiles and reader names for the request path."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        *,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._refresh_seconds = refresh_seconds
        self._task: asyncio.Task[None] | None = None
        self.pipelines: tuple[Pipeline, ...] = ()
        self.policies: dict[str, PolicyRef] = {}
        self.user_policies: dict[str, UserPolicyRef] = {}
        self.classifiers: dict[str, ClassifierRef] = {}
        #: Asks a classifier's model. The default calls the provider; a test sets a fake.
        self.classifier_backend: ClassifierBackend | None = None
        #: id -> (name, policy) for every active administrator policy.
        self._admin_views: dict[str, tuple[str, RedactionPolicy]] = {}
        #: user id -> id -> (name, policy) for each user's own policies.
        self._user_views: dict[str, dict[str, tuple[str, RedactionPolicy]]] = {}
        #: Names of active deployment models served by the built-in extractor.
        self.builtin_readers: tuple[str, ...] = ()
        #: Names of every active deployment OCR model: what "read with" may name.
        self.reader_models: tuple[str, ...] = ()
        #: Entity types users' own policies may name; None means any.
        self.user_entity_types: tuple[str, ...] | None = None
        self.evaluator = Evaluator()
        #: The folded policies, keyed by the ids that produced them; cleared on load.
        self._folded: dict[tuple[str, ...], RedactionPolicy] = {}
        self._user_folded: dict[
            tuple[str, ...], tuple[RedactionPolicy, tuple[Contributor, ...]]
        ] = {}
        #: False until the pipelines have been read once. A worker that has never
        #: read them cannot say what they are, so requests of the kinds they
        #: govern fail closed (503) rather than run with none. An app built
        #: without a database (no session factory) has none to read.
        self.loaded = session_factory is None
        self.load_error: str | None = None
        #: Rows that could not be turned into a pipeline or policy, as
        #: "id: reason". Skipped with an error log; the rest still load.
        self.bad_rows: list[str] = []

    @property
    def configured(self) -> bool:
        """Whether any pipeline does routing: the condition for logging every request.

        A pipeline that only applies a policy (no rule, a last rule that adds a
        policy and nothing else) is redaction as it always was, and logs as it
        always did: only a request that something was hidden in. A deployment
        migrated from scoped redaction rules is all of those, and its log must
        not change shape because of the migration.
        """
        return any(
            p.enabled
            and (any(r.enabled for r in p.rules) or p.otherwise.level != rulelib.base_level(p.kind))
            for p in self.pipelines
        )

    @property
    def bad_rules(self) -> list[str]:
        return self.bad_rows

    def pipelines_of(self, kind: str) -> tuple[Pipeline, ...]:
        return tuple(p for p in self.pipelines if p.kind == kind and p.enabled)

    def uses_verdicts(self, kind: str) -> bool:
        """Whether an enabled pipeline of this kind needs the request's own text read.

        A classifier no rule uses is never called (ADR 0113, section 4).
        """
        return any(p.uses_verdicts() for p in self.pipelines if p.kind == kind)

    def judge(
        self, *, user_id: str | None, redactor: object | None, default_threshold: float
    ) -> ContentJudge:
        """What counts items and asks classifiers for one request (no queries)."""
        policies = dict(self._admin_views)
        if user_id is not None:
            policies.update(self._user_views.get(user_id, {}))
        return ContentJudge(
            policies=policies,
            classifiers=self.classifiers,
            redactor=redactor,
            default_threshold=default_threshold,
            backend=self.classifier_backend,
        )

    def name_of(self, ident: str) -> str | None:
        """The name of a policy or a classifier by id."""
        found = self.policy_name(ident)
        if found is not None:
            return found
        ref = self.classifiers.get(ident)
        return ref.name if ref is not None else None

    def policy_name(self, policy_id: str) -> str | None:
        found = self.policies.get(policy_id)
        if found is not None:
            return found.name
        mine = self.user_policies.get(policy_id)
        return mine.name if mine is not None else None

    # -- the fold ------------------------------------------------------------

    def effective_policy(
        self, uses: Sequence[PolicyUse], environment: RedactionPolicy
    ) -> EffectivePolicy:
        """The policy governing one request, from the policies its pipelines chose.

        The administrators' policies fold by ``RedactionPolicy.combine``, widest
        scope first, and are the whole answer when any applies (the
        environment's policy, which ships filtering nothing, stands only when
        none does: ADR 0039). The requesting user's own policies are folded in
        last by ``user_rules.fold`` and can only tighten (ADR 0112). Zero
        queries: everything here arrived on the poll.
        """
        admin_refs: list[tuple[PolicyRef, PolicyUse]] = []
        #: Administrator policies a *user's* pipeline chose, and the user's own.
        user_picked: list[PolicyRef] = []
        own: list[UserPolicyRef] = []
        for use in uses:
            ref = self.policies.get(use.policy_id)
            by_user = use.run.pipeline.is_user
            if ref is not None:
                if by_user:
                    user_picked.append(ref)
                else:
                    admin_refs.append((ref, use))
                continue
            mine = self.user_policies.get(use.policy_id)
            if mine is not None and mine.user_id == use.run.pipeline.owner_id:
                if all(mine.id != m.id for m in own):
                    own.append(mine)
                continue
            logger.warning("a pipeline names policy %s, which is not loaded", use.policy_id)
        if not admin_refs:
            admin = EffectivePolicy(
                policy=environment,
                contributors=(
                    Contributor(
                        id=None, name="Deployment policy", owner="admin", policy=environment
                    ),
                ),
            )
        else:
            key = tuple(str(ref.id) for ref, _ in admin_refs)
            folded = self._folded.get(key)
            if folded is None:
                folded = RedactionPolicy.combine([ref.policy for ref, _ in admin_refs])
                self._folded[key] = folded
            narrowest_ref, narrowest_use = admin_refs[-1]
            admin = EffectivePolicy(
                policy=folded,
                scope=narrowest_use.run.pipeline.scope_name,
                rule_id=narrowest_ref.id,
                contributors=tuple(
                    Contributor(id=ref.id, name=ref.name, owner="admin", policy=ref.policy)
                    for ref, _ in admin_refs
                ),
            )
        # What a user's pipelines add comes after, as tighten-only additions to the
        # administrators' answer (or the environment's): a user's choice never
        # decides the allow-list and never replaces the environment's policy.
        rules = tuple(
            [r for ref in user_picked for r in policy_as_user_rules(ref)]
            + [rule for policy in own for rule in policy.rules]
        )
        if not rules and not user_picked:
            return admin
        ukey = (
            *(str(c.id) for c in admin.contributors),
            "|",
            *(str(p.id) for p in user_picked),
            "|",
            *(str(p.id) for p in own),
        )
        cached = self._user_folded.get(ukey)
        if cached is None:
            folded_user = user_rules.fold(
                admin.policy,
                rules,
                keywords=[k for ref in user_picked for k in ref.policy.keywords],
                custom_entities=[c for ref in user_picked for c in ref.policy.custom_entities],
            )
            floor = max(
                [admin.policy.default_mode, *(ref.policy.default_mode for ref in user_picked)],
                key=list(EntityMode).index,
            )
            if floor is not folded_user.default_mode:
                folded_user = folded_user.model_copy(update={"default_mode": floor})
            cached = (
                folded_user,
                (
                    *(
                        Contributor(id=ref.id, name=ref.name, owner="admin", policy=ref.policy)
                        for ref in user_picked
                    ),
                    *(rule.contributor() for policy in own for rule in policy.rules),
                ),
            )
            self._user_folded[ukey] = cached
        policy, contributors = cached
        return EffectivePolicy(
            policy=policy,
            scope=admin.scope,
            rule_id=admin.rule_id,
            contributors=(*admin.contributors, *contributors),
        )

    # -- loading -------------------------------------------------------------

    def load(
        self,
        pipelines: Sequence[PrivacyPipeline],
        policies: Sequence[RedactionPolicyRecord],
        user_policy_rows: Sequence[UserPolicy],
        classifiers: Sequence[ClassifierRef],
        reader_models: Sequence[str],
        builtin_readers: Sequence[str],
        settings: PrivacySettings | None = None,
    ) -> None:
        bad: list[str] = []
        good: list[Pipeline] = []
        rows = sorted(pipelines, key=lambda r: (r.kind, r.owner_id is not None, r.position))
        if len(rows) > MAX_PIPELINES:
            logger.error(
                "more than %d pipelines; only the first %d are in force",
                MAX_PIPELINES,
                MAX_PIPELINES,
            )
            rows = rows[:MAX_PIPELINES]
        for index, row in enumerate(rows):
            try:
                good.append(pipeline_from_row(row, order=index))
            except Exception as exc:
                bad.append(f"{row.id}: {type(exc).__name__}")
                logger.error(
                    "skipping privacy pipeline %s, which cannot be read", row.id, exc_info=True
                )
        parsed: dict[str, PolicyRef] = {}
        for record in policies:
            if not record.is_active:
                continue
            try:
                parsed[str(record.id)] = PolicyRef(
                    record.id,
                    record.name or "Policy",
                    RedactionPolicy.model_validate(record.policy or {}),
                    bool(record.shared_with_users),
                )
            except ValidationError:
                bad.append(f"{record.id}: unreadable policy")
                logger.error("redaction policy %s is unreadable; ignoring it", record.id)
        mine: dict[str, UserPolicyRef] = {}
        for user_row in user_policy_rows:
            try:
                mine[str(user_row.id)] = user_policy_of(user_row)
            except ValueError:
                bad.append(f"{user_row.id}: unreadable user policy")
                logger.error("user policy %s is unreadable; ignoring it", user_row.id)
        self.pipelines = tuple(good)
        self.policies = parsed
        self.user_policies = mine
        self.bad_rows = bad
        self.loaded = True
        self.load_error = None
        self._folded.clear()
        self._user_folded.clear()
        self.classifiers = {c.id: c for c in classifiers}
        self._admin_views = {pid: (ref.name, ref.policy) for pid, ref in parsed.items()}
        self._user_views = {}
        for pid, owned in mine.items():
            self._user_views.setdefault(str(owned.user_id), {})[pid] = (
                owned.name,
                user_rules.fold(RedactionPolicy(), owned.rules),
            )
        self.reader_models = tuple(reader_models)
        self.builtin_readers = tuple(builtin_readers)
        types = settings.user_entity_types if settings is not None else None
        self.user_entity_types = tuple(types) if types is not None else None
        self.evaluator = Evaluator()

    async def refresh(self) -> bool:
        """Re-read from the database. Never raises; True when it read."""
        if self._session_factory is None:
            return False
        try:
            async with self._session_factory() as session:
                self.load(*await read_all(session))
        except Exception as exc:
            if _schema_missing(exc):
                # The migration has not run, so no pipeline can exist: nothing to
                # enforce. (Migrations run before the gateway starts in a real
                # deployment; this is what a database without the table says.)
                self.loaded = True
                return False
            self.load_error = f"{type(exc).__name__}"
            logger.warning("could not refresh the privacy pipelines", exc_info=True)
            return False
        return True

    def ensure_loaded(self) -> None:
        """Refuse (503) when this worker has never read the pipelines."""
        if not self.loaded:
            from gateway.errors import ServiceUnavailableError

            raise ServiceUnavailableError(
                "Privacy pipelines have not loaded on this server yet, so the "
                "request is refused rather than run without them. Try again shortly.",
                code="privacy_rules_unavailable",
            )

    def start(self) -> None:
        if self._task is None and self._session_factory is not None:
            self._task = asyncio.create_task(self._poll())

    async def _poll(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._refresh_seconds)
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("the privacy poller raised; continuing", exc_info=True)

    async def aclose(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task


async def read_all(
    session: AsyncSession,
) -> tuple[
    list[PrivacyPipeline],
    list[RedactionPolicyRecord],
    list[UserPolicy],
    list[ClassifierRef],
    list[str],
    list[str],
    PrivacySettings | None,
]:
    pipelines = list(
        (await session.execute(select(PrivacyPipeline).order_by(PrivacyPipeline.kind)))
        .scalars()
        .all()
    )
    policies = list(
        (await session.execute(select(RedactionPolicyRecord).order_by(RedactionPolicyRecord.id)))
        .scalars()
        .all()
    )
    mine = list((await session.execute(select(UserPolicy).order_by(UserPolicy.id))).scalars().all())
    classifiers = await classifier_refs(session)
    settings = await session.get(PrivacySettings, 1)
    readers = (
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
    return (
        pipelines,
        policies,
        mine,
        classifiers,
        [str(r[0]) for r in readers],
        [str(r[0]) for r in readers if r[1] == LOCAL_EXTRACTOR],
        settings,
    )


async def classifier_refs(session: AsyncSession) -> list[ClassifierRef]:
    """Every classifier with what it needs to answer, read once per poll."""
    out: list[ClassifierRef] = []
    rows = (
        await session.execute(
            select(PrivacyClassifier, ModelDef, Provider)
            .outerjoin(ModelDef, ModelDef.id == PrivacyClassifier.model_id)
            .outerjoin(Provider, Provider.id == ModelDef.provider_id)
            .order_by(PrivacyClassifier.name)
        )
    ).all()
    for row, model, provider in rows:
        why = ""
        if model is None or provider is None:
            why = "no model is chosen"
        elif not model.is_active or not provider.is_active:
            why = "its model or provider is switched off"
        elif model.kind is not ModelKind.CHAT:
            why = "its model is not a chat model"
        elif not (row.topic or "").strip():
            why = "no topic is written"
        out.append(
            ClassifierRef(
                id=str(row.id),
                name=row.name,
                topic=row.topic or "",
                model_name=model.name if model is not None else None,
                upstream_model=model.upstream_model if model is not None else None,
                provider=provider,
                available=not why,
                why=why,
            )
        )
    return out


_EMPTY = PrivacyState()


def state_of(request: Any) -> PrivacyState:
    """The worker's privacy state; an empty one for an app built without it."""
    found = getattr(request.app.state, "privacy", None)
    return found if isinstance(found, PrivacyState) else _EMPTY
