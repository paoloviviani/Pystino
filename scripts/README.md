# Operational scripts

## `import_cortecs_pricing.py` — implemented

Imports per-model prices from the Cortecs catalogue into `model_prices`.

```bash
uv run python scripts/import_cortecs_pricing.py            # dry run (default)
uv run python scripts/import_cortecs_pricing.py --apply     # write the prices
```

Deliberately a script rather than a background job inside the gateway: pricing
changes are an administrative act, and a provider's catalogue should not be able to
silently change what the foundation charges its own groups.

It never creates models. Which models exist, and which groups may reach them, stays
an administrative decision — the importer only prices models already in the
catalogue, and names everything it skipped.

Prices are append-only and effective-dated, and a new row is written only when the
price actually changed. Running it nightly is therefore safe and does not fill the
table with identical rows.

## `check_cortecs_catalogue_tags.py`, `check_cortecs_stream_options.py`

Read-only probes against the live Cortecs API, kept because each answers a
question no documentation does: which catalogue tags `/v1/models` accepts
(it defaults to `tag=Instruct`, which is how fourteen models sat unseen), and
whether sending `stream_options` changes anything there. They spend nothing.

## The live checks

Everything named `test_*_live.py` runs against a **running stack**, not the
unit suite: `bill_to`, `cache_accounting`, `citations`, `console`, `providers`,
`public_tls`, `pystino_usage`, `quota_race`, `redaction`, `reporting`,
`surfaces`, `web_search`. They sign in through the gateway's OIDC login and
the bundled Authelia, as a browser does, via `live_session.py`. See
[Operations](../docs/operations.md#the-live-checks) for what each one covers
and the environment they need.

`benchmark_live.py` is the same shape for performance, and reproduces every
figure in [Measured performance](../docs/performance.md).

## The opencode bootstrap that was planned here, and where it went instead

An earlier plan put a device-flow script here that would mint a **gateway API
key** for a CLI. It was not built, and it should not be: the flow now
authenticates against the deployment's **identity provider** rather than the
gateway, and what it keeps is a refresh credential rather than a key — which
is what lets spend land on the signed-in person's own account instead of a
standing secret on a laptop (ADR 0040, ADR 0061).

That work lives in the chat repository (galopin, the Cerea machine agent, in its
`agent/` directory); the gateway's side of the contract is
[docs/coding-agents.md](../docs/coding-agents.md). The gateway still has no
device-flow endpoints of its own, and now needs none.
