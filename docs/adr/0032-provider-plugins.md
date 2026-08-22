# 0032 — Providers, routers, and a plugin seam for both

- Date: 2026-08-22
- Status: **proposed** — the design is here to be argued with before the money
  path is touched. Ground rule 3 applies: this is where a wrong answer is a
  wrong invoice.
- Supersedes the per-provider knobs added by
  [0027](0027-inference-providers.md), [0028](0028-embeddings-and-served-model.md)
  and [0030](0030-more-surfaces.md).
- Follows the plugin precedent set by [0026](0026-pluggable-detection.md), which
  already works in this codebase.
- Does **not** reopen [0003](0003-build-vs-adopt-gateway.md) — build rather than
  adopt LiteLLM. See the alternatives section for evidence since that supports it.

## Context

Vendor-specific knowledge is smeared across the gateway. Counting mentions of
provider names and their quirks:

| file | what it knows about vendors |
|---|---|
| `accounting/cost.py` | four spellings of cache-write tokens, two opposite prompt conventions, micro-EUR |
| `pricing.py` | `context_size`, `cache_read_cost`, `supported_features`, the catalogue's shape |
| `upstream.py` | bearer versus `x-api-key`, `stream_options` |
| `protocols.py` | where usage and the served model live, per surface |
| `models.py` / `schemas.py` | `auth_scheme`, `forward_stream_options`, `upstream_cost_unit` |
| `accounting/recorder.py` | the `provider` field some routers put in the body |

Three of those are **columns on `providers` added reactively**, one per
discovery: `auth_scheme` when Anthropic's own API refused a bearer token,
`forward_stream_options` when a provider documented that unknown parameters can
narrow routing, `upstream_cost_unit` when one turned out to report cost in
micro-EUR. Each was the right local fix. The pattern is the problem: the next
provider adds a fourth column, and the accounting modules keep accreting
`if provider == ...` in spirit if not in syntax.

Pricing sits outside the application entirely, in
`scripts/import_cortecs_pricing.py`, whose parser is Cortecs-shaped and whose
name says so.

Meanwhile a distinction has gone unnamed. Some counterparties **host** models;
others **choose** a host per request. That difference changes what the ledger
must record, and nothing in the schema expresses it.

## Decision

### 1. Providers and routers are different kinds, distinguished by one question

> **Is the endpoint that serves a request determined by the model asked for, or
> chosen per request by the counterparty?**

Fixed → a **provider**. Chosen per request → a **router**.

Not a vendor list, because a vendor list goes stale and invites argument. The
question is answerable for any counterparty and everything that matters follows
from it:

| | provider | router |
|---|---|---|
| price | the model determines it | the model *may* determine it; the serving endpoint may too |
| which endpoint ran | the model implies it | **a fact per request, and must be recorded** |
| model substitution | does not happen | happens, and is already modelled (ADR 0028) |
| reconciliation | a nice check | the only way to notice a routing change |

`providers.kind` becomes an enum: `provider` or `router`. A router's rows are
the ones for which "which endpoint served this" is a variable rather than a
constant, and the gateway can then require it be recorded rather than hoping.

### 2. A plugin owns vendor knowledge. It returns facts; the gateway does the arithmetic.

This is the load-bearing rule, and the reason the refactor is safe:

> **A plugin never computes money.** It normalises *inputs* — token counts,
> which endpoint served, what the counterparty says it charged, what the
> catalogue offers. `accounting/cost.py` remains the only code that multiplies a
> count by a rate.

That keeps the property which makes this ledger trustworthy: one place does the
arithmetic, and 775 tests are pointed at it. A plugin that could compute a
charge would be a plugin that could get an invoice wrong, and no amount of
plugin testing would restore the single-place guarantee.

Shape, following `Redactor` and the `llmp.redactors` entry point:

```python
class ProviderPlugin(Protocol):
    name: str
    kind: ProviderKind                      # provider | router

    # --- request shaping -------------------------------------------------
    def auth_headers(self, credential: str) -> Mapping[str, str]
    def prepare_payload(self, payload: dict, *, surface: ApiSurface) -> dict

    # --- reading a response: facts only ---------------------------------
    def read_usage(self, payload: dict, *, surface: ApiSurface) -> TokenCounts
    def read_served_endpoint(
        self, payload: dict, headers: Mapping[str, str]
    ) -> ServedBy | None
    def read_reported_cost(self, payload: dict) -> ReportedCost | None

    # --- catalogue and pricing ------------------------------------------
    async def catalogue(self, client: httpx.AsyncClient) -> list[CatalogueEntry]
```

