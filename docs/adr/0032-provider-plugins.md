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

That holds under both billing modes in decision 6: in `provider_reported` mode
the plugin still only *reports* a number it read, and the gateway decides to
treat it as the charge. The plugin multiplies nothing either way.

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

`ReportedCost` carries an amount, **its declared unit**, and whether the plugin
asserts the figure is the counterparty's actual charge rather than an estimate.
The unit because nothing in a payload says whether `136` means micro-EUR or
credits — the point already made by `UPSTREAM_COST_UNITS`, and moving it into the
plugin removes both the `upstream_cost_unit` column and the operator's obligation
to know it. **Built:** the column is gone, and so are `UPSTREAM_COST_UNITS` and
`read_upstream_cost`, which had no caller left once every plugin knew its own
unit. The capability that went with them is named in the consequences below. The assertion because decision 6 lets a deployment bill from that
figure, and a plugin that merely passes through something a vendor calls "cost"
should not be selectable for that.

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

### 6. Two billing modes, and both figures always recorded

Resolved 2026-08-22, replacing the earlier position that only our own arithmetic
may bill. A provider or router is configured as **one** of:

- `own_prices` *(default)* — we charge tokens x our price row. The counterparty's
  figure is evidence.
- `provider_reported` — the counterparty's figure **is** the charge. Our price
  row still exists, and is still evaluated, but as evidence.

The reason both are wanted is that they answer different accounting questions,
and which one is right is a policy the platform does not get to decide: strict
pass-through cost recovery makes their figure correct by definition, while
anything involving overhead recovery, a subsidised group, or a price held steady
across a budgeting year makes ours correct by definition.

**The invariant that makes either safe: both figures are always recorded, in
every mode.** A deployment that defers to the counterparty still keeps its own
arithmetic, and one that bills its own prices still keeps theirs. Misalignment is
then a recorded fact rather than something nobody can reconstruct afterwards —
which is the whole reason the earlier version of this ADR was too restrictive.

`usage_records` therefore carries three cost figures with one meaning each:

| column | meaning | always present |
|---|---|---|
| `cost` | **what we charge.** Authoritative for quotas, reports and invoices | yes |
| `computed_cost` | our arithmetic from the price row in force | yes |
| `upstream_cost` (+ currency) | what the counterparty said it charged | when reported |
| `upstream_cost_details` | their breakdown, stored verbatim as JSON | when reported |
| `cost_source` | which of the two produced `cost`, including fallback | yes |

`price_id` already records *which* price row was in force, and prices are
append-only and effective-dated, so our rates at the time are recoverable
without storing them again.

The breakdown is kept as JSON rather than as four more numeric columns because
their shape is theirs, not ours — the reference router reports five components
including `prompt_audio_cost`, and a different one will report something else.
Normalising it would be inventing a schema for someone else's data. Note the
constraint from ADR 0029's neighbourhood: PostgreSQL has no equality operator for
`json`, so this column must never appear in a `DISTINCT` or `GROUP BY`.

Five rules that go with the modes:

1. **`cost_source` never lies about a fallback.** If the mode is
   `provider_reported` and no figure arrives — a failed request, or a provider
   that omits it — the charge falls back to `computed_cost` and the source says
   `own_prices_fallback`. Silently falling back would have a pass-through
   deployment quietly billing from a price table nobody was maintaining. Reports
   should disclose the count, exactly as they already disclose estimated token
   counts.
2. **Several units are supported; none are converted.** Revised on review. An
   earlier draft refused `provider_reported` unless the counterparty reported the
   billing currency, which is the wrong lever: refusing to *hold* a figure is not
   the same as refusing to convert it. If an upstream bills dollars, the ledger
   records dollars and the report says dollars.

   A unit is a property of a figure, not of the system. `upstream_cost` carries
   its own `upstream_cost_currency` per row, reconciliation groups by provider
   **and** currency, and the disclosure names both units — so a dollar figure is
   never summed into a euro total and never silently rated into one. Already
   true, and pinned by
   `test_a_provider_that_bills_in_another_currency_is_reported_in_it`.

   What remains a single currency is arithmetic *within* one charge: a price row
   in USD on a model billed in EUR is a configuration error, because combining
   them needs a rate. `CurrencyMismatch` keeps exactly that meaning.

3. **Prices are still required in `provider_reported` mode**, because admission
   happens *before* the request and their figure only exists after. The quota
   reservation must use our estimate in both modes; only the settlement differs.
   A provider in `provider_reported` mode with unpriced models has no cost-based
   admission control at all, which is a hole worth warning about on the console
   rather than discovering from an overspend.
4. **The rounding is inherited knowingly.** Their figure is pre-rounded — the
   reference router rounds each component up to a whole micro-EUR, which is
   ~25% on a 40-token request and ~2% on a 400-token one. In
   `provider_reported` mode that is passed to the group being billed. That is
   the correct behaviour for pass-through and it should be visible, not
   discovered.
5. **A plugin declares whether its figure is fit to bill from.** Reporting a
   cost and being *contractually authoritative* about it are different claims,
   so `read_reported_cost` returns the amount, its unit, and whether the plugin
   asserts it is the counterparty's actual charge. A deployment cannot select
   `provider_reported` against a plugin that does not make that assertion.

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
costs nothing while unused.

