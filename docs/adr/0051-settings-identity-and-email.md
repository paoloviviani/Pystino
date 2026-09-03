# 0051 — The Settings screen: identity providers and the mail server as console decisions

- Date: 2026-09-03
- Status: **accepted, built**
- Supersedes the connection boundary of [0048](0048-oidc-policy-configuration.md)
  ("connection is environment, policy is configuration"); amends
  [0011](0011-oidc-integration.md) (one provider) and
  [0043](0043-local-authentication.md)/[0049](0049-password-reset.md) (SMTP by env).
- Requested as: identity providers configurable from the GUI, possibly more
  than one; the platform sends emails, configurable and testable from the GUI;
  all of it in a Settings screen.

## Context

0048 kept issuer, client secret and redirect in the environment because
making the IdP connection hot would put its reachability on the request path.
That reasoning survives — what changed is the mechanism: the configuration
moved into the database and the *client* became lazy, so IdP reachability
touches only the login that chose to use it, never a request that did not.

## Decision

**Two connection tables, seeded from the environment, afterwards owned by the
console.**

- `email_settings` — one row (there is one mail server). Empty means ADR
  0049's env SMTP stands, so the upgrade is silent. The password is encrypted
  at rest (ADR 0027's box) and write-only to the console: an edit that only
  fixes a port never re-types a password it was never shown.
- `identity_providers` — one row per directory. Name (the sign-in button's
  slug), issuer, client id, **secret encrypted at rest** (it is the
  credential that mints identities), scopes, group claim, userinfo toggle,
  **per-provider IdP→local group mappings**, enabled. Users stay keyed on
  `(issuer, subject)`, so two providers are two account namespaces by design.
- **Seeding**: startup inserts the environment's provider as the first row
  when the table is empty (and the email fallback simply remains when the
  email row is absent). An upgrading deployment logs in unchanged; from then
  on the rows are editable like any other. Deleting every provider row means
  no OIDC sign-in — the env fallback applies only while the table is empty.
- **Clients are built on demand and cached by (row identity, `updated_at`,
  origin)**: a configuration change takes effect on the next login,
  discovery is fetched lazily per client, and the redirect URI each provider
  registers is `{origin}/auth/callback/{name}`, namespaced so two directories
  cannot deliver a code to the wrong flow.
- **Multiple providers**: `GET /auth/methods` reports one entry per enabled
  provider; the sign-in page renders a button each. Exactly one provider and
  no local form keeps the old auto-redirect. The `/v1` bearer path routes a
  token to its provider by the **unverified** `iss` claim — a routing label
  between configured providers, never a trusted one; the signature check
  against the chosen provider's keys decides everything else.
- **The test button**: `POST /api/admin/email/test` sends a real message and
  returns the delivery outcome, because a wrong host or a refused TLS
  handshake is exactly the thing to see *before* a person sits on a reset
  page wondering where the mail went.

## Consequences

- The bearer path's pinned round-trip budget moves three selects → four: the
  routing lookup is an indexed select on a table with a row per configured
  IdP (test_query_counts.py carries the reasoning).
- Changing a provider's issuer or secret takes effect on the next login, not
  instantly mid-session: clients are cached per configuration, and existing
  sessions are stateless cookies that re-verify nothing.
- The per-provider mapping/claim settings move out of the global policy —
  they were per-provider facts that one global row was holding badly. ADR
  0048's provisioning policy (auto-provision, unknown-user rule, admin
  groups) stays global.
- `GATEWAY_OIDC__*` and the ADR 0049 SMTP env vars are now seed values and
  fallbacks, not the configuration.
