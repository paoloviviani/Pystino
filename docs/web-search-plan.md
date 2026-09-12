# Web search: what is built, and what would come next

Asked as "can we replicate this functionality (native search support and search
provider support…)", against
<https://openrouter.ai/docs/guides/features/server-tools/web-search>.

The answer is yes, in three pieces of very different size. **Phase 1 is built**
(ADR 0058), and so is **phase 2** for two of the four backends — Linkup and
Exa, with `POST /v1/search` behind them. Phase 3 is recorded here rather than
started, because it asks a question about what this gateway *is* that should be
answered deliberately.

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

## Phase 2 — search backends of our own (built, for two of four)

A `SearchPlugin` beside `gateway/plugins/`, under ADR 0032's rule: **returns
facts, never computes money**. A search provider becomes a third `kind`
alongside provider and router, with its key encrypted at rest (ADR 0027).

**Not a rate. A count.** An earlier version of this paragraph said the backend
would carry "its rate in the same per-request unit phase 1 introduced"; that is
now decided the other way, and the metering half of it is built. Two of the
four vendors' prices cannot be established at source — Jina publishes no
per-token figure publicly at all, and Staan's dearer "for AI" tier is neither a
documented request parameter nor reported back in the response — so a rate
table here would be a guess in half its rows, wearing the same type as a
measurement. Request counts also reconcile *better* against a vendor
dashboard than money does: no currency, no rounding, no rate that drifts out of
date while the table says otherwise.

What that buys, and what it costs:

* `LimitMetric.OWN_SEARCH_REQUESTS` is a quota metric across the same scopes as
  cost and tokens, all rules must pass, and it flows through the one
  reserve → record → settle path in `routers/_metered.py`.
* `usage_records.own_search_requests` is the count, **separate from
  `search_count`**, which is the counterparty's server-side searches from phase
  1. Merging them would leave no report able to tell "Anthropic searched" from
  "we called Staan".
* `own_search_backend` and `own_search_tier` are plain label columns with no
  rate attached. They land now because they cannot be backfilled later, and
  because a money layer — if one is ever wanted — would have to look a rate up
  by exactly those two.
* **The limitation, which every screen that offers this has to state:** a
  request quota bounds *volume, not spend*. Exa `deep-reasoning` is $15 per
  1,000 against `instant` at $7, and Linkup `deep` is ten times `flash`, so a
  thousand searches is a number anyone can reason about and a bill nobody can.

### What is built

`gateway/plugins/search.py` is the protocol, and **Linkup and Exa** implement
it. A search backend is a `Provider` row like any other — its key encrypted at
rest, its client cached and rebuilt on edit — with `kind = search` and a plugin
that can also run a search. The decision that made this small rather than large
is that a search backend is **not a second kind of configuration**: it reuses
the provider registry, the credential path, the access grants and the ledger,
and the only thing it adds is two methods.

Five things about the shape, each of which was a choice with an alternative:

* **A "model" is a backend at a tier.** `upstream_model` carries Linkup's
  `depth` or Exa's `type`, `ModelKind.SEARCH` is the kind, and `POST /v1/search`
  names a model like every other surface. That is what makes *which depth a
  caller may run* a grant an administrator makes through the machinery that
  already exists, which matters precisely because a request ceiling bounds
  volume and not spend. The alternative — a `depth` parameter on the request —
  would have made the bullet above unenforceable.
* **The route goes through `_metered`**, reserving `worst_case_own_searches=1`
  and `TokenCounts()`. Nothing else. There is no token cost to bound and no
  rate to multiply.
* **The search is counted before the call and never refunded.** A vendor
  error, a timeout and an unreadable 2xx all leave the count in the ledger:
  vendors bill requests received, and a ceiling that forgave a failure would be
  raisable by making the search fail. What it costs is that a misconfigured
  backend burns a caller's budget — visible, because the row names the backend.
* **The query is redacted**, because it is a second egress. The consequence is
  real and is not worked around: under a policy that protects `PERSON`, a
  search for a person by name searches for `<PERSON_…>`. Redaction is scoped
  per provider (ADR 0038), so the escape hatch is to turn the entity off *for
  the search provider*.
* **Exa's `costDollars` is read and logged, never stored.** Putting a vendor's
  dollar figure in `upstream_cost` would have the reconciliation report treat
  it as a counterparty charge against a price table that does not exist. Exa's
  own schema agrees: the field says it "is not an invoice record".

