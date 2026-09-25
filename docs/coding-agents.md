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
`docs/agent-machines.md` and `agent/PROTOCOL.md`. The stack side is one switch,
`CODE_AGENTS_ENABLED=true` in `.env` (the full stack's; a Pystino-only
deployment has no chat, so no `/code` panel to dial into).

## Two ways in

| How | Credential | Bills |
|---|---|---|
| `deploy/opencode/install.sh` | a `gwk_…` API key pasted by a human | the key's group |
| galopin (`galopin enroll`, then `galopin run`) | an OIDC enrolment (device or loopback flow), renewed by galopin's local shim | the signed-in person, with `x-bill-to` set to their chosen group; the machine also appears in the chat's `/code` panel |

`install.sh` never mints the key. `POST /api/me/keys` is session-cookie only, so a
human mints the key in the console and pastes it at a hidden prompt. The key is
never taken as an argv flag, because argv leaks through `ps` and shell history.

## What `/v1` sees from an enrolled machine

- `Authorization: Bearer <access token>` for the `opencode-enrollment` client. It is
  validated locally like every OIDC bearer: issuer, audience, signature, expiry.
  The shim refreshes it before it expires, and opencode never holds it.
- `x-bill-to: <group>`: the group chosen at enrolment, which must be one of the
  person's groups. Spend lands in the gateway ledger under that group,
  where the console shows it. `/v1` has no usage endpoint of its own.
- Revoking the grant at the IdP ends the machine's `/v1` access and its chat link
  together within one access-token lifetime, because both use the same enrolment
  credential.

## What the IdP side needs

The bundled Authelia has a third OIDC client beside the console's and the chat's:
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
  and later 4.39.x fixed its bugs, which is why the compose files pin
  `4.39.22` rather than floating on `4.39`.

The `groups` scope matters here for the same reason it matters for the chat:
`/v1` access tokens are validated locally with no userinfo round trip, so a
claim that is not in the token is not seen, and the enrollment would bill to
no group.

### An agent machine needs a longer-lived refresh token than a browser does

A browser session logs out; an agent machine sits idle over a weekend and is
expected to still work on Monday. Identity providers size their defaults for
the browser case:

- **Authelia's** default `refresh_token` lifespan is 90 minutes. The bundled
  configuration (`authelia/configuration.yml`) defines a lifespan profile,
  `agent_machine` (`access_token: 1h`, `refresh_token: 90d`), and assigns it
  to the `opencode-enrollment` client only; the console and chat clients keep
  the default, since their sessions really are browser-length.
- **Keycloak's** realm-level offline-session idle timeout (what governs an
  `offline_access` refresh token) defaults to **30 days**, which is long
  enough for this use case. With your own Keycloak, register an
  `opencode-enrollment` client in the same shape as above.

## Where the rest of it is documented

The machine agent, the `/code` panel and the machine link are the chat's. See the chat
repository's `agent/` (galopin's code and `PROTOCOL.md`), `docs/agent-machines.md`
(installing and running a machine) and `docs/code-panel.md` (operators).
