# Prompt-cache accounting: what the providers actually send

- Investigated 2026-08-17 against the **live** Cortecs API with a real key, plus
  LiteLLM's and OpenRouter's documentation and issue tracker.
- Status: **fixed.** The investigation is kept because the evidence is the
  interesting part — the spelling table below is what the reader now consults,
  and a provider adding a fifth name is caught by
  `scripts/test_cache_accounting_live.py`. Ground rule 3 in
  the ground rules
  [in the repository's working notes](https://github.com/paoloviviani/Pistin-Gateway/blob/main/CLAUDE.md)
  apply: this was a money bug.
- Related: [ADR 0028](https://gitlab.linksfoundation.com/viviani/ai-stack/-/blob/main/docs/adr/0028-embeddings-and-served-model.md) (the two prompt
  conventions), [ADR 0031](https://gitlab.linksfoundation.com/viviani/ai-stack/-/blob/main/docs/adr/0031-model-capabilities.md) (`context_size`, the
  same class of mistake).

## The short version

1. There are **at least four different spellings** for "cache write tokens"
   across providers, and the Chat Completions reader looked for **none** of
   them. Only the Anthropic reader handled cache writes at all, so
   `/v1/messages` was correct and the other four surfaces were not.
2. For the 19 Cortecs models that price cache writes, a request through
   `/v1/chat/completions` therefore billed written tokens at the **full input
   rate** — an overcharge of roughly 20× on Gemini, whose writes are far cheaper
   than its input, and an undercharge on Anthropic, whose are dearer. Wrong in
   both directions depending on the provider.
3. `cache_write_tokens` had no column, so even the surface that read the count
   correctly then discarded it.
4. Cortecs **does** report cost downstream, inside `usage`, in integer
   micro-EUR, and it was ignored.

## Evidence: what Cortecs sends

Two identical requests, ~1150-token prompt, `max_tokens=5`, three seconds apart.

`deepseek-v4-flash-0731` (vLLM backend — tensorix / inceptron / scaleway):

```jsonc
// first call
{"completion_tokens": 2, "prompt_tokens": 1152, "total_tokens": 1154,
 "prompt_tokens_details": {"cached_tokens": 0, "created_cache_tokens": 1024},
 "cost": 136,
 "cost_details": {"prompt_cost": 135, "cache_read_cost": 0,
                  "cache_write_cost": 0, "completion_cost": 1,
                  "prompt_audio_cost": 0}}
// second call — cache hit
{"completion_tokens": 2, "prompt_tokens": 1152, "total_tokens": 1154,
 "prompt_tokens_details": {"cached_tokens": 768, "created_cache_tokens": 256},
 "cost": 67,
 "cost_details": {"prompt_cost": 45, "cache_read_cost": 21,
                  "cache_write_cost": 0, "completion_cost": 1,
                  "prompt_audio_cost": 0}}
```

`qwen3-235b-a22b-instruct-2507` (nebius / scaleway / tensorix) — **different
field names again**, and two of them for the same quantity:

```jsonc
{"prompt_tokens_details": {"audio_tokens": 0, "cached_tokens": 0,
                           "video_tokens": 0, "cache_write_tokens": 0,
                           "cache_creation_tokens": 0},
 "completion_tokens_details": {"audio_tokens": 0, "reasoning_tokens": 0,
                               "image_tokens": 0}}
```

`ministral-3b-2512` (mistral) — no cache-write key at all, and `cached_tokens`
stayed 0 across both calls, so Mistral's caching did not engage for this shape
of request. Also puts `service_tier` *inside* `usage`.

### The cost is in integer micro-EUR

Derived, then checked against the catalogue rates for
`deepseek-v4-flash-0731` (input 0.117, output 0.251, cache read 0.027 EUR/Mtok):

| reported | tokens × rate | µEUR | matches |
|---|---|---|---|
| `prompt_cost` 135 | 1152 × 0.117 | 134.78 | ✓ |
| `completion_cost` 1 | 2 × 0.251 | 0.502 | ✓ |
| `prompt_cost` 45 | (1152 − 768) × 0.117 | 44.93 | ✓ |
| `cache_read_cost` 21 | 768 × 0.027 | 20.74 | ✓ |

So: **1 unit = 1e-6 EUR**, rounded to the nearest integer. That rounding is
coarse enough to matter — a 0.5 µEUR completion became 1 — which is one reason
not to bill from it directly.

### Semantics, confirmed rather than assumed

- `prompt_tokens` **includes** both the cached and the cache-written slices
  (OpenAI convention, as ADR 0028 says).
- `prompt_cost` covers only the slice charged at the input rate. On the second
  call that was `1152 − 768 = 384` tokens — note the 256 *written* tokens were
  billed at the **input** rate, because this model has no `cache_write_cost`.
  Our `compute_cost` already falls back exactly that way when
  `cache_write_per_mtok` is `None`, so that path is correct today.
- `cached_tokens + created_cache_tokens ≤ prompt_tokens`, and the three slices
  are disjoint — which is the model `TokenCounts` already uses.

## Every spelling seen, in one place

Cache **write** / creation:

| field | where | seen on |
|---|---|---|
| `created_cache_tokens` | `prompt_tokens_details` | Cortecs → vLLM backends |
| `cache_write_tokens` | `prompt_tokens_details` | Cortecs → nebius; OpenRouter; LiteLLM says moonshot/deepseek/kimi |
| `cache_creation_tokens` | `prompt_tokens_details` | Cortecs → nebius (alongside the above); LiteLLM's own normalised name |
| `cache_creation_input_tokens` | top level | Anthropic native — **we handle this one** |
| `cacheWriteInputTokens` | top level, camelCase | AWS Bedrock (per LiteLLM) |

Cache **read**:

| field | where | seen on |
|---|---|---|
| `cached_tokens` | `prompt_tokens_details` | OpenAI standard; Cortecs, OpenRouter, LiteLLM — **we handle this** |
| `cache_read_input_tokens` | top level | Anthropic — **we handle this** |
| `cacheReadInputTokens` | top level, camelCase | Bedrock |

Other keys encountered that we do not read: `prompt_tokens_details.audio_tokens`,
`.video_tokens`, `completion_tokens_details.image_tokens`.

## How the other gateways handle it

**LiteLLM** — normalises everything into an OpenAI-shaped `Usage` with a
`PromptTokensDetails`, using `cached_tokens` for reads and its own
`cache_creation_tokens` for writes. For Anthropic it computes
`prompt_tokens = input + cache_read + cache_creation`; for Bedrock
`inputTokens + cacheReadInputTokens + cacheWriteInputTokens`. Cost goes through
`completion_cost()`, and the proxy returns it in an `x-litellm-response-cost`
header.

Its open issue [#27191](https://github.com/BerriAI/litellm/issues/27191) is our
bug, written up by someone else:

- configured `cache_read_input_token_cost` under `custom_cost_per_token` is
  **silently ignored**, so cached tokens are billed at the full input rate — a
  67% overcharge in their example;
- the proxy dashboard reports zero cache tokens for OpenAI-compatible providers
  because the aggregation reads only the Anthropic field names.

Unresolved when read. Useful as independent confirmation that the failure mode
is real and that reading one spelling is not enough.

**OpenRouter** — `prompt_tokens_details.cached_tokens` for reads,
`prompt_tokens_details.cache_write_tokens` for writes. Reports cost downstream
as `usage.cost` in credits, plus `cost_details.upstream_inference_cost`, "the
actual cost charged by the upstream AI provider" — a distinction worth stealing.

*Unverified:* the OpenRouter documentation summary suggested cached tokens are
counted *separately from* `prompt_tokens`, which contradicts both OpenAI's
convention and LiteLLM's reading of it. Not tested against their live API, so
do not rely on it either way without checking.

## What is already right

- The catalogue price import reads `cache_read_cost` and `cache_write_cost`,
  which is exactly what Cortecs sends (`_CACHE_READ_KEYS` / `_CACHE_WRITE_KEYS`
  in `pricing.py`). No repeat of the `context_size` bug — checked.
- `TokenCounts.billable_prompt` already subtracts both cache slices, and
  `compute_cost` already adds the written slice back into the input-rate charge
  when the model has no cache-write price. Both correct.
- `from_anthropic_usage` reads writes properly, so `/v1/messages` is fine.
- 56 of 107 Cortecs models price cache reads; 19 price writes. Cache-write
  pricing appears only on Anthropic, Google Gemini and the newer OpenAI models,
  which is consistent with those being the providers with explicit/TTL caching.

## What was changed

1. **Cache writes are read on the OpenAI-shaped surfaces.** `from_usage` and
   `from_responses_usage` left `cache_write` at 0; both now consult
   `_CACHE_WRITE_KEYS`, in `prompt_tokens_details` first and then flattened at
   the top level.

   A tolerant multi-key reader is right *here* and wrong for the case ADR 0028
   warns about, and the distinction is worth keeping straight: the two prompt
   conventions differ in **meaning** — one includes cached tokens, the other
   excludes them — and merging them silently produces wrong numbers. These
   differ only in the **spelling of the same quantity**. Tolerating spellings is
   safe; tolerating semantics is not.

2. **The slices are clamped.** First match wins rather than a sum, because
   Nebius returns both `cache_write_tokens` and `cache_creation_tokens` for one
   quantity. `cached + written <= prompt` is enforced, with the standardised
   `cached_tokens` taking precedence, so a provider reporting writes *outside*
   its prompt total cannot drive the input charge to zero.

3. **The upstream figure is recorded, never billed from.** `usage_records`
   gained `upstream_cost` and `upstream_cost_currency`, and `usage_records`
   also gained `cache_write_tokens` — which had been computed since ADR 0030
   and then dropped, there being no column for it.

   Reading `usage.cost` needs a **declared unit**: nothing in the payload says
   whether `136` means micro-EUR or credits, and the two differ by a factor of a
   million.

   *Superseded 2026-08-24, same conclusion by a better route.* This was
   `providers.upstream_cost_unit`, null by default, with the accepted names in
   `UPSTREAM_COST_UNITS`. [ADR 0032](https://gitlab.linksfoundation.com/viviani/ai-stack/-/blob/main/docs/adr/0032-provider-plugins.md) slice 3 moved
   the answer into the plugin and dropped the column: the unit is knowledge about
   a counterparty, and asking an operator to fill it in was asking them to be the
   plugin. A provider whose plugin does not read a cost reports none — which is
   the same safe default, reached without a form field that can be typed wrong.

   The admin usage report sums it and says so in a disclosure when present,
   because a column nothing reads is a column nobody trusts.

4. **Tests.** `apps/gateway/tests/test_cache_accounting.py` — every spelling in
   the table, the both-Nebius-keys case, the clamps, the observed payloads
   verbatim, the Gemini-shaped ~20x overcharge, and the two conventions still
   producing the same answer from their different inputs.
   `scripts/test_cache_accounting_live.py` drives a real cache hit and checks the
   ledger, which is the only thing that can notice a renamed field.

## Reproducing

`scripts/test_cache_accounting_live.py`, with `deploy/.env` sourced. It finds an
active chat model whose price carries a cache-read rate, sends the same
~1500-token prompt twice, and asserts the ledger caught both slices. It skips
rather than fails where no such model is catalogued, because the fake upstream
does not cache.

Verified against the live provider on 2026-08-21: prompt 1628, cached 1280,
written 256, our cost 0.000076029 EUR against a reported 0.000077 — the gap
being their rounding of each component up to a whole micro-EUR.

Note the supplied key is **provider-scoped** — google, azure and amazon backends
answer 404 `Provider not in allowed providers`, so Anthropic-, Gemini- and
OpenAI-backed caching could not be observed directly and the table entries for
those come from documentation, not from this key.
