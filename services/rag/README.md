# RAG indexing and retrieval — placeholder

**Nothing is built here yet.** Phase 3.

## What goes here

Document ingestion and retrieval: chunking, embedding, indexing, and a retrieval
API the frontend can configure. Open WebUI's *degree of configurability* is the
reference for scope — search mode, OCR endpoint, embedding endpoint, reranking all
being operator-choosable rather than hardcoded.

## Decisions already taken

| Choice | Decision | ADR |
|---|---|---|
| Vector store | pgvector first; retrieval behind an interface so Qdrant is a swap | [0018](../../docs/adr/0018-vector-store.md) |
| Document conversion / OCR | A configurable HTTP endpoint. Docling (`docling-serve`) is the reference implementation, not a hardcoded dependency. | [0019](../../docs/adr/0019-document-conversion.md) |
| Embeddings and reranking | Configurable OpenAI-compatible endpoints. No bundled model. | [0020](../../docs/adr/0020-embeddings-and-reranking.md) |

Pin **pgvector >= 0.8.2**: 0.8.2 fixed CVE-2026-3172, a buffer overflow in
parallel HNSW index builds.

## Things to get right, learned from the gateway

- **Embeddings are billable.** Route them through the gateway's accounting rather
  than calling an embedding endpoint directly, or indexing spend becomes invisible
  — a large ingestion run can cost more than the chat traffic it serves.
- **Model-weight licences stay outside this tree.** Docling core is MIT (LF AI &
  Data), but individual models carry their own licences, and the faster
  alternatives are worse: Marker is GPL-3.0 code plus RAIL-M weights with a
  revenue restriction, MinerU has income-threshold conditions. Neither is OSI. The
  HTTP-endpoint design is what keeps that decision the operator's rather than ours.
- Chunk metadata belongs in PostgreSQL alongside the vectors while we are on
  pgvector; that transactional consistency is most of the reason for choosing it.
