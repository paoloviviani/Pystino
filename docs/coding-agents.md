# Coding agents: what the gateway provides

A coding agent on somebody's own machine is a `/v1` client like any other, except
that it cannot hold a credential the way a browser or a server can. It runs
unattended, it is configured by a file on disk, and the person it works for is not
watching. This page is the **gateway's side** of that contract: how such a machine
authenticates to `/v1`, which group it bills, and what the identity provider must
provide.

The machine side lives in the chat repository. galopin, the Cerea machine agent, is
its `agent/` directory. It enrols with the IdP, runs a local refreshing shim in front
of `/v1`, supervises opencode, and dials out to the chat's `/code` panel. Its
installation and operation are documented in the chat repository's
`docs/agent-machines.md` and `agent/PROTOCOL.md`. The stack side is one switch here,
`pystino init --agents` (`CODE_AGENTS_ENABLED=true`).

## Two ways in

| How | Credential | Bills |
|---|---|---|
| `deploy/opencode/install.sh` | a `gwk_…` API key pasted by a human (ADR 0010) | the key's group |
| galopin (`galopin enroll`, then `galopin run`) | an OIDC enrolment (device or loopback flow), renewed by galopin's local shim (ADR 0040) | the signed-in person, with `x-bill-to` set to their chosen group (ADR 0061); the machine also appears in the chat's `/code` panel |

`install.sh` never mints the key. `POST /api/me/keys` is session-cookie only, so a
human mints the key in the console and pastes it at a hidden prompt. The key is
never taken as an argv flag, because argv leaks through `ps` and shell history.

## What `/v1` sees from an enrolled machine

- `Authorization: Bearer <access token>` for the `opencode-enrollment` client. It is
  validated locally like every OIDC bearer: issuer, audience, signature, expiry.
  The shim refreshes it before it expires, and opencode never holds it.
- `x-bill-to: <group>`: the group chosen at enrolment, which must be one of the
  person's groups (ADR 0061). Spend lands in the gateway ledger under that group,
  where the console shows it. `/v1` has no usage endpoint of its own.
- Revoking the grant at the IdP ends the machine's `/v1` access and its chat link
  together within one access-token lifetime, because both use the same enrolment
  credential.

## What the IdP side needs

The installer writes a third OIDC client beside the console's and the chat's:
`opencode-enrollment`. It is **public** — a binary on a user's machine cannot
keep a secret — so there is no client secret, `token_endpoint_auth_method` is
`none`, and PKCE S256 is mandatory so a stolen code is useless without the
verifier. Redirect URIs are loopback only.

Two details are easy to get wrong, and both were found live:

- **`offline_access` must be in `scopes` _and_ `refresh_token` must be in
  `grant_types`.** The token endpoint issues a refresh token only to a client
  whose grant types carry the refresh-token grant; `offline_access` alone is
  not enough. The symptom is a device flow that approves cleanly and a shim
  that then has no refresh credential at all.
- **The device grant needs Authelia 4.39.22 or later.** 4.39.0 introduced it
  and later 4.39.x fixed its bugs, which is why
  `deploy/compose/docker-compose.idp-authelia.yml` pins `4.39.22` rather than
  floating on `4.39`.

The `groups` scope matters here for the same reason it matters for the chat:
`/v1` access tokens are validated locally with no userinfo round trip, so a
claim that is not in the token is not seen, and the enrollment would bill to
no group (ADR 0061).

### An agent machine needs a longer-lived refresh token than a browser does

A browser session logs out; an agent machine sits idle over a weekend and is
expected to still work on Monday. The two bundled IdPs' platform defaults are
sized for the browser case, not this one:

- **Authelia's** default `refresh_token` lifespan is 90 minutes —
  `deploy/idp/generate-authelia-config.sh` defines a custom lifespan profile,
  `agent_machine` (`access_token: 1h`, `refresh_token: 90d`), and assigns it
  to the `opencode-enrollment` client only; the console and chat clients keep
  the platform default, since their sessions really are browser-length.
- **Keycloak's** realm-level offline-session idle timeout (what actually
  governs an `offline_access` refresh token's lifetime) is left unset in
  `deploy/idp/keycloak-realm.json.template`, which means Keycloak's own
  default applies: **30 days**. That is already long enough for this use
  case, so there is nothing to override — noted here so the absence reads as
  a decision, not an oversight.

## Where the rest of it is documented

The machine agent, the `/code` panel and the machine link are the chat's. See the chat
repository's `agent/` (galopin's code and `PROTOCOL.md`), `docs/agent-machines.md`
(installing and running a machine) and `docs/code-panel.md` (operators).
