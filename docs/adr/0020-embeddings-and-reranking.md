# 0020 — Embeddings and reranking via configurable OpenAI-compatible endpoints

- Status: accepted (decision recorded; **not implemented** — Phase 3)
- Date: 2026-08-14

## Context

The assumption was configurable OpenAI-compatible endpoints for embeddings and
reranking, with no bundled model.

## Decision

**Validated, and strongly.** This was the right call and needs little argument:

- Configurable OpenAI-compatible endpoints for both embeddings and reranking.
- **No bundled model, no bundled inference runtime.**

Three reasons worth writing down:

1. **It sidesteps every model-weight licence question.** Bundling an embedding model
   means auditing that model's terms and re-auditing on every upgrade. Not bundling means
   the operator chooses, and [0001](0001-licensing.md) never has to be consulted.
2. **It avoids putting a GPU inference runtime inside our deployment.** A research
   foundation may or may not have GPUs, and the same platform must work either way — a
   hosted endpoint, a local vLLM, or a CPU model behind a small service, all interchangeable.
3. The gateway already speaks this protocol, so embeddings can be routed **through** it and
   therefore accounted for.

## Consequences

- **Route embeddings through the gateway, not around it.** They are billable, and a large
  ingestion run can cost more than the chat traffic it serves. Calling an embedding
  endpoint directly makes indexing spend invisible to every quota and report.
  - This implies Phase 3 adds `POST /v1/embeddings` to the gateway, with the same
    accounting and quota path as chat completions. That endpoint does not exist yet.
- **Changing the embedding model invalidates the index.** Vectors from different models are
  not comparable. The configured model name must be stored *with* each index so a mismatch
  is detectable rather than silently returning nonsense results. This is the single most
  common way a RAG system starts quietly returning garbage.
- Reranking is a separate endpoint and a separate cost, and there is no OpenAI standard for
  it — providers differ (Cohere-style `/rerank`, Jina, local cross-encoders). The interface
  must accommodate that, and it means reranking is *less* portable than embeddings, not more.
- Dimension count is a schema concern for pgvector ([0018](0018-vector-store.md)): the
  column is typed with a fixed dimensionality, so switching to a model with a different
  output size is a migration, not a configuration change.
