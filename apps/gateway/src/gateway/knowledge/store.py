"""The retrieval contract, and the two implementations behind it.

ADR 0018 chose pgvector with the explicit warning that if pgvector-specific SQL
is written throughout, the "swap to Qdrant" it promises is a rewrite and the
decision was wasted. This module is where that promise is kept: three methods,
no SQL above them, and a score rather than a distance so a caller never has to
know which metric the backend used.

**Two implementations, and only one of them is the product.** ``PgVectorStore``
is what runs. ``ExactStore`` computes cosine similarity in Python over rows it
selects, because SQLite cannot express vector distance at all and the unit suite
runs on SQLite — so without it, nothing above this layer (ingestion, the ACL,
the API shape, the reindex) would be testable without a PostgreSQL. It is the
same bargain as ``InMemoryCounterStore``, and it carries the same caveat: it
proves the *callers* correct and proves nothing whatever about the SQL, which is
why `scripts/test_knowledge_live.py` exists.

Scores, not distances, and the reason is not cosmetic. pgvector's ``<=>`` is a
cosine *distance* in [0, 2]; every operator-facing threshold in every RAG system
is phrased as a similarity in [0, 1]. Converting at the boundary means a
threshold set in the console keeps its meaning if the backend changes, and that
nobody has to remember which direction is better.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

import sqlalchemy as sa
from sqlalchemy import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.models import KnowledgeChunk, KnowledgeDocument


@dataclass(frozen=True, slots=True)
class NewChunk:
    """One passage, ready to be stored, with the vector already paid for."""

    ordinal: int
    text: str
    token_count: int
    embedding: list[float]


@dataclass(frozen=True, slots=True)
class Hit:
    """One retrieved passage.

    Carries the document's title and ``source_ref`` because every caller needs
    them to cite the answer, and fetching them separately would be one query per
    hit — the round-trip budget this project pins in `test_query_counts.py` is
    the reason that is worth denormalising into the result rather than the table.
    """

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    ordinal: int
    text: str
    #: Cosine similarity, 1.0 identical, 0.0 orthogonal. Never a distance.
    score: float
    title: str
    source_ref: str | None


class VectorStore(Protocol):
    """What retrieval needs, and nothing else.

    Deliberately not a general "vector database" interface: there is no index
    management, no collection creation, no metric selection. The index is
    created by a migration and the metric is cosine, because a knowledge base
    that could be indexed under a different metric than it is queried with is a
    knowledge base that silently returns nonsense.
    """

    async def upsert(
        self,
        session: AsyncSession,
        *,
        knowledge_base_id: uuid.UUID,
        document_id: uuid.UUID,
        chunks: Sequence[NewChunk],
    ) -> int:
        """Replace this document's chunks with these. Returns how many landed."""
        ...

    async def search(
        self,
        session: AsyncSession,
        *,
        knowledge_base_id: uuid.UUID,
        dimensions: int,
        query: Sequence[float],
        limit: int,
        min_score: float = 0.0,
    ) -> list[Hit]:
        """The nearest passages in one base, best first."""
        ...

    async def delete_document(self, session: AsyncSession, document_id: uuid.UUID) -> int:
        """Forget one document's chunks. Returns how many were removed."""
        ...


async def _clear(session: AsyncSession, document_id: uuid.UUID) -> int:
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            sa.delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document_id)
        ),
    )
    return int(result.rowcount or 0)


async def _insert(
    session: AsyncSession,
    *,
    knowledge_base_id: uuid.UUID,
    document_id: uuid.UUID,
    chunks: Sequence[NewChunk],
) -> int:
    if not chunks:
        return 0
    session.add_all(
        [
            KnowledgeChunk(
                document_id=document_id,
                knowledge_base_id=knowledge_base_id,
                ordinal=chunk.ordinal,
                text=chunk.text,
                token_count=chunk.token_count,
                embedding=list(chunk.embedding),
                dimensions=len(chunk.embedding),
            )
            for chunk in chunks
        ]
    )
    await session.flush()
    return len(chunks)


