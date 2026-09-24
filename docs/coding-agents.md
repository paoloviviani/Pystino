# Coding agents: enrolling opencode against the gateway

A coding agent on somebody's own machine is a `/v1` client like any other —
but it is the one client that cannot hold a credential the way a browser or a
server can. It runs unattended, it is configured by a file on disk, and the
person it works for is not watching. This page is what this repository ships
to wire one up, and why each piece exists.

The machine side is one binary, `pystino-agent` (`deploy/agent/`), plus the
pasted-key fallback in `deploy/opencode/install.sh`.

## One binary, two jobs

| Job | What it carries | Command |
|---|---|---|
| **LLM** | opencode → the local refreshing shim → gateway `/v1`, billed to the signed-in person | `pystino-agent enroll`, then the shim `run` starts |
| **control** | the machine dials *out* to the chat over WSS (`/chat/api/v2/code/machine`), so the chat's `/code` panel can drive it | `pystino-agent run` |

There is no relay and no daemon to pair: the machine connects outbound with its
own enrolment token, and a person confirms it in the `/code` panel
(`deploy/agent/PROTOCOL.md` §4). The stack side is one switch,
`pystino init --agents` (`CODE_AGENTS_ENABLED=true`); the machine's public OIDC
client `opencode-enrollment` ships in the bundled Authelia.

## Two ways in

| How | Credential | Bills |
|---|---|---|
| `deploy/opencode/install.sh` | a `gwk_…` API key pasted by a human (ADR 0010) | the key's group |
| `pystino-agent enroll` + `run` | an OIDC enrolment (device or loopback flow), renewed by the local shim (ADR 0040) | the signed-in person, `x-bill-to` their chosen group (ADR 0061); the machine appears in the chat's `/code` panel |

`install.sh` never mints the key: `POST /api/me/keys` is session-cookie only,
so a human mints it in the console and pastes it at a hidden prompt. The key
is never taken as an argv flag — argv leaks through `ps` and shell history.

## The enrollment CLI

`deploy/agent/` is a small Go program with subcommands. Build it from that
directory (`go build -o pystino-agent .`, Go 1.24+), or cross-compile for a
target machine (`GOOS=darwin GOARCH=arm64 go build …`); it has no runtime
dependency beyond `opencode` itself.

```
pystino-agent enroll   sign in, pick a billing group, write opencode.json,
                        store the refresh credential
pystino-agent serve    the local refreshing proxy shim opencode points at
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
  file exists for a reader with no loopback access — `run`, which reports it
  to the chat over the machine link — and the endpoint for one that has it; both always agree,
  because the endpoint reads the same in-memory status the file was last
  written from.

### `enroll`

```
pystino-agent enroll [--issuer URL] [--gateway URL] [--client-id ID]
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

## Setting up a machine

```sh
# 1. the binary (built from deploy/agent/, or copied from a release build)
install -m 0755 pystino-agent ~/.local/bin/pystino-agent
# 2. enrol: sign in (device flow on a headless box), pick a billing group,
#    write opencode.json, store the refresh credential (mode 0600)
pystino-agent enroll --issuer https://llm.example.org/authelia \
  --gateway https://llm.example.org --cerea https://llm.example.org/chat \
  --output ~/.config/opencode/opencode.json [--device] [--allow-free-models]
# 3. run: supervise opencode and dial out to the chat
pystino-agent run
```

`--allow-free-models` lets `run` offer models from providers other than the
gateway's own (by default only `pystino/*`, so spend lands where the machine
enrolled). `--allow-auto-accept` and `--workspace-root` are the machine's own
vetoes, fixed at enrol time (PROTOCOL.md §4). To keep `run` alive across
reboots, install it as a user service — a systemd user unit on Linux, a
LaunchAgent on macOS; the deployment re-architecture report's cutover plan has
both, verbatim.

### The permission posture

opencode's permission rules live in the machine's own config. `enroll` writes
`--output` whole (it asks before replacing an existing file; `--yes` does not
ask), so point it at a dedicated path, or re-add your own rules afterwards. The
chat panel's approval card is the gate because the machine asks.

## Where the rest of it is documented

The `/code` panel and the machine link are the chat's, not the gateway's. See the chat repository's
`docs/code-panel.md` (operators) and `docs/agent-machines.md` (the person at
the keyboard).
