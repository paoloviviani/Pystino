"""Extract, chunk, embed, store — and pay for it on the way.

This runs in a **detached task**, not in a request. ADR 0019 is explicit that
OCR is expensive and slow and belongs in an ingestion worker; a 200-page PDF
held open in a request would occupy one of two uvicorn workers for minutes.
There is no queue service, so the task is an ``asyncio`` task with its own
session and a strong reference held on ``app.state.background_tasks`` — the same
mechanism `chat.py` uses for detached settlement (ADR 0060), for the same reason
it is needed there: a task nobody holds is a task the garbage collector may
collect mid-write.

The consequence a caller sees is that ``POST`` returns immediately with
``status: pending`` and the document's status is the progress bar. That is also
how OpenAI's own vector-store file API behaves, so a client written against it
needs no special case.

**Every stage is metered through `_metered`.** Extraction bills pages,
embedding bills tokens, and both go through the same reserve → record → settle
pipeline as a chat completion, using the same function — which is why `begin`
now takes ``fx`` and ``session_factory`` instead of a request. ADR 0020's
sharpest consequence is that indexing is billable and a large ingestion run can
cost more than the chat traffic it serves; a second, private copy of the money
code would make that spend invisible to exactly the reports built to catch it.

**What redaction does here is a decision, not a default.** A knowledge base is
the first durable store of user text in this deployment, so the question "is the
stored text redacted" has to be answered rather than inherited.

* The text **sent to the embedding provider is redacted**, because that is an
  egress and every egress is redacted. This works precisely because
  placeholders are deterministic: an indexed document and a later query
  substitute the same entity to the same placeholder, so they still match.
  `embeddings.py` records that property; this is the thing it was for.
* The text **stored in the chunk is what was extracted**, unredacted. Storing
  placeholders instead would be security theatre while ``file_blobs`` holds the
  original document three tables away — and it would hand every reader
  ``<PERSON_a1b2>`` in place of a name they are entitled to see in their own
  file. Retrieval hands that text back to a caller who already owns it, and the
  moment it heads for a model it passes through the ordinary redaction on
  ``/v1/chat/completions``. The protection is at the boundary, where it already
  lives, rather than duplicated into the store.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload, selectinload

from gateway.accounting import TokenCounts
from gateway.accounting.tokens import TokenEstimator
from gateway.config import Settings
from gateway.deps import Principal
from gateway.extraction import DocumentRefused, as_ocr_response, extract_document
from gateway.fx import FXService
from gateway.knowledge.chunking import chunk_markdown
from gateway.knowledge.resolver import KnowledgeProfile
from gateway.knowledge.store import NewChunk, VectorStore, store_for
from gateway.models import (
    ApiSurface,
    FileBlob,
    Group,
    IndexStatus,
    KnowledgeBase,
    KnowledgeDocument,
    ModelDef,
    StoredFile,
    User,
)
from gateway.providers import ProviderRegistry
from gateway.quota import QuotaEngine
from gateway.redaction.base import Redactor
from gateway.routers import _metered
from gateway.types import utcnow
from gateway.upstream import UpstreamError

logger = logging.getLogger(__name__)

#: The local extractor's plugin name, as `ocr.py` defines it. Duplicated as a
#: constant rather than imported from the router, because a background task
#: importing a router module is how import cycles start.
LOCAL_EXTRACTOR = "extractor"


class IngestionFailed(Exception):
    """Something went wrong that the document row should record and report.

    Raised for conditions a caller can act on — no embedding model configured,
    an extractor that found no text layer, a provider that refused. Anything
    *not* of this type is a bug and is logged with a traceback before the
    document is failed, because "unexpected error" in a status field with no
    stack trace anywhere is unactionable.
    """


@dataclass(slots=True)
class Ingestor:
    """Everything ingestion needs that a request would otherwise have supplied.

    Assembled once at startup and held on ``app.state``, so a detached task
    never reaches for a request that has already finished.
    """

    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    quota: QuotaEngine
    estimator: TokenEstimator
    fx: FXService
    providers: ProviderRegistry
    #: The control-plane client, the same one `/v1/ocr` hands the extractor.
    #: Not the upstream client: that one has no read timeout so long streams
    #: survive, and a hung extractor must not hang an ingestion forever.
    control_http: httpx.AsyncClient
    redactor: Redactor
    background_tasks: set[asyncio.Task[None]]

    def spawn(self, document_id: uuid.UUID, profile: KnowledgeProfile) -> None:
        """Start ingesting, detached, and keep a reference to the task.

        The reference is the point. ``asyncio`` holds only a weak one, so a task
        nobody keeps can be collected between awaits — which here would abandon
        a document in ``extracting`` forever with no error to show for it.
        """
        task = asyncio.create_task(self._run(document_id, profile))
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    async def _run(self, document_id: uuid.UUID, profile: KnowledgeProfile) -> None:
        try:
            await self.ingest(document_id, profile)
        except IngestionFailed as exc:
            await self._fail(document_id, str(exc))
        except asyncio.CancelledError:
            # Shutdown. Leave the row in whatever stage it reached: `pending`
            # and `extracting` are both re-runnable, and writing "cancelled"
            # into an error field would look like a fault rather than a restart.
            raise
        except Exception:
            logger.exception("ingestion of document %s raised", document_id)
            await self._fail(document_id, "An unexpected error occurred while indexing.")

    async def _fail(self, document_id: uuid.UUID, message: str) -> None:
        """Record the failure on the row, in its own session.

        Its own session because the one that raised may be in a failed
        transaction, and the whole value of a status column is that it survives
        the thing that broke.
        """
        try:
            async with self.session_factory() as session:
                document = await session.get(KnowledgeDocument, document_id)
                if document is None:
                    return
                document.status = IndexStatus.FAILED
                document.error = message[:500]
                await session.commit()
        except Exception:
            logger.exception("could not record the ingestion failure for %s", document_id)

    # -- the pipeline ------------------------------------------------------

    async def ingest(self, document_id: uuid.UUID, profile: KnowledgeProfile) -> None:
        """Take one document from ``pending`` to ``ready``."""
        async with self.session_factory() as session:
            document = await self._load_document(session, document_id)
            base = document.knowledge_base
            owner, group = await self._billing(session, base)

            text = document.text
            if text is None:
                document.status = IndexStatus.EXTRACTING
                await session.commit()
                text = await self._extract(session, document, base, owner, group, profile)
                document.text = text
                document.text_sha256 = hashlib.sha256(text.encode()).hexdigest()
                document.chars = len(text)
                await session.commit()

            if not text.strip():
                # Never a silent success. An empty document that reports
                # `ready` is indistinguishable from one that indexed fine and
                # simply never matches anything.
                raise IngestionFailed(
                    "No text could be extracted from this document. If it is a scan, "
                    "index it with an OCR model rather than the built-in extractor."
                )

            document.status = IndexStatus.EMBEDDING
            await session.commit()

            chunks = chunk_markdown(
                text,
                chunk_chars=base.chunk_chars,
                chunk_overlap=base.chunk_overlap,
            )
            if not chunks:
                raise IngestionFailed("The document produced no passages to index.")

            embedded = await self._embed(
                session,
                base=base,
                owner=owner,
                group=group,
                profile=profile,
                texts=[chunk.text for chunk in chunks],
            )

            store: VectorStore = store_for(session.bind.dialect.name if session.bind else "sqlite")
            written = await store.upsert(
                session,
                knowledge_base_id=base.id,
                document_id=document.id,
                chunks=embedded,
            )

            # The base learns its dimensionality from the first vector that ever
            # lands in it, and never from a number anyone typed.
            dimensions = len(embedded[0].embedding) if embedded else None
            if dimensions is not None and base.dimensions != dimensions:
                base.dimensions = dimensions
            if base.embedding_model_id is None:
                base.embedding_model_id = profile.embedding_model_id

            document.chunk_count = written
            document.status = IndexStatus.READY
            document.error = ""
            document.indexed_at = utcnow()
            await session.commit()
            logger.info(
                "indexed document %s into base %s: %d passages, %d dimensions",
                document.id,
                base.id,
                written,
                dimensions or 0,
            )

    async def _load_document(
        self, session: AsyncSession, document_id: uuid.UUID
    ) -> KnowledgeDocument:
        result = await session.execute(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.id == document_id)
            .options(joinedload(KnowledgeDocument.knowledge_base))
        )
        document = result.scalars().first()
        if document is None:
            raise IngestionFailed("The document no longer exists.")
        return document

    async def _billing(
        self, session: AsyncSession, base: KnowledgeBase
    ) -> tuple[User, Group]:
        """Who pays for indexing this base.

        The base's owner and the group it was created under, not whoever
        triggered the ingestion — a reindex started by an administrator must
        still bill the group whose corpus it is, or an admin clicking "reindex"
        moves somebody else's spend onto their own budget.
        """
        owner = await session.get(User, base.owner_user_id)
        if owner is None:
            raise IngestionFailed("The owner of this knowledge base no longer exists.")
        group: Group | None = None
        if base.billing_group_id is not None:
            group = await session.get(Group, base.billing_group_id)
        if group is None and owner.default_billing_group_id is not None:
            group = await session.get(Group, owner.default_billing_group_id)
        if group is None:
            raise IngestionFailed(
                "This knowledge base has no billing group, so indexing it could not "
                "be accounted for and was refused."
            )
        return owner, group

    async def _model(self, session: AsyncSession, model_id: uuid.UUID) -> ModelDef:
        result = await session.execute(
            select(ModelDef)
            .where(ModelDef.id == model_id)
            .options(selectinload(ModelDef.prices), joinedload(ModelDef.provider))
        )
        model = result.scalars().first()
        if model is None:
            raise IngestionFailed("The configured model no longer exists.")
        if not model.is_active:
            raise IngestionFailed(f"The model {model.name!r} has been deactivated.")
        return model

    async def _begin(
        self,
        session: AsyncSession,
        *,
        owner: User,
        group: Group,
        model: ModelDef,
        surface: ApiSurface,
        worst_case: TokenCounts,
    ) -> _metered.Metered:
        """Reserve and open a ledger row for one stage of ingestion.

        A quota refusal comes back from `begin` as a ``JSONResponse``, because
        on a request path that is the right answer. There is no response to
        return here, so it becomes a failure recorded on the document — an
        ingestion refused for budget is exactly the thing an operator needs to
        see spelled out, and a half-indexed document is worse than none.
        """
        metered = await _metered.begin(
            fx=self.fx,
            session_factory=self.session_factory,
            session=session,
            principal=Principal(user=owner, billing_group=group),
            settings=self.settings,
            quota=self.quota,
            estimator=self.estimator,
            model=model,
            surface=surface,
            request_id=f"ingest-{uuid.uuid4().hex[:16]}",
            worst_case=worst_case,
            streamed=False,
        )
        if not isinstance(metered, _metered.Metered):
            raise IngestionFailed(
                "Indexing was refused because the billing group is over its quota. "
                "Raise the limit or wait for the window to roll, then reindex."
            )
        return metered

    async def _extract(
        self,
        session: AsyncSession,
        document: KnowledgeDocument,
        base: KnowledgeBase,
        owner: User,
        group: Group,
        profile: KnowledgeProfile,
    ) -> str:
        """Turn the stored bytes into markdown, billed per page."""
        if document.file_id is None:
            raise IngestionFailed(
                "This document has no text and no file to extract it from."
            )
        blob = await session.get(FileBlob, document.file_id)
        stored = await session.get(StoredFile, document.file_id)
        if blob is None or stored is None:
            raise IngestionFailed(
                "The uploaded file has been deleted, so its text cannot be "
                "extracted again."
            )

        model_id = base.extractor_model_id or profile.extractor_model_id
        if model_id is None:
            # The built-in extractor is unpriced and has no model row unless the
            # deployment imported one, so this path bills nothing and records
            # nothing. That is correct rather than a gap: `markitdown` runs on
            # our own hardware, and inventing a ledger row for it would put a
            # zero-cost line in every report.
            return await self._extract_locally(blob.data, stored.media_type, stored.filename)

        model = await self._model(session, model_id)
        if model.provider is not None and model.provider.plugin == LOCAL_EXTRACTOR:
            metered = await self._begin(
                session,
                owner=owner,
                group=group,
                model=model,
                surface=ApiSurface.OCR,
                # A floor, not a worst case — the same admission `ocr.py` makes:
                # the page count is not known until the document is parsed.
                worst_case=TokenCounts(pages=1),
            )
            try:
                text, pages = await self._extract_locally_counted(
                    blob.data, stored.media_type, stored.filename
                )
            except IngestionFailed:
                await metered.completed(upstream_status=422)
                raise
            metered.accounting.observe_payload(
                {"usage_info": {"pages_processed": pages}}, headers={}
            )
            await metered.completed(upstream_status=200)
            document.pages = pages
            return text

        return await self._extract_upstream(
            session,
            document=document,
            owner=owner,
            group=group,
            model=model,
            data=blob.data,
            media_type=stored.media_type,
        )

    async def _extract_locally(self, data: bytes, media_type: str, filename: str) -> str:
        text, _pages = await self._extract_locally_counted(data, media_type, filename)
        return text

    async def _extract_locally_counted(
        self, data: bytes, media_type: str, filename: str
    ) -> tuple[str, int]:
        """The built-in extractor, which never sends the document anywhere."""
        try:
            outcome = await extract_document(
                self.control_http,
                self.settings.extractor.endpoint,
                data,
                media_type=media_type,
                filename=filename,
            )
        except UpstreamError as exc:
            raise IngestionFailed(
                f"The document extractor could not be reached: {exc}"
            ) from exc

        # `as_ocr_response` is called for its *refusal* semantics rather than
        # its shape: it is the single place that knows what each non-text
        # outcome means for a caller — a scan needs an OCR model, an unknown
        # format needs converting — and duplicating that table here is how the
        # two would drift into giving different advice for the same document.
        try:
            as_ocr_response(outcome, model_name="markitdown")
        except DocumentRefused as exc:
            raise IngestionFailed(str(exc)) from exc
        return outcome.text, outcome.pages

    async def _extract_upstream(
        self,
        session: AsyncSession,
        *,
        document: KnowledgeDocument,
        owner: User,
        group: Group,
        model: ModelDef,
        data: bytes,
        media_type: str,
    ) -> str:
        """Extract through a provider's OCR model, billed per page."""
        import base64

        upstream = await _metered.resolve_upstream(self.providers, model)
        metered = await self._begin(
            session,
            owner=owner,
            group=group,
            model=model,
            surface=ApiSurface.OCR,
            worst_case=TokenCounts(pages=1),
        )
        encoded = base64.b64encode(data).decode()
        payload: dict[str, Any] = {
            "model": model.upstream_model,
            "document": {
                "type": "document_url",
                "document_url": f"data:{media_type};base64,{encoded}",
            },
        }
        try:
            response = await upstream.ocr(payload, request_id=metered.accounting.request_id)
        except Exception as exc:
            await metered.completed(upstream_status=None)
            raise IngestionFailed(f"The OCR provider could not be reached: {exc}") from exc

        if response.status_code >= 400:
            await metered.completed(upstream_status=response.status_code)
            raise IngestionFailed(
                f"The OCR provider refused the document ({response.status_code}): "
                f"{_metered.error_text(response.payload) or 'no detail'}"
            )

        if response.payload is not None:
            metered.accounting.observe_payload(response.payload, headers=response.headers)
        await metered.completed(upstream_status=response.status_code)

        pages = list((response.payload or {}).get("pages") or [])
        document.pages = len(pages)
        # Joined with a rule rather than a blank line: a page boundary is a
        # structural break, and the chunker splits on headings and paragraphs.
        return "\n\n".join(str(page.get("markdown") or "") for page in pages)

    async def _embed(
        self,
        session: AsyncSession,
        *,
        base: KnowledgeBase,
        owner: User,
        group: Group,
        profile: KnowledgeProfile,
        texts: list[str],
    ) -> list[NewChunk]:
        """Embed every passage, in batches, billed per token."""
        model_id = base.embedding_model_id or profile.embedding_model_id
        if model_id is None:
            raise IngestionFailed(
                "No embedding model has been configured for this deployment. An "
                "administrator sets one on the Knowledge screen."
            )
        model = await self._model(session, model_id)
        upstream = await _metered.resolve_upstream(self.providers, model)

        batch_size = max(1, self.settings.knowledge.embed_batch)
        out: list[NewChunk] = []
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            vectors = await self._embed_batch(
                session,
                owner=owner,
                group=group,
                model=model,
                upstream=upstream,
                batch=batch,
            )
            for offset, (text, vector) in enumerate(zip(batch, vectors, strict=True)):
                out.append(
                    NewChunk(
                        ordinal=start + offset,
                        text=text,
                        token_count=self.estimator.count_text(text),
                        embedding=vector,
                    )
                )
        return out

    async def _embed_batch(
        self,
        session: AsyncSession,
        *,
        owner: User,
        group: Group,
        model: ModelDef,
        upstream: Any,
        batch: list[str],
    ) -> list[list[float]]:
        # Redacted on the way out, exactly as `/v1/embeddings` does it, and for
        # the same reason: this is an egress. Deterministic placeholders are
        # what make it safe — the query will substitute identically, so the
        # vectors still compare.
        redacted: list[str] = []
        for text in batch:
            # One message per passage, exactly as `/v1/embeddings` shapes it, so
            # one engine serves both paths and a policy change reaches indexing
            # as well as querying.
            outcome = await self.redactor.redact_request([{"role": "user", "content": text}])
            redacted.append(str(outcome.messages[0].get("content") or ""))
        prompt_tokens = sum(self.estimator.count_text(text) for text in redacted)
        metered = await self._begin(
            session,
            owner=owner,
            group=group,
            model=model,
            surface=ApiSurface.EMBEDDINGS,
            worst_case=TokenCounts(prompt=prompt_tokens, completion=0),
        )
        payload: dict[str, Any] = {"model": model.upstream_model, "input": redacted}
        payload = metered.shape_payload(payload, surface=ApiSurface.EMBEDDINGS)
        try:
            response = await upstream.embeddings(
                payload, request_id=metered.accounting.request_id
            )
        except Exception as exc:
            await metered.completed(upstream_status=None)
            raise IngestionFailed(
                f"The embedding provider could not be reached: {exc}"
            ) from exc

        if response.status_code >= 400:
            await metered.completed(upstream_status=response.status_code)
            raise IngestionFailed(
                f"The embedding provider refused the request ({response.status_code}): "
                f"{_metered.error_text(response.payload) or 'no detail'}"
            )
        if response.payload is not None:
            metered.accounting.observe_payload(response.payload, headers=response.headers)
        await metered.completed(upstream_status=response.status_code)

        data = list((response.payload or {}).get("data") or [])
        if len(data) != len(batch):
            # A provider returning a different number of vectors than we sent
            # texts would silently misalign every passage with somebody else's
            # vector, which is unrecoverable and undetectable later.
            raise IngestionFailed(
                f"The embedding provider returned {len(data)} vectors for "
                f"{len(batch)} passages."
            )
        # Sorted by the provider's own index rather than trusted in order: the
        # OpenAI shape carries one, and a provider that batches internally is
        # entitled to answer out of order.
        data.sort(key=lambda item: int(item.get("index", 0)))
        vectors: list[list[float]] = []
        for item in data:
            vector = item.get("embedding")
            if not isinstance(vector, list) or not vector:
                raise IngestionFailed("The embedding provider returned an empty vector.")
            vectors.append([float(value) for value in vector])
        widths = {len(vector) for vector in vectors}
        if len(widths) > 1:
            raise IngestionFailed(
                f"The embedding provider returned vectors of differing lengths: {sorted(widths)}."
            )
        return vectors


    async def embed_query(
        self,
        session: AsyncSession,
        *,
        base: KnowledgeBase,
        owner: User,
        group: Group,
        text: str,
    ) -> list[float]:
        """Embed one search query, with the model the base was indexed with.

        **The base's model, never the deployment's current default.** If an
        administrator has changed the default and this base has not been
        reindexed, embedding the query with the new model would compare vectors
        from two different models — which does not degrade retrieval, it makes
        it meaningless. PostgreSQL would refuse outright on a dimension change
        and silently mis-rank on a same-dimension change, and the second is the
        dangerous one.

        Billed to the base's group like indexing is, and for the same reason: a
        search costs embedding tokens, and a search nobody is charged for is a
        search that does not appear in the report that would explain the bill.
        """
        model_id = base.embedding_model_id
        if model_id is None:
            raise IngestionFailed(
                "This knowledge base has not been indexed yet, so it cannot be searched."
            )
        model = await self._model(session, model_id)
        upstream = await _metered.resolve_upstream(self.providers, model)
        vectors = await self._embed_batch(
            session, owner=owner, group=group, model=model, upstream=upstream, batch=[text]
        )
        return vectors[0]

    async def billing_for(
        self, session: AsyncSession, base: KnowledgeBase
    ) -> tuple[User, Group]:
        """Public spelling of `_billing`, for the search path."""
        return await self._billing(session, base)


def stale_since(document: KnowledgeDocument, profile: KnowledgeProfile) -> datetime | None:
    """When this document stopped matching the deployment's configuration.

    Used by the console to say *which* documents a reindex would change, rather
    than offering a button that might do nothing. A document is stale when the
    base it is in was indexed with a different embedding model than the one now
    configured — not when the chunk geometry changed, because the base
    snapshots its own geometry and keeps using it.
    """
    base = document.knowledge_base
    if base.embedding_model_id is None or profile.embedding_model_id is None:
        return None
    if base.embedding_model_id == profile.embedding_model_id:
        return None
    return document.indexed_at or datetime.now(UTC)