`ReportedCost` carries an amount **and its declared unit**, because nothing in a
payload says whether `136` means micro-EUR or credits — the point already made
by `UPSTREAM_COST_UNITS`. Moving the unit into the plugin removes the
`upstream_cost_unit` column and the operator's obligation to know it.

`read_served_endpoint` is what the current code cannot do: the reference router
names the serving endpoint **only in a response header**, never in the body or
the stream frames, so `usage_records.upstream_provider` is null for every request
through it. That fix belongs in a plugin, not in the shared recorder.

The three `providers` columns collapse into plugin behaviour. `extra_headers`
and `base_url` stay — they are deployment facts, not vendor facts.

### 3. Pricing moves into the plugin, and stays a deliberate act

`catalogue()` replaces `parse_catalogue`, and a generic admin route plus CLI
replaces `scripts/import_cortecs_pricing.py`.

What must **not** change, and is easy to lose in a refactor:

- **Dry run is the default.** The script's own reasoning stands: a pricing change
  is an administrative act, and a timer inside the request path would let a
  counterparty's catalogue silently change what the foundation charges its own
  groups.
- **Prices stay append-only and effective-dated.** A plugin proposes candidate
  prices; the gateway decides whether they differ from the row in force and
  writes a new one. Past requests keep the price they were billed at.
- **Currency mismatch still refuses.** No plugin may convert.

### 4. Prices may be per sub-provider, and the schema should allow it before it is needed

`model_prices` gains a nullable `sub_provider`. Null means "any endpoint", which
is every row today. A router whose price genuinely varies by endpoint gets one
row per endpoint, and `select_price` prefers an exact match over the null row.

Forward-compatible rather than speculative: the column is null everywhere until
a plugin populates it, and the alternative — discovering that a router varies and
needing a migration on the money table under time pressure — is worse.

### 5. The Cortecs plugin is the router reference implementation

Encoding what was **measured**, not assumed (2026-08-22, live API):

| behaviour | value | how it was established |
|---|---|---|
| kind | router | 53 of 108 models list more than one sub-provider; `gpt-oss-120b` lists eight |
| auth | bearer | works |
| `stream_options` | do not send | accepted, and usage arrives either way |
| served endpoint | `x-cortecs-provider` header | found by dumping headers; absent from body and frames |
| served model | `x-cortecs-model` header | ditto; feeds the existing substitution check |
| reported cost | `usage.cost`, integer **micro-EUR**, plus `cost_details` | derived and checked four ways against catalogue rates |
| cache-write spellings | `created_cache_tokens`, `cache_write_tokens`, `cache_creation_tokens` | three backends, three names, two of them in one response |
| catalogue keys | `context_size`, `pricing.*`, `input_modalities`, `output_modalities`, `supported_features`, `providers` | ADR 0031 |

## An inconvenient measurement, stated plainly

The brief for this work is "correct billing on our side based on actual provider
info". The evidence says that, **for Cortecs today, the catalogue price is
already correct regardless of which sub-provider serves the request**:

| model | served by | input charged / listed | output charged / listed |
|---|---|---|---|
| `llama-3.3-70b-instruct` | nebius | 0.1163 / 0.1160 | 0.3750 / 0.3580 |
| `gpt-oss-120b` | ovh | 0.0805 / 0.0800 | 0.4000 / 0.4000 |
| `deepseek-v4-flash-0731` | inceptron | 0.1186 / 0.1170 | 0.2857 / 0.2510 |

Every deviation is **per-component rounding up to a whole micro-EUR** —
`44.89→45`, `34.80→35`, `43.41→44`, `7.03→8` — not a different rate. Three
different sub-providers, all honouring the listed price. Cortecs appears to
absorb the difference between its sub-providers and quote one price.

Two caveats on that conclusion. Routing is **sticky**: ten identical requests
went to the same sub-provider every time, so no model was observed served by two
different endpoints, and price invariance across endpoints is therefore *not*
proven — only that each observed pairing honoured the listed price. And the
sample is three models.

