# Web search: what is built, and what would come next

Asked as "can we replicate this functionality (native search support and search
provider support…)", against
<https://openrouter.ai/docs/guides/features/server-tools/web-search>.

The answer is yes, in three pieces of very different size. **Phase 1 is built**
([ADR 0058](adr/0058-per-search-pricing.md)). Phases 2 and 3 are recorded here
rather than started, because the second one asks a question about what this
gateway *is* that should be answered deliberately.

## What OpenRouter actually offers

Verified from their documentation and one corroborating search on 2026-09-07.
A server tool, `tools: [{"type": "openrouter:web_search", …}]`, with an
`engine` of `auto`, `native`, `exa`, `firecrawl`, `parallel` or `perplexity`.
`auto` uses the provider's own search where the model has it and falls back to
Exa. Their prices: Exa from $0.007 per request, Parallel from $0.001,
Perplexity $0.005, native passed through.

Worth recording because it was asked as a list including Jina and Linkup:
**neither is an OpenRouter engine**, and nothing there is called "staan". The
requested backend priority for phase 2 is the user's own, not OpenRouter's:

> exa, jina, staan, linkup

## Phase 1 — price and cap what already happens (built)

Passthrough surfaces already forwarded the tool, so native search worked and
was billed as zero, with no ceiling and no trace. That is closed:
`model_prices.per_search`, `usage_records.search_count`, the count read only
from the counterparty's reported usage, and `max_uses` written into the
outgoing tool so a request's search spend is bounded by the provider itself.
ADR 0058 has the whole of it.

## Phase 2 — search backends of our own

A `SearchPlugin` beside `gateway/plugins/`, under ADR 0032's rule: **returns
facts, never computes money**. A search provider becomes a third `kind`
alongside provider and router, with its key encrypted at rest (ADR 0027) and
its rate in the same per-request unit phase 1 introduced.

All four candidates are plain REST over HTTPS, so this adopts no SDK and needs
no licence review (ADR 0001 is satisfied without a decision). In the requested
order:

| Backend | Notes |
|---|---|
| **Exa** | OpenRouter's default; embeddings-and-keyword hybrid. Prices per request with per-result overage above 10. |
| **Jina** | Reader/search APIs; commonly paired with an embedding step. |
| **Staan** | <https://staan.ai> — "the first European Search API", GDPR-framed, **priced in EUR** (€2 per 1,000 web-search requests, €4 for the AI variant, first 1,000 a month free, 20 QPS). The currency matters here: a EUR-native rate needs none of ADR 0054's conversion machinery, and European data residency is the reason this deployment exists at all. |
| **Linkup** | Fourth by request. Not yet examined at source. |

Nothing above is verified beyond Staan's public pricing page; each one's API
shape must be read at source before it is implemented, per ground rule 2.

## Phase 3 — the gateway executing the search

This is the expensive one, and the reason to decide rather than drift.

Every `/v1` route today makes **one** upstream call: resolve → reserve → call →
record → settle. A server tool the gateway executes means N upstream calls plus
M search calls inside one metered request. Consequences, all real:

- `routers/_metered.py` is built around a single call, and
  `test_query_counts.py` bounds round trips per request;
- streaming has to survive the loop — the final answer streams while earlier
  turns were consumed internally;
- one ledger row now aggregates several upstream calls, so `upstream_model`,
  `usage_source` and the cost components all need a defined meaning across
  them;
- Anthropic's `pause_turn` already exists and a plain proxy **silently
  truncates it today**, which is worth fixing whether or not phase 3 happens.

## Two hazards that matter more here than they do at OpenRouter

**A search query is a second egress.** The model's query is prompt-derived text
sent to a third party. Redaction currently protects the request to the *model*
provider; ADR 0037's policy would have to cover a tool call's arguments, and
the per-provider scopes in
[redaction-scoping-plan.md](redaction-scoping-plan.md) are where a search
backend slots in. Results coming back are untrusted text entering the prompt —
a prompt-injection surface added deliberately, and it should be written down as
such rather than discovered.

**`encrypted_content` and response redaction are in direct conflict.**
Anthropic requires the assistant's search-result blocks, `encrypted_content`
included, to be sent back **unchanged** on later turns, or the request fails
with a 400. Response-side redaction rewrites assistant content. Multi-turn
native search and redaction cannot both be naive about this.

## One thing not to copy

`auto` silently falling back between engines is fine as behaviour and not fine
as a silence. The ledger must record which engine served — a `search_source`
beside `cost_source` and `usage_source`. Silent substitution is the class of
thing this codebase refuses everywhere else: `own_prices_fallback` is never
silent, and a substituted model is disclosed.