Note what this does *not* settle. Per-endpoint prices are for the `own_prices`
mode; a deployment in `provider_reported` mode against a router that varies by
endpoint gets the right answer automatically, because their figure already
accounts for whichever endpoint ran. The two mechanisms address the same risk
from opposite ends, and decision 6 is what lets a deployment pick which end.

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

## A further step, deliberately not taken here

Reporting a counterparty's figure in its own unit (rule 2) is not the same as
**billing** different groups in different currencies. The latter would mean
`GATEWAY_BILLING_CURRENCY` becoming a default rather than a constraint, and it
has one sharp edge that wants its own decision: a cost limit of "EUR 10 per day"
cannot count USD spend. Counting it as though it were EUR is silently wrong;
ignoring it leaves a group with no cap at all. The fail-visible shape would be a
cost rule that declares its currency and counts only matching rows, with the
console warning when a group has spend in a currency no rule covers. Requests,
tokens and image metrics are unit-free and unaffected.

Not built, and not blocking: every price and every reported figure in this
deployment is EUR.

## Resolved by review, 2026-08-22

1. **May a counterparty be the billing truth?** **Yes, configurably** — see
   decision 6. The earlier "no" was too restrictive: it protected the ledger's
   arithmetic at the cost of forbidding pass-through cost recovery, which is a
   legitimate accounting policy the platform should not decide on a deployment's
   behalf. What survives from the objection is the *auditability* requirement:
   both figures are recorded in both modes, so a divergence is always
   reconstructable, and a fallback is never silent.
2. **In-tree or entry-point discovered?** **Both** — in-tree for the plugins we
   maintain, so they are reviewed and tested with the gateway, and the
   `llmp.providers` entry point supported so a deployment can add a counterparty
   without forking. Same shape as ADR 0026.

## Resolved while building slice 3, 2026-08-24

Both open questions turned out to have the same answer, arrived at from two
directions.

1. **How is a plugin bound to a provider row?** `providers.plugin`, a plain
   string, validated **at save time** by `PluginName` in `schemas.py` against the
   registry — not at startup. Startup validation was the redaction parallel, but
   the cases differ: there is one redaction engine for the process and a plugin
   per provider row, so a startup check would refuse to boot over a row that no
   traffic touches.

2. **What happens to a provider row whose plugin disappears?** It degrades to
   generic and says so at ERROR, in **two** places, and the two consequences are
   not the same:

   * `RequestAccounting` loses the counterparty's reported cost and its serving
     endpoint. Bad, and recoverable — the row is still billed from our prices.
   * `ProviderRegistry` presents the credential as a bearer token, which for a
     row that needed `x-api-key` is a 401 that reads like an outage. Worse, and
     the reason its log line names the plugin.

   Refusing to serve the provider was the other candidate. Rejected: it turns a
   missing optional package into an outage for every model behind that provider,
   where degrading turns it into a wrong number in a reconciliation column. Both
   are bad; only one is silent, and the log lines are what stop it being.

## Slice 3: the three columns, removed

Built 2026-08-24. `auth_scheme`, `forward_stream_options` and
`upstream_cost_unit` are gone, replaced by `auth_headers` and `prepare_payload`
on the plugin, plus the plugin's own knowledge of its counterparty's cost unit.

**One value was translatable and is translated.** `auth_scheme` had two values
and its second existed because Anthropic's own API rejects a bearer token.
Dropping the column without a home for that answer would have removed the
capability, so there is now an `anthropic` plugin and migration 0010 rewrites
`auth_scheme = 'x_api_key'` to `plugin = 'anthropic'`. Such a row authenticates
exactly as before, and gains the `anthropic-version` header it also needed and
previously had to be given by hand through `extra_headers`.

**Two values were not, and this is the cost of the decision.** A per-row knob
became per-plugin behaviour, so a deployment with two providers of the same type
that need different answers can no longer express that. The replacement is a
plugin — twenty lines and an entry-point name. That is a higher bar than a
checkbox, deliberately for the cost unit (a wrong unit is a reconciliation report
off by a factor of a million, which reads as a provider overcharging rather than
as a typo) and reluctantly for the stream option.

Concretely, two behaviours change at upgrade:

* A provider row on the `cortecs` plugin **stops sending**
  `stream_options.include_usage`. Cortecs reports usage either way — measured —
  so this buys nothing and may buy routing breadth back; whether it does is still
  unverified, because they do not name the serving sub-provider in stream frames.
* A generic provider row with a declared cost unit **stops recording** the
  counterparty's figure. Past rows keep what they were recorded with.

Migration 0010 prints a line naming every affected provider rather than failing
or being silent. Failing would leave a deployment unable to upgrade over a
configuration question; silence would let a reconciliation column quietly stop
being populated. It also cannot restore those two values on downgrade, which is
what the warning is for.

**The console gained the simplification this was for.** The provider dialog
asked for four things — type, billing mode, credential header, stream options —
where three of them were the same question asked in different words. It now asks
for the type, and derives the rest.

### A bug found while doing it

`AdminProviders.test.tsx` asserted that the type column read `cortecs`, which is
the *fallback* the cell renders before `/api/admin/provider-plugins` resolves;
once loaded it reads `Cortecs (router)`. The assertion was passing on a transient
state and only failed when unrelated copy changes shifted the timing. Now it
waits on the label, which is both the deterministic assertion and the one that
describes what an operator sees.
