# 0018 — Vector store: pgvector first, Qdrant behind an interface

- Status: accepted (decision recorded; **not implemented** — Phase 3)
- Date: 2026-08-14

## Context

The assumption was Qdrant, with an explicit request to compare it against pgvector given
we already run PostgreSQL.

## Findings (verified 2026-08-14)

| | Qdrant | pgvector |
|---|---|---|
| Licence | **Apache-2.0** (verified at the repo) | LICENSE file present; widely reported as the PostgreSQL Licence — **not read directly this session** |
| Current | v1.13.x referenced in mid-2026 benchmarks | README references **v0.8.6**; 0.8.2 fixed **CVE-2026-3172**, a buffer overflow in parallel HNSW index builds |
| Open-core | Qdrant Cloud is managed hosting; no enterprise-only features found in the OSS repo | n/a |

On performance, the published comparisons **contradict each other**, which is itself the
finding: one set reports Qdrant with the lowest latency (~4ms p50) and better raw
throughput at scale; a 2026 Tigerdata benchmark reports pgvectorscale at 471 QPS versus
Qdrant at 41 QPS at 99% recall on 50M vectors. These are vendor-adjacent and should be
treated as directional only. Do not choose on them.

## Decision

**pgvector first**, with retrieval behind an interface so Qdrant is a swap rather than a
rewrite. This argues against the original assumption.

Reasoning that does not depend on contested benchmarks:

1. **We already run PostgreSQL.** Qdrant means a second stateful service to operate,
   secure, back up, upgrade and monitor — for a small foundation that is a real ongoing
   cost, paid every week.
2. **Transactional consistency** between chunks and their document metadata. With a
   separate vector store, an ingestion that half-fails leaves vectors without metadata or
   vice versa, and reconciling them is application code nobody wants to write.
3. One backup story, one restore story, one connection pool.

**Decide the switch on corpus size, not on vibes.** pgvector is comfortable below roughly
5–10M chunks. Above that, or if p50 latency becomes a user-visible complaint, move to
Qdrant — its Apache-2.0 licence means there is no licensing obstacle to doing so.

## Consequences

- **Pin pgvector >= 0.8.2** for CVE-2026-3172. Current appears to be 0.8.6.
- Verify pgvector's licence file before it enters the tree ([0001](0001-licensing.md)).
- Use `halfvec` (2-byte floats) where recall permits: roughly half the storage, indexable
  to 4000 dimensions.
- The retrieval interface must not leak SQL. If `services/rag` builds pgvector-specific
  queries throughout, the "swap" is a rewrite and this ADR was wasted.
- `pgvectorscale` (Timescale) is the obvious next step before abandoning PostgreSQL; its
  licence needs checking, as Timescale has used the non-OSI TSL for some components.
