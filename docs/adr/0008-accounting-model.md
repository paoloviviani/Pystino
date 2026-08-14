# 0008 — The accounting model

- Status: accepted
- Date: 2026-08-14

## Context

This is the part of the system that must be defensible in an audit months later:
what was spent, by whom, against which group, at what price.

## Decisions

### One row per request, created before the call and finalised after

`usage_records` rows are created with status `in_progress` **before** the upstream is
touched, and finalised afterwards. The alternative — accumulate in memory and insert
once at the end — loses the entire request whenever a client vanishes mid-generation,
and a suspended mobile client is a normal event, not an error path.

Terminal statuses: `completed`, `client_disconnected`, `upstream_error`. A refused
(429) request writes **no row at all** — it consumed nothing.

`finalise()` is idempotent, because it is reachable from both the success path and
the disconnect handler and those can overlap. The first terminal status wins.

### Cost is computed and stored at request time, with the price that produced it

`usage_records.price_id` records **which** `model_prices` row was applied. Prices are
append-only and effective-dated (`effective_from <= now`), so:

- re-pricing a model never rewrites what a past request cost;
- a price dated in the future is ignored, which makes scheduling a change safe;
- an auditor can reconstruct the arithmetic.

### Two billing rules written down because they silently produce wrong invoices

1. **Cached prompt tokens are a subset of `prompt_tokens`, not an addition.** Every
   provider reporting a cache-read count includes those tokens in `prompt_tokens`
   too. Billing both fields at full rate double-charges the cached portion. The
   uncached remainder is billed at the input rate and the cached portion at the
   cache-read rate; with no cache-read price configured, the whole prompt is billed
   at the input rate (the conservative reading).
2. **Reasoning tokens are already inside `completion_tokens`.** They are a breakdown,
   not an extra. Stored for visibility, deliberately not billed again.

### `usage_source` — never silently report zero

| Value | Meaning |
|---|---|
| `upstream_exact` | The upstream reported usage and we believed it. |
| `estimated` | No usable usage frame; counted locally. |
| `unavailable` | Nothing was generated and nothing to estimate. |

The `estimated` case exists because upstreams do lose the trailing usage frame of a
stream (BerriAI/litellm#25389, closed as not-planned), and because some ignore
`include_usage` altogether. Recording zero is worse than estimating: it is
indistinguishable from a free request, it understates a group's spend, and nobody
notices until an invoice arrives.

A usage object full of zeroes is *not* believed either — if text was generated, that
falls back to estimation.

The estimator (`accounting/tokens.py`) is deliberately simple and has no
model-specific tokeniser: a real BPE count needs the upstream's exact vocabulary,
which we do not have for an arbitrary configurable provider, and a wrong tokeniser
produces confidently wrong numbers rather than obviously approximate ones. It does
count CJK characters densely, since chars/4 would understate a Chinese prompt
fourfold.

`GET /api/me/usage` reports `estimated_requests` alongside the totals, so a reader
can always separate measured spend from inferred spend.

### Currency

All prices and quotas are denominated in one configured currency (default EUR). A
price in any other currency is **refused, not converted**: a silently applied
exchange rate produces invoices that look correct and are wrong. The refusal
surfaces as a 400 with `price_currency_mismatch`, and the importer skips and names
such models rather than dropping them quietly.

### Transcripts

`assistant_text` holds the accumulated output. One plain choice is stored as text;
anything richer (several choices, or tool calls) is stored as JSON, because a
transcript that silently keeps only the first choice is worse than one that is
occasionally a JSON blob. Tool call arguments are reassembled from their fragments —
agent traffic is mostly tool calls, and losing them loses the answer.

## Consequences

- A cost of zero is meaningful and distinguishable: `price_id IS NULL` means the
  model was unpriced, and an operator can find those rows. An unpriced model is
  servable rather than an error.
- A misconfigured price does not lose the usage record: cost stays zero, `price_id`
  stays null, and the anomaly is visible in reporting instead of swallowed.
- `client_disconnected` rows count toward quotas. The tokens were spent.
- Transcript storage is a privacy surface. `persist_transcripts` can disable it, and
  the GDPR erasure path is `ON DELETE SET NULL` on the identity columns
  ([0005](0005-persistence.md)) — but note that `assistant_text` itself may contain
  personal data, so an erasure procedure has to blank it explicitly. **That
  procedure is not written yet.**
