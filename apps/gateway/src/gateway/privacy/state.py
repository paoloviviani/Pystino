"""The routing rules in force on this worker, and how it learns they changed.

The same shape as the redaction resolver (ADR 0033), for the same reason: the
request path must stay query-free (``test_query_counts.py``), so rules, profiles
and the names of local models arrive on a ten-second poll and a request reads
attributes. A failed poll changes nothing; an admin's own write refreshes the
worker that served it at once, and the others catch up within the interval.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.models import DetectorProfile, ModelDef, ModelKind, PrivacyRule, Provider
from gateway.privacy.classify import VerdictCache
from gateway.privacy.evaluate import Evaluator, Profile, profile_snapshot
from gateway.privacy.rules import Rule, condition_from_json

logger = logging.getLogger(__name__)

DEFAULT_REFRESH_SECONDS = 10.0

#: Engines this release can actually run for a verdict. A model engine (phase 2)
#: is "not installed" until it advertises itself.
RUNNABLE_ENGINES = frozenset({"presidio", "patterns"})


def engine_available(engine: str) -> bool:
    return engine in RUNNABLE_ENGINES


def rule_from_row(row: PrivacyRule) -> Rule:
    return Rule(
        id=row.id,
        kind=row.kind,
        name=row.name,
        enabled=row.enabled,
        observe_only=row.observe_only,
        position=row.position,
        conditions=tuple(condition_from_json(c) for c in (row.conditions or [])),
        action=dict(row.action or {}),
    )


class PrivacyState:
    """Rules, profiles and local-model names for the request path."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        *,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._refresh_seconds = refresh_seconds
        self._task: asyncio.Task[None] | None = None
        self.rules: tuple[Rule, ...] = ()
        self.profiles: dict[str, Profile] = {}
        #: Names a caller sends, in order, of active local embedding models.
        self.local_embedding_models: tuple[str, ...] = ()
        #: Names of active models served by this deployment's own extractor.
        self.local_extractor_models: tuple[str, ...] = ()
        self.cache = VerdictCache()
        self.evaluator = Evaluator(self.profiles, self.cache)

    @property
    def configured(self) -> bool:
        """Whether any rule is on: the condition for logging every request."""
        return any(rule.enabled for rule in self.rules)

    def rules_of(self, kind: str) -> tuple[Rule, ...]:
        return tuple(r for r in self.rules if r.kind == kind)

    def uses_verdicts(self, kind: str) -> bool:
        """Whether an enabled rule of this kind needs the request's own text read.

        A classifier no rule uses is never called (ADR 0113, section 4).
        """
        return any(
            r.enabled and any(c.key in ("verdict", "lang") for c in r.conditions)
            for r in self.rules
            if r.kind == kind
        )

    def load(
        self,
        rules: list[PrivacyRule],
        profiles: list[DetectorProfile],
        embedding_models: list[str],
        extractor_models: list[str],
    ) -> None:
        self.rules = tuple(
            rule_from_row(r) for r in sorted(rules, key=lambda r: (r.kind, r.position))
        )
        self.profiles = profile_snapshot(
            profiles, {p.id: engine_available(p.engine) for p in profiles}
        )
        self.local_embedding_models = tuple(embedding_models)
        self.local_extractor_models = tuple(extractor_models)
        self.evaluator = Evaluator(self.profiles, self.cache)

    async def refresh(self) -> bool:
        """Re-read from the database. Never raises; True when it read."""
        if self._session_factory is None:
            return False
        try:
            async with self._session_factory() as session:
                self._apply(await read_all(session))
        except Exception:
            logger.warning("could not refresh the privacy routing rules", exc_info=True)
            return False
        return True

    def _apply(self, loaded: tuple[Any, ...]) -> None:
        self.load(*loaded)

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
                logger.warning("the privacy routing poller raised; continuing", exc_info=True)

    async def aclose(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task


async def read_all(
    session: AsyncSession,
) -> tuple[list[PrivacyRule], list[DetectorProfile], list[str], list[str]]:
    rules = list(
        (
            await session.execute(
                select(PrivacyRule).order_by(PrivacyRule.kind, PrivacyRule.position)
            )
        )
        .scalars()
        .all()
    )
    profiles = list(
        (await session.execute(select(DetectorProfile).order_by(DetectorProfile.name)))
        .scalars()
        .all()
    )
    return (
        rules,
        profiles,
        await _names(session, local_embedding=True),
        await _names(session, local_embedding=False),
    )


async def _names(session: AsyncSession, *, local_embedding: bool) -> list[str]:
    stmt = (
        select(ModelDef.name)
        .join(Provider, Provider.id == ModelDef.provider_id)
        .where(
            ModelDef.is_active.is_(True),
            ModelDef.owner_id.is_(None),
            Provider.is_active.is_(True),
            Provider.owner_id.is_(None),
        )
        .order_by(ModelDef.name)
    )
    if local_embedding:
        stmt = stmt.where(ModelDef.kind == ModelKind.EMBEDDING, Provider.is_local.is_(True))
    else:
        stmt = stmt.where(ModelDef.kind == ModelKind.OCR, Provider.plugin == "extractor")
    return [str(n) for n in (await session.execute(stmt)).scalars().all()]


_EMPTY = PrivacyState()


def state_of(request: Any) -> PrivacyState:
    """The worker's routing state; an empty one for an app built without it."""
    found = getattr(request.app.state, "privacy", None)
    return found if isinstance(found, PrivacyState) else _EMPTY
