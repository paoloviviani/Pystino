"""Which engine this worker is running, and how it learns that it changed.

See [ADR 0033](../../../../docs/adr/0033-redaction-engine-selection.md).

Redaction used to be read once, at startup, from the environment. Now an admin
can change it from the console, which raises a question the old design did not
have to answer: **how does a worker that did not handle the request find out?**

Three answers were possible and two are wrong.

*Read the row per request.* Correct and immediate, and it puts a `SELECT` on the
hottest path in the gateway for a value that changes a few times a year. It also
breaks the round-trip budget that ``test_query_counts.py`` pins, which exists
precisely so this kind of cost cannot be added without noticing.

*Require a restart.* Honest, and what OIDC discovery does — but redaction is not
OIDC. An operator switching the layer off is often responding to an incident, and
"now restart the gateway" is the wrong sentence to read at that moment.

*Poll.* One query every ``refresh_seconds`` per worker, off the request path
entirely, so ``get_redactor`` stays a single attribute read. Staleness is bounded
by the interval and the API says so rather than implying the change is instant.
That is what this does.

Two properties worth stating because they are easy to lose:

* **The redactor is rebuilt only when the row actually changes**, not on every
  poll. ``HttpDetectionRedactor`` owns a connection pool and a per-process
  detection LRU that is ~90% of the value of the cache (see docs/performance.md);
  rebuilding it every ten seconds would quietly destroy the hit rate.
* **A failed poll changes nothing.** The engine in force stays in force and the
  failure is logged. A database blip must not switch redaction off, and it must
  not switch it on either — either direction is a silent policy change made by an
  outage.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.config import EffectivePolicy, RedactionPolicy, RedactionSettings
from gateway.models import RedactionConfig, RedactionRule, RedactionScope
from gateway.redaction.base import Redactor
from gateway.redaction.registry import UnknownEngineError, resolve

logger = logging.getLogger(__name__)

#: How long a worker may keep serving with an engine an admin has changed. Ten
#: seconds: long enough that the poll is free, short enough that an operator who
#: just switched the layer off does not wonder whether it worked.
DEFAULT_REFRESH_SECONDS = 10.0


#: A cap on how many scoped rules a worker will hold. Rules are loaded into
#: memory so the request path stays query-free (ADR 0038), which trades an
#: unbounded table for an unbounded footprint. Bounded and logged rather than
#: silently truncated: a deployment that hits this is one whose redaction is
#: quietly not what its console says.
MAX_RULES = 2000


async def load_rules(session: AsyncSession) -> list[RedactionRule]:
    """Every active scoped rule, newest first, up to :data:`MAX_RULES`."""
    return list(
        (
            await session.execute(
                select(RedactionRule)
                .where(RedactionRule.is_active.is_(True))
                .order_by(RedactionRule.updated_at.desc())
                .limit(MAX_RULES + 1)
            )
        )
        .scalars()
        .all()
    )


async def current_engine(session: AsyncSession) -> RedactionConfig | None:
    """The newest ``redaction_config`` row, or None if the console never set one.

    None is not "off". It means the environment decides, which is what every
    deployment did before this table existed.
    """
    return (
        await session.execute(
            select(RedactionConfig).order_by(RedactionConfig.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()


def build_for(
    settings: RedactionSettings, engine: str, policy: RedactionPolicy | None = None
) -> Redactor:
    """Construct *engine* using the environment's other redaction settings.

    Two things the console can change — the engine and the per-entity policy —
    and everything else still comes from the environment: the endpoint, the
    placeholder key, the timeouts. So this is a settings copy with those fields
    replaced, and the engine's own constructor is what refuses a combination that
    cannot work — a missing endpoint, an unset placeholder key.

    ``policy=None`` means the row said nothing about it, which is not "redact
    nothing": the deployment's own policy stands.
    """
    update: dict[str, object] = {"engine": engine}
    if policy is not None:
        update["policy"] = policy
    return resolve(engine)(settings.model_copy(update=update))


class RedactionResolver:
    """Holds the engine in force for this worker, and polls for changes."""

    def __init__(
        self,
        settings: RedactionSettings,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        on_change: Callable[[Redactor], None] | None = None,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        # Called with the new redactor whenever it changes, so `app.state.redactor`
        # stays the single place the rest of the gateway reads. Passed in rather
        # than reaching into app state, which keeps this testable without a FastAPI
        # app and keeps the resolver ignorant of where it is mounted.
        self._on_change = on_change
        self._refresh_seconds = refresh_seconds

        self._redactor: Redactor = resolve(settings.engine)(settings)
        #: The policy in force, which is the environment's until a row carries
        #: one. Held here so the admin screen can report what is *running*
        #: rather than what is configured — the same rule the engine follows.
        self._policy: RedactionPolicy = settings.policy
        #: Scoped rules, by (scope, subject id). Loaded in the same poll as the
        #: engine, so `policy_for` is dictionary lookups and no query at all.
        self._rules: dict[tuple[str, uuid.UUID], _Rule] = {}
        #: Folded results, keyed by the rule ids that produced them. A fold is
        #: microseconds against a 65ms detection floor, but it runs on every
        #: request and the inputs repeat; cleared whenever the rules reload.
        self._folded: dict[tuple[uuid.UUID, ...], RedactionPolicy] = {}
        #: The row this worker built from, or None while the environment decides.
        #: Compared by id, not by engine name: two rows naming the same engine are
        #: still two decisions, and an operator who switches away and back should
        #: see the second change take effect.
        self._config_id: Any = None
        self._source = "environment"
        self._policy_source = "environment"
        self._task: asyncio.Task[None] | None = None

    # -- what the request path reads ---------------------------------------

    @property
    def redactor(self) -> Redactor:
        return self._redactor

    @property
    def policy(self) -> RedactionPolicy:
        """The deployment policy, before any scoped rule."""
        return self._policy

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    def policy_for(
        self,
        *,
        provider_id: uuid.UUID | None = None,
        model_id: uuid.UUID | None = None,
        group_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
        api_key_id: uuid.UUID | None = None,
    ) -> EffectivePolicy:
        """The policy governing one request, and which rule to blame for it.

        Order matters and is widest first: ``combine`` takes the allow-list from
        the first policy alone, and the first is always the deployment's.
        Everything else folds by strictest-wins, so the order of the rest is
        irrelevant to the result — but keeping it widest-to-narrowest is what
        makes "the narrowest rule that applied" meaningful below.
        """
        subjects = (
            (RedactionScope.PROVIDER, provider_id),
            (RedactionScope.MODEL, model_id),
            (RedactionScope.GROUP, group_id),
            (RedactionScope.USER, user_id),
            (RedactionScope.API_KEY, api_key_id),
        )
        applicable = [
            rule
            for scope, subject in subjects
            if subject is not None and (rule := self._rules.get((scope.value, subject))) is not None
        ]
        if not applicable:
            return EffectivePolicy(policy=self._policy)

        key = tuple(rule.id for rule in applicable)
        folded = self._folded.get(key)
        if folded is None:
            folded = RedactionPolicy.combine([self._policy, *(rule.policy for rule in applicable)])
            self._folded[key] = folded

        # The narrowest, because that is the one somebody set deliberately for
        # this subject and the first place an operator looks. A request under
        # several rules names only this one; the others are visible on the rules
        # screen, and ADR 0038 says so rather than implying the trail is complete.
        narrowest = applicable[-1]
        return EffectivePolicy(
            policy=folded, scope=narrowest.scope, rule_id=narrowest.id
        )

    @property
    def policy_source(self) -> str:
        """``console`` when the row in force carries a policy of its own."""
        return self._policy_source

    @property
    def source(self) -> str:
        """``console`` if a row is in force, ``environment`` otherwise.

        Reported on the admin screen because the two disagreeing is invisible
        otherwise, and it is exactly the confusion a database override introduces.
        """
        return self._source

    @property
    def refresh_seconds(self) -> float:
        return self._refresh_seconds

    # -- keeping it current ------------------------------------------------

    async def refresh(self) -> bool:
        """Read the row and rebuild if it changed. True if the engine changed.

        Never raises. A poll that cannot read the database leaves the engine in
        force exactly as it was: a blip must not change redaction policy in either
        direction.
        """
        try:
            async with self._session_factory() as session:
                row = await current_engine(session)
                rules = await load_rules(session)
                row_id = row.id if row is not None else None
                engine = row.engine if row is not None else self._settings.engine
                policy = _policy_of(row)
                changed = self._reload_rules(rules)
                if row_id == self._config_id:
                    # Rules reloaded either way: they are a dictionary swap, not
                    # a redactor rebuild, so they cost nothing and must not wait
                    # for an engine change that may never come.
                    return changed
        except Exception:
            logger.warning("could not read the redaction configuration", exc_info=True)
            return False

        try:
            replacement = build_for(self._settings, engine, policy)
        except (UnknownEngineError, ValueError):
            # The engine was validated when it was saved, so reaching here means
            # the deployment changed underneath it — a plugin uninstalled, or an
            # endpoint removed from the environment. Keep serving with what is in
            # force and say so loudly: falling back to noop would switch the
            # layer off as a side effect of a packaging mistake.
            logger.error(
                "the configured redaction engine %r cannot be constructed; still running %r",
                engine,
                getattr(self._redactor, "name", "unknown"),
                exc_info=True,
            )
            return False

        previous = self._redactor
        self._redactor = replacement
        self._config_id = row_id
        self._source = "console" if row_id is not None else "environment"
        self._policy = policy or self._settings.policy
        self._policy_source = "console" if policy is not None else "environment"
        if self._on_change is not None:
            self._on_change(replacement)
        logger.info(
            "redaction engine is now %r (from the %s)",
            getattr(replacement, "name", engine),
            self._source,
        )
        await _close_quietly(previous)
        return True

    def _reload_rules(self, rows: Sequence[RedactionRule]) -> bool:
        """Swap the rule table in. True when it changed."""
        if len(rows) > MAX_RULES:
            logger.error(
                "more than %d active redaction rules; only the %d most recently "
                "updated are in force on this worker",
                MAX_RULES,
                MAX_RULES,
            )
            rows = rows[:MAX_RULES]
        parsed = _rules_of(rows)
        if parsed == self._rules:
            return False
        self._rules = parsed
        self._folded.clear()
        logger.info("redaction rules reloaded: %d active", len(parsed))
        return True

    def start(self) -> None:
        """Begin polling. Idempotent."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll())

    async def _poll(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._refresh_seconds)
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                # `refresh` swallows its own failures, so anything here is a bug
                # in the loop rather than in the read. Logged and retried: a dead
                # poller means an engine change that never lands, silently.
                logger.warning("the redaction poller raised; continuing", exc_info=True)

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            # Awaiting the cancellation rather than abandoning it, so shutdown
            # cannot race a poll that is mid-rebuild and leave a client pool open.
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await _close_quietly(self._redactor)


