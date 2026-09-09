"""What the knowledge pipeline is configured to do, kept current without a query.

The third resolver of this shape, after `redaction/resolver.py` (ADR 0033) and
`oidc_policy.py` (ADR 0048), and it exists for the reason those do: the
alternative spellings are both wrong. A ``SELECT`` per request would blow the
round-trip budget `test_query_counts.py` pins — and this one would be read on
every retrieval, which is a request path. Requiring a restart would mean
"restart the gateway" is the answer to "change the embedding model", which is
the wrong sentence to say during an incident.

So: read once before serving, then poll. A change reaches the other worker
within ``refresh_seconds`` and the API reports that number, because a change
that is not instant should say so rather than leave an administrator refreshing
a screen.

**Three differences from the redaction resolver**, all of them consequences of
what this configures rather than of style.

It resolves to **plain data, not to a live object**. The redaction resolver
rebuilds a redactor that owns an HTTP pool and a detection cache, so it compares
by row id to avoid throwing those away every ten seconds. A ``KnowledgeProfile``
is five values; rebuilding it is free, and there is nothing to keep.

It **never affects an existing index**. Everything here is a default for the
*next* base created or the *next* document ingested. A base pins its own
embedding model and chunk geometry at creation precisely so that this poll
cannot change the meaning of vectors already written — which is what makes
changing the default a safe operation rather than a data migration.

And it **can legitimately resolve to nothing**. A deployment with the feature
enabled but no embedding model chosen is not misconfigured, it is unfinished;
``embedding_model_id`` is None and the API says which decision is missing.
Falling back to "some embedding model" would pick one at random and index a
corpus with it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.config import Settings
from gateway.models import KnowledgeConfig

logger = logging.getLogger(__name__)

#: Matches the redaction resolver's, and for the same reason: fast enough that
#: an administrator does not wonder whether the save worked, slow enough to be
#: free.
DEFAULT_REFRESH_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class KnowledgeProfile:
    """The pipeline's settings as one immutable snapshot.

    Read as a whole rather than field by field, so that a poll landing between
    two reads cannot serve half of one configuration and half of the next — the
    sort of inconsistency that would put one document's chunks at two sizes.
    """

    enabled: bool
    embedding_model_id: uuid.UUID | None
    extractor_model_id: uuid.UUID | None
    vector_store: str
    chunk_chars: int
    chunk_overlap: int
    #: ``console`` when a row is in force, ``environment`` when none is. On the
    #: admin screen because the two disagreeing is otherwise invisible, and
    #: that confusion is exactly what a database override introduces.
    source: str
    #: Which row, so that "this is the same decision, saved twice" and "this is
    #: a new decision" are distinguishable in a log.
    config_id: uuid.UUID | None

    @property
    def ready(self) -> bool:
        """Whether ingestion can actually run.

        Enabled but with no embedding model is *unfinished*, not broken, and the
        difference matters: the API says which decision is missing rather than
        answering 503 as though something had failed.
        """
        return self.enabled and self.embedding_model_id is not None


async def current_config(session: AsyncSession) -> KnowledgeConfig | None:
    """The newest row, or None when the environment decides."""
    result = await session.execute(
        select(KnowledgeConfig).order_by(KnowledgeConfig.created_at.desc()).limit(1)
    )
    return result.scalars().first()


def _from_environment(settings: Settings) -> KnowledgeProfile:
    return KnowledgeProfile(
        enabled=settings.knowledge.enabled,
        embedding_model_id=None,
        extractor_model_id=None,
        vector_store="pgvector",
        chunk_chars=settings.knowledge.chunk_chars,
        chunk_overlap=settings.knowledge.chunk_overlap,
        source="environment",
        config_id=None,
    )


def _merge(base: KnowledgeProfile, row: KnowledgeConfig) -> KnowledgeProfile:
    """Apply a row's decisions over the environment's defaults.

    A null column means **"this row does not decide"**, never "off" — so an
    administrator setting only the embedding model does not thereby reset the
    chunk geometry to a default they never chose. That is the ``oidc_config``
    rule, and getting it wrong here would silently re-chunk every base created
    after the save.
    """
    return replace(
        base,
        embedding_model_id=row.embedding_model_id,
        extractor_model_id=row.extractor_model_id,
        vector_store=row.vector_store or base.vector_store,
        chunk_chars=row.chunk_chars if row.chunk_chars is not None else base.chunk_chars,
        chunk_overlap=(row.chunk_overlap if row.chunk_overlap is not None else base.chunk_overlap),
        source="console",
        config_id=row.id,
    )


class KnowledgeResolver:
    """Holds the current profile and keeps it current.

    The request path reads ``resolver.profile``, one attribute lookup, exactly
    as it reads ``app.state.redactor``. Nothing on a retrieval touches the
    database to find out how it is configured.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._refresh_seconds = refresh_seconds
        self._profile = _from_environment(settings)
        self._task: asyncio.Task[None] | None = None

    @property
    def profile(self) -> KnowledgeProfile:
        return self._profile

    @property
    def refresh_seconds(self) -> float:
        return self._refresh_seconds

    async def refresh(self) -> bool:
        """Re-read the row. True if anything changed.

        Never raises. A database blip leaves the profile in force exactly as it
        was — the same rule the redaction resolver follows, and for a sharper
        reason here: failing open to "no embedding model" would make every
        upload fail, and failing open to a *different* model would index part of
        a corpus incomparably with the rest.
        """
        try:
            async with self._session_factory() as session:
                row = await current_config(session)
        except Exception:
            logger.warning("could not read the knowledge configuration", exc_info=True)
            return False

        base = _from_environment(self._settings)
        profile = base if row is None else _merge(base, row)
        if profile == self._profile:
            return False

        previous = self._profile
        self._profile = profile
        if previous.embedding_model_id != profile.embedding_model_id:
            # Logged at info and named explicitly, because this is the change
            # that makes existing indexes incomparable with new ones and the
            # only warning anyone gets is this line and the reindex button.
            logger.info(
                "knowledge embedding model changed: %s -> %s (existing bases keep "
                "their own model until reindexed)",
                previous.embedding_model_id,
                profile.embedding_model_id,
            )
        else:
            logger.info("knowledge configuration reloaded from %s", profile.source)
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
                # `refresh` swallows its own failures, so anything reaching here
                # is a bug in the loop rather than in the read. A dead poller
                # means a saved change that never lands, silently.
                logger.warning("the knowledge poller raised; continuing", exc_info=True)

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
