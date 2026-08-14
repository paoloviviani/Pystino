# 0019 — Document conversion / OCR behind a configurable HTTP endpoint

- Status: accepted (decision recorded; **not implemented** — Phase 3)
- Date: 2026-08-14

## Context

Document conversion and OCR for RAG ingestion, with a hard requirement that it sit
behind a configurable HTTP endpoint rather than being hardcoded. Docling and
alternatives were to be evaluated.

## Findings (verified 2026-08-14)

| Tool | Licence | Notes |
|---|---|---|
| **Docling** | **MIT**, now under the **LF AI & Data Foundation** | 64.8k stars. Ships `docling-serve`, an HTTP API — fits the endpoint requirement natively. CPU-capable. Individual models carry their own licences. |
| **Marker** (Datalab) | **GPL-3.0 code + RAIL-M weights** | Fastest at scale (~120 pages/s on an H100 batched; Marker 2 scores 76.0 on olmOCR-bench at ~5× MinerU's throughput). RAIL-M restricts commercial use above a revenue threshold. **Not OSI.** |
| **MinerU** (OpenDataLab) | income-threshold conditions | Accuracy leader in several comparisons. **Not OSI** as used commercially. |
| **pdf-craft** | — | Purpose-built for scanned books; fully local. |

## Decision

**The endpoint is the architecture.** `services/rag` calls a configurable HTTP endpoint;
Docling (`docling-serve`) is the **reference implementation and default**, not a
hardcoded dependency.

This is more than tidiness — it is what keeps the licence decision where it belongs:

- Docling is MIT under a neutral foundation, so the default is unencumbered.
- An operator who needs Marker's throughput or MinerU's accuracy can point the endpoint
  at them and accept those terms **for their own deployment**. We neither vendor nor
  default to either, so no non-OSI obligation enters this repository.
- Per [0001](0001-licensing.md), making either the default would require explicit
  approval. Neither is.

## Consequences

- **Model weights are licensed separately from the code that runs them.** Docling core is
  MIT, but GraniteDocling and other VLMs it can invoke have their own terms. Because we do
  not bundle weights, that assessment stays with the operator — but anyone building a
  turnkey image must do it.
- `docling-serve`'s own licence (as distinct from Docling core) was **not verified this
  session**. Check before it goes into `deploy/`.
- The endpoint contract must be defined in `packages/shared-py` when Phase 3 starts, in
  the same spirit as the redaction contract: a stable wire format is what makes the
  implementation swappable.
- OCR is expensive and slow. It belongs in an ingestion worker, never in a request path.

## A related licence note, recorded here because this is where it bites

**Open WebUI is the reference for RAG configurability scope, and nothing more.** Since
v0.6.6 (19 April 2025) it carries a branding clause requiring visible "Open WebUI"
branding above 50 users in any 30-day window, plus a CLA for contributions. It is
explicitly **not OSI-certified open source**; code up to and including v0.6.5 remains
BSD-3.

Look at *what* it makes configurable — search mode, OCR endpoint, embedding endpoint,
chunking — and implement it independently. Do not copy code.