@dataclass(frozen=True, slots=True)
class _Rule:
    """One scoped rule, parsed once at load rather than per request."""

    id: uuid.UUID
    scope: str
    policy: RedactionPolicy


def _policy_of(row: RedactionConfig | None) -> RedactionPolicy | None:
    """The policy a row carries, or None if it carries none.

    A stored policy that no longer parses is treated as absent and logged, not
    raised: the alternative is a worker that stops redacting because a column it
    could not read was malformed, which is the failure this whole module exists
    to avoid.
    """
    if row is None or row.policy is None:
        return None
    try:
        return RedactionPolicy.model_validate(row.policy)
    except ValidationError:
        logger.error(
            "the stored redaction policy on row %s could not be read; using the "
            "deployment default instead",
            row.id,
            exc_info=True,
        )
        return None


def _rules_of(rows: Sequence[RedactionRule]) -> dict[tuple[str, uuid.UUID], _Rule]:
    """Parse rows into rules, dropping any whose policy no longer reads.

    Dropped rather than raised, and logged loudly: one malformed row must not
    stop a worker resolving policy for every other scope. The failure it would
    otherwise cause is the worst kind — redaction stops for everyone because one
    group's rule has a typo.
    """
    parsed: dict[tuple[str, uuid.UUID], _Rule] = {}
    for row in rows:
        try:
            policy = RedactionPolicy.model_validate(row.policy or {})
        except ValidationError:
            logger.error("redaction rule %s has an unreadable policy; ignoring it", row.id)
            continue
        parsed[(str(row.scope), row.scope_id)] = _Rule(
            id=row.id, scope=str(row.scope), policy=policy
        )
    return parsed


async def _close_quietly(redactor: Redactor) -> None:
    """Release an engine's resources, if it has any.

    Optional rather than part of the ``Redactor`` protocol, matching how shutdown
    already treats it: most engines have nothing to release, and requiring an
    empty ``aclose`` from every plugin author is friction for no benefit.
    """
    closer = getattr(redactor, "aclose", None)
    if closer is None:
        return
    try:
        await closer()
    except Exception:
        logger.warning("could not close the previous redaction engine", exc_info=True)