class PgVectorStore:
    """Retrieval as a SQL query, which is the whole point of ADR 0018.

    The ranking expression is ``embedding::halfvec(N) <=> query::halfvec(N)``,
    and it is written this way to match the partial expression index that
    migration 0027 creates. Three measured facts sit behind that, none of them
    guessable:

    * HNSW refuses a ``vector`` column above **2000** dimensions, so the common
      3072-dimension model cannot be indexed as ``vector`` at all;
    * ``halfvec`` raises that ceiling to 4000, and the planner does choose a
      halfvec-cast index for a halfvec-cast ``ORDER BY``;
    * comparing two vectors of *different* length raises ``DataError`` rather
      than returning a number, which is why ``dimensions`` is in the WHERE
      clause and not merely in a comment.

    Ranking therefore happens in half precision while storage stays full
    precision. That costs nothing that matters for a top-k ordering, and it buys
    one query path instead of a branch at 2000 dimensions that no test would
    ever reach.
    """

    name = "pgvector"

    async def upsert(
        self,
        session: AsyncSession,
        *,
        knowledge_base_id: uuid.UUID,
        document_id: uuid.UUID,
        chunks: Sequence[NewChunk],
    ) -> int:
        await _clear(session, document_id)
        return await _insert(
            session,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            chunks=chunks,
        )

    async def search(
        self,
        session: AsyncSession,
        *,
        knowledge_base_id: uuid.UUID,
        dimensions: int,
        query: Sequence[float],
        limit: int,
        min_score: float = 0.0,
    ) -> list[Hit]:
        if len(query) != dimensions:
            # Refused rather than truncated or padded. A query embedded by a
            # different model than the base was indexed with cannot be answered,
            # and answering it approximately is worse than not answering.
            raise ValueError(f"query has {len(query)} dimensions, base is indexed at {dimensions}")
        literal = "[" + ",".join(repr(float(value)) for value in query) + "]"
        # `1 - distance` at the database rather than in Python, so that the
        # threshold is applied before LIMIT and the count is honest.
        sql = sa.text(
            """
            SELECT c.id            AS chunk_id,
                   c.document_id   AS document_id,
                   c.ordinal       AS ordinal,
                   c.text          AS text,
                   d.title         AS title,
                   d.source_ref    AS source_ref,
                   1 - (c.embedding::halfvec(:dims) <=> (:q)::halfvec(:dims)) AS score
              FROM knowledge_chunks c
              JOIN knowledge_documents d ON d.id = c.document_id
             WHERE c.knowledge_base_id = :base
               AND c.dimensions = :dims
             ORDER BY c.embedding::halfvec(:dims) <=> (:q)::halfvec(:dims)
             LIMIT :limit
            """
        )
        rows = (
            await session.execute(
                sql,
                {
                    "base": knowledge_base_id,
                    "dims": dimensions,
                    "q": literal,
                    "limit": limit,
                },
            )
        ).mappings()
        return [
            Hit(
                chunk_id=row["chunk_id"],
                document_id=row["document_id"],
                ordinal=row["ordinal"],
                text=row["text"],
                score=float(row["score"]),
                title=row["title"],
                source_ref=row["source_ref"],
            )
            for row in rows
            if float(row["score"]) >= min_score
        ]

    async def delete_document(self, session: AsyncSession, document_id: uuid.UUID) -> int:
        return await _clear(session, document_id)


class ExactStore:
    """Cosine similarity in Python, for dialects that cannot do it in SQL.

    Not a fallback anybody should deploy: it reads every chunk of the base into
    the process. It exists so that the suite can test ingestion, retrieval
    plumbing, the ACL and reindexing on SQLite, and so that a developer without
    a PostgreSQL can run the whole thing.

    It is *exact*, which makes it a useful oracle: a live script can compare its
    ranking against pgvector's approximate one and see how much recall the index
    is costing. That is the only reason to keep it once PostgreSQL is the only
    supported deployment.
    """

    name = "exact"

    async def upsert(
        self,
        session: AsyncSession,
        *,
        knowledge_base_id: uuid.UUID,
        document_id: uuid.UUID,
        chunks: Sequence[NewChunk],
    ) -> int:
        await _clear(session, document_id)
        return await _insert(
            session,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            chunks=chunks,
        )

    async def search(
        self,
        session: AsyncSession,
        *,
        knowledge_base_id: uuid.UUID,
        dimensions: int,
        query: Sequence[float],
        limit: int,
        min_score: float = 0.0,
    ) -> list[Hit]:
        if len(query) != dimensions:
            raise ValueError(f"query has {len(query)} dimensions, base is indexed at {dimensions}")
        rows = (
            await session.execute(
                sa.select(
                    KnowledgeChunk.id,
                    KnowledgeChunk.document_id,
                    KnowledgeChunk.ordinal,
                    KnowledgeChunk.text,
                    KnowledgeChunk.embedding,
                    KnowledgeDocument.title,
                    KnowledgeDocument.source_ref,
                )
                .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
                .where(
                    KnowledgeChunk.knowledge_base_id == knowledge_base_id,
                    KnowledgeChunk.dimensions == dimensions,
                )
            )
        ).all()

        query_norm = math.sqrt(sum(value * value for value in query))
        hits: list[Hit] = []
        for row in rows:
            vector = row.embedding
            if not vector or len(vector) != dimensions:
                continue
            norm = math.sqrt(sum(value * value for value in vector))
            if norm == 0.0 or query_norm == 0.0:
                continue
            dot = sum(a * b for a, b in zip(vector, query, strict=True))
            score = dot / (norm * query_norm)
            if score < min_score:
                continue
            hits.append(
                Hit(
                    chunk_id=row.id,
                    document_id=row.document_id,
                    ordinal=row.ordinal,
                    text=row.text,
                    score=score,
                    title=row.title,
                    source_ref=row.source_ref,
                )
            )
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:limit]

    async def delete_document(self, session: AsyncSession, document_id: uuid.UUID) -> int:
        return await _clear(session, document_id)


def store_for(dialect: str) -> VectorStore:
    """The store this dialect can actually run.

    Chosen from the dialect rather than configured, because it is not a choice:
    PostgreSQL has the index and SQLite has no distance operator. When there is
    a second *real* backend — Qdrant, the reason this contract exists — it will
    be a configuration decision and this function is where it lands.
    """
    if dialect == "postgresql":
        return PgVectorStore()
    return ExactStore()