So the honest framing of what per-sub-provider support buys **today**:

- **Attribution and reconciliation**, not different rates. Recording which
  endpoint ran makes "did the price we billed match the endpoint that served it"
  answerable from our own ledger, and lets the drift figure be broken down by
  endpoint instead of only by router.
- **Detection if that ever changes.** A router that starts passing through
  sub-provider prices would show up as drift against one endpoint, which is
  exactly what the existing `upstream_cost` reconciliation is for.
- **The rounding is real and quantifiable.** Charging up to a whole micro-EUR per
  component means small requests are proportionally overcharged — 25% on a
  40-token request, ~2% on a 400-token one. That is not a bug to fix but a fact
  to expect, and it explains the small positive drift already visible in reports.

If per-endpoint pricing is wanted regardless, the schema above supports it and
costs nothing while unused. What it should **not** do is bill from the
counterparty's reported figure: that carries their rounding and their currency,
and we refuse conversion by design.

## Consequences

Good:

- Adding a counterparty stops meaning "add a column and edit four accounting
  modules". It means writing a plugin and a test for it.
- The vendor evidence in `docs/cache-accounting-findings.md` becomes executable
  rather than prose: each spelling is a plugin's declared behaviour, with a test.
- `upstream_provider` gets populated for routers, which it never has been.
- Pricing stops being a script somebody has to remember exists.

Costs and risks:

- **This is a refactor of the money path.** It must be staged so the suite is
  green at every step, and each stage should leave the ledger's arithmetic
  untouched.
- A plugin seam invites plugins that overreach. The "facts, not arithmetic" rule
  needs to be enforced by the contract's *return types* — a plugin that can only
  return counts and declared units cannot compute a charge.
- Three columns and one script get removed. That is a migration and a
  documentation change, not just code.

## Alternatives considered

**Keep adding knobs.** Cheapest per step and the reason we are here. It has
worked four times; the fifth is `if provider == "bedrock"` inside `cost.py`.

**Adopt LiteLLM as the provider layer.** Already decided in
[ADR 0003](0003-build-vs-adopt-gateway.md) and not reopened here. Two pieces of
evidence found since **corroborate** that decision rather than complicate it:

- 0003 rested partly on LiteLLM issue #25389, streamed responses silently
  reporting zero tokens, closed as not-planned. The same failure class has since
  appeared again in
  [#27191](https://github.com/BerriAI/litellm/issues/27191) — configured cache
  rates silently ignored, so cached tokens bill at the full input rate, plus a
  dashboard that reads only Anthropic's field names. Unresolved when read. That
  is the second instance of "the accounting is approximately right", which is
  0003's central objection.
- The licence position is unchanged and was re-verified on 2026-08-22: MIT core,
  everything under `enterprise/` separately licensed, a CLA required to
  contribute, and the paywalled features are SSO, RBAC, audit logs and team
  budgets — which is what this platform exists to provide (ground rule 1).

What this ADR adds is that the plugin seam deliberately leaves the door open to a
`litellm` plugin doing provider *translation* while the gateway keeps the ledger.
That is the shape to reach for when genuine provider diversity arrives, and it is
available precisely because plugins return facts and never compute money.

**One kind, with a `chooses_endpoint` boolean instead of provider/router.**
Simpler schema, and it hides that the two need different *mechanisms*: a router
needs the served endpoint recorded per request, needs substitution checked, and
needs reconciliation grouped by endpoint. A boolean would leave all three
optional.

## Open questions, for whoever reviews this

1. **Should a router be allowed to bill from its reported cost?** This ADR says
   no. If a counterparty's figure is ever contractually authoritative, that is a
   different decision and wants its own ADR.
2. **Should plugins be in-tree or entry-point discovered?** ADR 0026 chose entry
   points for redactors. In-tree is simpler to test and review; entry points let
   a deployment add a counterparty without forking. Probably: in-tree for the
   ones we maintain, entry points supported for others.
3. **How is a plugin bound to a provider row?** A `plugin` column naming it,
   validated at startup against the registry like the redaction engine — which
   refuses an unknown name rather than falling back.
4. **What happens to a provider row whose plugin disappears?** Refusing at
   startup is consistent with redaction; refusing means one uninstalled package
   takes the gateway down. Probably: refuse to *serve that provider*, loudly, and
   keep running.
