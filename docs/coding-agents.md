# Coding agents: enrolling opencode against the gateway

A coding agent on somebody's own machine is a `/v1` client like any other —
but it is the one client that cannot hold a credential the way a browser or a
server can. It runs unattended, it is configured by a file on disk, and the
person it works for is not watching. This page is what this repository ships
to wire one up, and why each piece exists.

Everything here lives in `deploy/opencode/`.

## Two axes, and only one of them is this repository's

| Axis | What it carries | Where it is set up |
|---|---|---|
| **LLM** | opencode → the local `serve` shim → gateway `/v1` | here |
| **control** | the paseo daemon → a self-hosted relay → the chat's `/code` panel | the chat's repository (ADR 0085) |

The two are independent. A machine can have the LLM axis alone (opencode
billed to the person, driven from their own terminal) or both (the same
machine also drivable from the chat's Agents panel). Nothing on the control
axis touches `/v1`, and nothing on the LLM axis touches the relay.

`deploy/opencode/setup-agent.sh` sets up both, which is why it takes a
`--relay` as well as a `--gateway`. The `--skip-daemon` flag reduces it to
this page's half.

## Three ways in, in increasing order of what they buy

| Script | Credential | Bills |
|---|---|---|
| `deploy/opencode/install.sh` | a `gwk_…` API key pasted by a human (ADR 0010) | the key's group |
| `deploy/opencode/setup-agent.sh --skip-daemon` | an OIDC enrollment, renewed by a local shim (ADR 0040) | the signed-in person, `x-bill-to` their chosen group (ADR 0061) |
| `deploy/opencode/setup-agent.sh` | the same, plus the daemon pairing | as above, and the machine appears in the chat's `/code` panel |

`install.sh` never mints the key: `POST /api/me/keys` is session-cookie only,
so a human mints it in the console and pastes it at a hidden prompt. The key
is never taken as an argv flag — argv leaks through `ps` and shell history.

## The enrollment CLI

`deploy/opencode/enroll/` is a small Go program with three subcommands. Build
it from that directory (`go build -o pystino-enroll .`); `setup-agent.sh`
builds it for you.

```
pystino-enroll enroll   sign in, pick a billing group, write opencode.json,
                        store the refresh credential
pystino-enroll serve    the local refreshing proxy shim opencode points at
pystino-enroll pair     pair this machine's paseo daemon into the chat's
                        /code panel, using the enrollment's own access token
```

### Why a shim at all

An OIDC access token expires in minutes; opencode holds a **static** `apiKey`
in its config and has no notion of renewal. So the token never reaches
opencode. `enroll` stores the _refresh_ credential (mode 0600, at
`<config-dir>/opencode/pystino-credentials.json`), `serve` holds it, and the
written `opencode.json` points its `baseURL` at
`http://127.0.0.1:<port>/v1` — the shim. Every request gets a fresh bearer
and the recorded `x-bill-to` injected on the way past.

The default shim port is **41871**, bumped upward while occupied and then
recorded in the config, so `enroll`'s own loopback callback listener and the
shim never collide.

### When the refresh token dies

A gateway or IdP redeploy can revoke the refresh token `serve` holds. That is
expected and recoverable — re-enroll the machine — but the failure has to be
fast and legible, not a 502 that opencode's client quietly retries into a
long hang. Three things make that true:

- **A 401, not a 502, once the refusal is permanent.** The token endpoint's
  `invalid_grant` (RFC 6749 §5.2) is the one code that specifically means
  "this refresh token is invalid, expired, or revoked" — nothing a retry
  fixes. The shim answers those with `401` and an OpenAI-shaped body
  (`{"error":{"message","type","code":"enrollment_expired"}}`), which the
  opencode client (built on the Vercel AI SDK) never retries — its
  `isRetryable` is `statusCode in {408,409,429}` or `>= 500`, so 401 stops it
  cold and surfaces the message verbatim instead of exhausting a retry
  budget first. A transient refusal (a network error, a 5xx, any other OAuth
  error code) still answers the old `502`, which opencode does retry.
- **Refreshed proactively, not just on request.** `serve` refreshes once at
  startup and then every 15 minutes, well inside the access-token lifespan
  (see the `agent_machine` lifespan below), so the credential's state is
  already known before opencode ever sends a request. Once a refresh comes
  back `invalid_grant` the loop stops — nothing left to check until a human
  re-enrolls and restarts `serve` — and every request after that is answered
  from the cached verdict with no further IdP round trip.
- **A status file and a health endpoint, kept in sync.** Every state change
  (`ok` / `expired` / `unreachable`) is written atomically to
  `<creds-dir>/pystino-status.json` (next to
  `pystino-credentials.json`) as `{"state","checkedAt","message"}`, and the
  same JSON is served at `GET http://127.0.0.1:<port>/pystino/health`. The
  file exists for a reader with no loopback access — Cerea, through the
  paseo daemon — and the endpoint for one that has it; both always agree,
  because the endpoint reads the same in-memory status the file was last
  written from.

### `enroll enroll`

```
pystino-enroll enroll [--issuer URL] [--gateway URL] [--client-id ID]
                      [--device | --loopback] [--group NAME] [--output PATH]
                      [--creds PATH] [--shim-port PORT] [--no-discover]
                      [--allow-opencode-provider] [--yes]
```

- `--gateway` takes an origin or a `/v1` base; a bare origin gets `/v1`
  appended.
- `--issuer` is prompted when missing. It is deployment-specific — the
  bundled Authelia answers under `<origin>/authelia` and the bundled Keycloak
  under `<origin>/idp/realms/pystino` (ADR 0084) — so there is no default
  worth guessing.
- `--client-id` defaults to `opencode-enrollment`, the id baked into both
  bundled IdPs.
- The flow is picked automatically and can be forced: `--loopback` is the
  browser flow with an ephemeral `127.0.0.1:<port>/callback` redirect,
  `--device` is RFC 8628 for a box with no browser. The two conflict.
- `--group` preselects the billing group; without it the CLI prompts when
  `GET /v1/billing/groups` returns more than one. The header takes the group
  **name**, not the id, so the name is what gets recorded.
- `--no-discover` skips `GET /v1/models` and writes a placeholder model map.
  Discovery never fails the enrollment: an unreachable gateway still leaves a
  working auth setup.

### The device flow prints the code-bearing URL

Both bundled IdPs issue `verification_uri_complete`, which carries the user
code as a query parameter. That is what the CLI prints as `open:`, so one
paste does what open-plus-type would; the bare code is still printed on its
own line, because a wrapped URL pastes broken and a typed code does not. An
IdP that omits the complete URI falls back to `verification_uri`.

### What `enroll` writes into `opencode.json`

```json
{
  "$schema": "https://opencode.ai/config.json",
  "enabled_providers": ["pystino"],
  "provider": {
    "pystino": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Pystino",
      "options": { "baseURL": "http://127.0.0.1:41871/v1" },
      "models": {
        "my-model": {
          "name": "My Model",
          "limit": { "context": 131072, "output": 16384 }
        }
      }
    }
  }
}
```

Three things in there are load-bearing:

- **No `apiKey`.** opencode would send it verbatim and it would expire. The
  shim owns the credential instead.
- **`enabled_providers: ["pystino"]`.** opencode's own allowlist: _"When set,
  ONLY these providers will be enabled."_ Without it, a built-in provider
  with ambient credentials — an `ANTHROPIC_API_KEY` in the environment, a
  logged-in Copilot — offers models that bypass the gateway entirely, and
  that spend never lands in the account the enrollment exists to bill.
  Naming just `pystino` also stays correct when an opencode release ships a
  new built-in, which enumerating disabled providers would not.
  `--allow-opencode-provider` omits the key for operators who want both.
- **`limit.context` and `limit.output` on every model.** Both keys are
  _required_ by opencode's schema for custom-provider models: omit either and
  opencode refuses the whole file with `Missing key
provider.pystino.models.<id>.limit.output`. Where the gateway publishes no
  hint (both fields are nullable on `GET /v1/models`) the CLI writes defaults
  — 131072 context, 16384 output — rather than omitting the key, because an
  omission is a broken file and a default is merely approximate.

Only chat-kind models make the list. An embedding tier in a coding agent's
model picker is one accidental keypress from a 400.

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

## `setup-agent.sh`

```
./deploy/opencode/setup-agent.sh [--relay HOST:PORT] [--relay-tls|--no-relay-tls]
                                 [--gateway ORIGIN] [--issuer ORIGIN]
                                 [--paseo-version X] [--opencode-version X]
                                 [--allow-opencode-provider] [--name NAME]
                                 [--skip-daemon] [--skip-llm] [--skip-posture] [--yes]
```

`PASEO_RELAY`, `PYSTINO_GATEWAY` and `PYSTINO_ISSUER` are the environment
defaults for the first three.

Requirements: `bash`, `node`/`npm`, `python3`, and **Go 1.24 or later** — but
only when the `pystino-enroll` binary needs building. `go.mod` requires 1.24;
an older toolchain tries to auto-download one and dies with "toolchain not
available", so the script checks the version itself and names the remedy
(apt's `golang` is usually older; install the official tarball).

The script **rebuilds the CLI when any `*.go` beside it is newer than the
binary**, not merely when the binary is missing. The earlier "exists, so skip"
rule was silent drift: a box kept running last month's CLI — missing flags,
missing model gating — while every run printed success.

It also retires a previous `pystino-enroll serve` before enrolling. A shim
from an earlier run owns the default port with the earlier run's credentials;
leaving it alive makes `enroll` dodge to a new port that nothing serves while
`opencode.json` points at it (found live: 41872 written, nothing listening).

### The permission posture

The script writes `"permission": {"edit": "ask", "bash": "ask"}` into the
opencode config, merged rather than overwritten. The paseo daemon drops
per-prompt permission rules, so the posture has to live in the machine's
config — that is what makes the chat panel's approval card the gate it claims
to be. `--skip-posture` leaves an existing config alone.

### Pairing, and its fallback

With both axes wired, the last step is automatic: `pystino-enroll pair` runs
`paseo daemon pair --json` for the daemon's pairing offer and POSTs it to the
chat, authenticated by the enrollment's own access token. The chat trusts the
same issuer, so there is no second sign-in and no human paste.

`--skip-llm` has no token to pair with, and a machine that cannot reach the
chat origin has no route: both end with the manual path instead — run `paseo
daemon pair` and paste the printed link into the chat's **/code → Pair a
device** dialog. A failed automatic pairing says so and hands back the same
instructions; the LLM axis is unaffected either way.

## Where the rest of it is documented

The relay, the `/code` panel and what an operator has to deploy for the
control axis are the chat's, not the gateway's. See the chat repository's
`docs/code-panel.md` (operators) and `docs/agent-machines.md` (the person at
the keyboard).
