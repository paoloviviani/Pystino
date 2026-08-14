# 0014 — The model catalogue and the Cortecs pricing importer

- Status: accepted
- Date: 2026-08-14

## Context

Per-model pricing must be configurable, with a separate importer pulling prices from the
Cortecs catalogue. Prices feed every cost calculation, so a price read wrong is as
damaging as broken arithmetic.

## The catalogue

Three tables, deliberately separate:

- **`models`** — what exists. `name` is what clients send (ours to choose);
  `upstream_model` is what we send upstream. Decoupling them means a model can be
  renamed or repointed at a different provider without breaking clients.
- **`model_prices`** — append-only, effective-dated. See
  [0008](0008-accounting-model.md).
- **`group_model_access`** — which groups may use which models. **Absence of a row means
  no access.** There is no global allow-all: a model nobody has been granted is
  invisible, which is the safe default for a per-group billing system.

`GET /v1/models` is served from this catalogue, filtered by the caller's group
memberships (the union across all their groups, so a client discovering models sees
everything it could reach after switching group). It is **never proxied from the
upstream** — see [0003](0003-build-vs-adopt-gateway.md) for why an upstream's model list
is not trustworthy as a source of truth.

"Model does not exist" and "your group may not use it" both return **404**. Which models
another group can reach is not this caller's business.

## The importer

`scripts/import_cortecs_pricing.py`, with the logic in `gateway/pricing.py` so it is
testable.

Verified from docs.cortecs.ai: `GET https://api.cortecs.ai/v1/models`, auth optional
(supplying a key narrows the result to what the account can reach), and per model a
pricing object with `input_token` and `output_token` **per million tokens**, a
`currency`, and optionally `cache_read_cost` / `cache_write_cost` on the same basis.
Cortecs bills natively in EUR, which is why it is the default upstream for a European
foundation.

Design decisions:

- **A script, not a background job.** Pricing changes are a deliberate administrative
  act. A provider's catalogue must not be able to silently change what the foundation
  charges its own groups.
- **Dry run by default.** `--apply` is required to write anything.
- **Never creates models.** Which models exist, and which groups may reach them, stays
  administrative. The importer only prices models already in the catalogue and **names
  everything it skipped** — a model that silently fails to import looks exactly like a
  free model.
- **Matches on `upstream_model`** first, since the catalogue publishes the upstream id.
- **Writes a new row only when the price actually changed.** Otherwise a nightly cron
  appends an identical row every night and makes the history unreadable.
- **Currency mismatches are refused, not converted**, and reported by name.
- Prices are parsed via `Decimal(str(value))` — never through float, because
  `Decimal(0.15)` is not `0.15`.
- `--save-to` and `--from-file` exist so a change can be captured and reviewed before it
  is applied.

## Stated uncertainty

The **pricing fields** were verified against the Cortecs documentation. The **envelope**
around the model list was not verified against a live response. The parser therefore
accepts the plausible shapes (`{"data": [...]}`, `{"models": [...]}`, a bare list, and
flat or nested pricing keys) and reports anything it cannot read rather than guessing.

If the first real run reports "no prices could be read", use `--save-to` to capture the
response and compare it with the key lists at the top of `gateway/pricing.py`.

## Consequences

- Audio and speech pricing (`audio_cost` per second, `speech_cost` per million
  characters) exist in the catalogue and are **not** modelled. Add columns before
  serving audio models, or their cost silently records as zero.
- `gateway seed` creates a working group/model/price/key so `docker compose up` yields a
  system you can actually send a request to. Without it the first attempt returns 401
  then 404.
- A model with no price is servable and records `cost = 0` with `price_id = NULL`, so
  unpriced models are findable in reporting rather than invisible.
