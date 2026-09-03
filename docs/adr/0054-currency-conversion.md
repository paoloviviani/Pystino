# 0054 — Currency conversion at the decision and aggregation points

- Date: 2026-09-03
- Status: **accepted, built**
- Supersedes the currency rule of [0014](0014-model-catalogue-and-pricing.md)
  ("currency mismatches are refused, not converted") and removes
  `CurrencyMismatch` from cost computation.
- Requested as: automatic conversion for USD-priced models — the rate fetched
  from an API, cached, used for the whole day, last known on failure — applied
  to quota checks and aggregate figures; per-provider and per-model breakdowns
  report native figures.

## The policy, precisely scoped

**Conversion happens at exactly two points, and nowhere else:**

1. **Admission** — the quota estimate a request reserves against limits set in
   the billing currency. No rate available → the request is **refused** with a
   400 naming the missing pair, never served unmetered and never billed at a
   guessed rate.
2. **Settle** — the recorded billing-currency cost, with the day's rate stored
   on the row (`cost_fx_rate`) so any converted aggregate is reconstructable
   and auditable.

Everything else stays native. `compute_cost` computes in the price's own
currency; the usage row records `cost` (billing currency, what quotas and
aggregates read) beside `cost_native`/`cost_currency` (what the counterparty
actually charges); the per-model breakdowns in reports surface the native
figure, because a model's rows share one price currency and its native sum
means something. A group, user or day mixes currencies and reports in the
billing currency alone.

## The rate

- **Source**: frankfurter.dev — the ECB's reference rates, no key, no
  registration. Verified live on 2026-09-03 (1 USD = 0.86371 EUR).
- **One fetch per pair per day**, persisted in `fx_rates` and served from an
  in-process cache: the hot path's read is a dictionary lookup, and the
  database is touched at most once per day per pair.
- **Fallback**: if the API does not answer, the **last known rate** is used,
  however old — and the row's `fetched_at` says so. The fallback lives in the
  database rather than a rebuildable cache, because a cache flush turning
  every USD-priced model into an outage is the wrong trade.
- The poller warms the pairs the catalogue actually prices, computed rather
  than configured: a new USD-priced import starts converting with no settings
  change.

## What this supersedes

ADR 0014's rule was right about the failure mode and wrong about the
frequency: "an exchange rate applied silently produces invoices that look
right and are wrong" is true of *silent* conversion at unknown rates. This
design makes the conversion loud — the rate is on every converted row, the
source and date are reported, and the fallback degrades to last-known rather
than to zero. The refusal it replaces had become the thing standing between
the community price catalogues (all USD) and every EUR-billing deployment.

## Consequences

- Quota counters and reports sum `cost` across rows in one currency, so the
  quota engine needs no currency awareness — the conversion happens before
  anything is counted.
- A rate fetched from a free API is a rate that can be wrong by a small
  percentage. Accepted, with the error bounded by the daily cadence and the
  source being the ECB's own reference rate rather than a spread-adding
  converter.
- The reconciliation report stays untouched: it compares our figure against
  the provider's reported figure per provider, in whatever units each side
  reported — no conversion there either.
- `PUT /api/me/limits` style notification mails (ADR 0052) name percentages,
  not amounts, so they are conversion-free by construction.
