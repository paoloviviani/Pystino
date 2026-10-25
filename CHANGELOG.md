# Changelog

## Unreleased

### Added

- **A price per request for web-search backends.** Searches through a backend
  of the gateway's own (`POST /v1/search`, `POST /v1/search/{backend}`) were
  recorded at a cost of 0, which made the usage table look wrong. An
  administrator now sets a price per request for each backend on the Web search
  screen (with an optional per-tier override, in any currency the ECB publishes
  a rate for, defaulting to the billing currency). The price is effective-dated
  and append-only like a model price, so a change never rewrites what an
  earlier search was billed.
  - Each search records its cost at request time: `cost`, `cost_native` /
    `cost_currency`, `computed_cost`, and the new `usage_records.search_price_id`.
    Cost quotas count it in the reservation and when settled.
  - A price of **0** means "billed out of band": cost 0, with a price reference.
    It is recorded apart from **unpriced** (no price set: cost 0, no reference).
  - Every backend starts unpriced, so upgrading bills nothing until a price is
    set. Reports show the unpriced searches beside the searches column and as a
    note, and the usage CSV gains a trailing `unpriced_own_searches` column.
  - New admin endpoints: `GET` and `POST /api/admin/search-prices`.
  - Migration 0072 is additive (a `search_prices` table and a nullable column).

### Notes

- Searches made before this release are **not repriced**: their rows keep a
  cost of 0 and no price reference, and reports count them as unpriced.