Both vendors' contracts were read from their live OpenAPI documents on
2026-09-11 rather than from their prose, and in both cases it mattered:

| Backend | Read at source | What the document changed |
|---|---|---|
| **Linkup** | `https://api.linkup.so/v1/openapi.json` | `POST /v1/search`, bearer. Required `q`, `depth` (`deep`/`fast`/`flash`/`standard`), `outputType`. Text hits are `{name, url, content, favicon, type}` — `name`, not `title`. Their own quickstart shows `curl -G`, which would be a GET with a query string; the schema says POST-only with a required body, and the schema wins. **No cost or usage field on a search response at all** — the balance is a separate endpoint returning a bare number. And the "10 QPS org-wide" recorded earlier in this project is **not** in the document: it describes a 429 and defines no numeric limit, so nothing depends on it. |
| **Exa** | `https://api.exa.ai/openapi.json` (`info.version` 2.0.0) | `POST /search`, `x-api-key`. `type` is `instant`/`fast`/`auto`/`deep-lite`/`deep`/`deep-reasoning` — **not** the `neural`/`keyword`/`auto` that a documentation-rendering fetch still serves from a stale copy. A result has **no relevance score**; `resolvedSearchType` is deprecated and may be an empty string, which is why the ledger records the tier we asked for. `costDollars` is `{total, search:{neural,keyword}, summary, contents:{text,highlights,summary}}`. |

All candidates are plain REST over HTTPS, so this adopts no SDK and needs no
licence review (ADR 0001 is satisfied without a decision).

### What is deliberately not built, and why

| Backend | Why not |
|---|---|
| **Jina** | Reader/search APIs; commonly paired with an embedding step. No per-token figure is published publicly. Not a blocker for a *count*, but it was never established at source what a request to their search endpoint is and is not, and a plugin written from memory is the thing ground rule 2 forbids. |
| **Staan** | <https://staan.ai> — "the first European Search API", GDPR-framed, **priced in EUR** (€2 per 1,000 web-search requests, €4 for the AI variant, first 1,000 a month free, 20 QPS). The blocker is specific: the dearer "for AI" tier is **neither a documented request parameter nor reported back**, so `own_search_tier` could not be filled honestly, and a tier column that silently names the cheap tier on a dear request is worse than no backend. Settling it needs a real key. |

Both are a plugin module and one line in `plugins/registry.py` when somebody
has a key; the protocol is the hook, and nothing else has to move.

**A renderer is deployed, and it is not really ours.** A backend returns
URLs and snippets, and a snippet is not an answer — turning a result into text a
model can read means fetching the page and running its JavaScript, because a
growing share of the web is an empty `<div>` to an HTTP client, with no error to
say so. the chat repository's `deploy/compose/docker-compose.playwright.yml` is a headless browser on
the compose network, opt-in. Note what it is actually for, because the
distinction matters: **fetching a URL somebody named is not searching**, and the
browser exists for fetch first — see the chat's `docs/browser.md`. Search is a
second consumer of it, and only for Linkup, since Exa, Jina and Staan can all
return page content themselves;
the chat's `docs/browser.md` has the version pin, the measured footprint, and why it
publishes no port. Fetching the page is a *third* egress after the prompt and
the query, and the pages read on a user's behalf are often more revealing than
the query — which is why the rendering is in this deployment rather than at a
hosted scraping API.

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

**Citation offsets and redaction: fixed, and not the conflict this document
first claimed.** The original version said Anthropic's `encrypted_content`
requirement and our redaction were in direct conflict, with a 400 waiting for
multi-turn native search. That was inferred from Anthropic's documentation and
never checked against the code; it is wrong, and checking it turned up the real
defect one field over. Both are written up in
ADR 0059, which is built: restoration now
reports where it wrote, and each surface moves the offsets it knows about.

What remains true and unfixed is the mild version. On a multi-turn conversation
the client replays the assistant turn, we re-redact its `text` blocks, and
`encrypted_content` passes through untouched — so the provider decrypts its own
original search results and reads them beside our redacted prose. Not an error;
just the two halves of one message disagreeing.

## One thing not to copy

`auto` silently falling back between engines is fine as behaviour and not fine
as a silence. The ledger must record which engine served — a `search_source`
beside `cost_source` and `usage_source`. Silent substitution is the class of
thing this codebase refuses everywhere else: `own_prices_fallback` is never
silent, and a substituted model is disclosed.
