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

## `opencode_bootstrap.py` — not written yet (Phase 4)

Will run the OIDC **device authorization flow** so a CLI can get a gateway API key
without a redirect URI:

1. `POST` to the device authorization endpoint, get a user code and verification URI
2. open a browser to the verification URI
3. poll the token endpoint until the user approves
4. exchange the resulting identity for a **gateway API key** via the management API
5. write that key plus the gateway's base URL into opencode's config

Groundwork already in place:

- `gateway/oidc.py` parses `device_authorization_endpoint` out of the discovery
  document, so the endpoint is already available.
- API key minting exists at `POST /api/me/keys` and returns the secret exactly once.

Still to do: the gateway has **no device-flow endpoints of its own** yet. The
management API currently authenticates browser sessions via the authorization-code
flow only, so step 4 has nothing to call. That is the actual work of Phase 4, not
the script.

One design note to settle first: the key minted for a CLI should be pinned to a
billing group and given an expiry, rather than inheriting the user's default
forever. A long-lived unscoped key on a developer laptop is the credential most
likely to leak.
