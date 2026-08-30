# 0044 — Keycloak leaves the stack; OIDC becomes generic-provider-only

- Date: 2026-08-29
- Status: **accepted, built**
- Supersedes the *deployment* aspect of [0011](0011-oidc-integration.md)
  (which verified the OIDC client against a bundled Keycloak) and the
  deployment aspect of [0035](0035-public-tls-exposure.md) (whose four holes
  included Keycloak's dev-mode admin console and seeded users). Both ADRs'
  protocol decisions — PKCE, nonce, JWKS handling, the configurable groups
  claim — are untouched and remain in force.
- Related: [0043](0043-local-authentication.md) (local sign-in, which took over
  the "default way in" role), [docs/oidc-generic-provider.md](../oidc-generic-provider.md)
  (how to connect any provider, with a GitLab worked example).

## Context

Keycloak entered this stack in Phase 1 as the development identity provider:
a seeded realm whose users pin the provisioning branches, driven end to end by
`scripts/test_oidc_flow.py`. It earned its keep — ADR 0011's two-hostname
problem and the MissingGreenlet bug were both found against it.

But it never stopped being the *largest* component for what it did. A
personal deployment carried a JVM serving one person, `start-dev` with an
in-memory realm, a container-restart-wipes-everything caveat, and three
environment variables existing only to rotate the credentials the fixture
published. And local authentication ([ADR 0043](0043-local-authentication.md))
has since given a deployment a first-class way in that needs no IdP at all.

The decision: **remove Keycloak from the repository entirely.** OIDC stays —
it is the right protocol for organisations with a directory — but it is now
configured against whatever provider the operator already runs, rather than
shipping one.

## Decisions

### Delete, not deprecate

`docker-compose.keycloak.yml`, `deploy/keycloak/` (realm seed),
`docker-compose.overlay.yml` and `docker-compose.chat-overlay.yml` (both
Keycloak-coupled), and the three scripts that *are* the Keycloak flow
(`test_oidc_flow.py`, `test_bearer_tokens_live.py`, `test_chat_live.py`) are
deleted outright. A fixture nobody runs is worse than no fixture: it reads as
a supported path and rots silently.

The ADRs stay. 0011 and 0035 record why the OIDC client and the TLS proxy are
built the way they are; their Keycloak sections are now history, and the repo
rule is that a reversed decision is documented, not erased.

### The OIDC client is already generic — so configuration just passes through

`gateway/oidc.py` was written provider-agnostic from the start (discovery,
configurable claim, JWKS cache with rotation refetch). The only
Keycloak-shaped thing left was compose hardcoding its issuer. The proxy
overlay now passes `GATEWAY_OIDC__*` and `CHAT_OIDC__*` through from
`deploy/.env` with inert defaults, so connecting GitLab — or Entra, Okta,
Keycloak hosted elsewhere — is editing `.env` and restarting, exactly
[docs/oidc-generic-provider.md](../oidc-generic-provider.md). Disabled until
an issuer is named: with local auth on, a deployment with no IdP is complete,
not crippled.

### The live rig keeps its login through the feature that replaced the IdP

Four live scripts (`test_console_live.py`, `test_providers_live.py`,
`test_surfaces_live.py`, `test_public_tls_live.py`) needed *a session*, not
Keycloak specifically. `scripts/live_session.py` now signs in with local
password auth — `POST /auth/login`, the same cookie the OIDC callback mints —
so the checks run on a deployment with no identity provider at all, and
exercise ADR 0043 on every live run. Admin credentials come from
`GATEWAY_LOCAL_ADMIN_*`; the non-admin checks (403s, per-user routing) skip
with a reason when `GATEWAY_LOCAL_USER_*` is not configured rather than
failing, since the check is about authorisation, not provisioning.

The three deleted scripts' coverage is genuinely gone until an IdP is
connected: the authorization-code flow end to end, access tokens on `/v1`
(ADR 0040's live half), and the chat's streamed turn. The unit suite still
covers the flow logic against a stub client; what is lost is the
against-a-real-provider run — which is precisely the check ADR 0011 says to
repeat before going live with any provider.

### What was deliberately not built

- **No Generic-IdP test fixture.** Standing up a second fake OIDC server in
  compose would recreate the weight just removed. The stub client in the unit
  suite covers the protocol; a real provider is covered by following the doc
  against the real thing.
- **No self-service registration, no password reset** — unchanged from 0043.

## Consequences

- The stack is two fewer containers, and `deploy/.env` loses three required
  variables (`KEYCLOAK_ADMIN_PASSWORD`, `KEYCLOAK_SEED_PASSWORD`,
  `KEYCLOAK_CLIENT_SECRET`).
- **The GitLab nonce question was checked and answered**: gitlab.com's
  discovery document (`claims_supported`, read 2026-08-30) names no `nonce`
  claim, so its ID tokens do not carry one. The gateway's nonce check is
  therefore **tolerant of absence** — a token with no nonce is accepted (the
  spec makes the claim optional; a provider that never mints one cannot be
  required to), while a token carrying a *mismatched* nonce is still refused,
  which catches every replayed token that names a nonce at all. What is lost
  is protection against a stolen nonce-less token, which is protection that
  provider never offered. The same tolerance is applied in chat-api's own
  validator, which is a deliberate copy — the chat-api does not import
  gateway code (ADR 0040).
- Everything else was verified against gitlab.com's live discovery document:
  issuer `https://gitlab.com`, RS256-only signing, PKCE S256, and the
  `groups_direct` claim in the ID token.
- Existing deployments that were mid-upgrade from the Keycloak shape will
  re-provision users on their next OIDC login if the issuer string changed —
  the `(issuer, subject)` trap, unchanged and documented.
- CLAUDE.md's ground rule 4 shrinks from four holes to three: with no IdP in
  the stack, "admin/admin on the dev IdP" and "seeded passwords in the repo"
  stop being deployment holes; the local admin's password in gitignored
  `deploy/.env` and the Secure-cookie rule remain.
