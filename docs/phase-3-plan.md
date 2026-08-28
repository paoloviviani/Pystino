# Phase 3 — the chat application

Decided 2026-08-28, in a design conversation whose forks are recorded here rather
than left to be re-argued. The goal is a working subset of what Open WebUI offers —
chat, knowledge bases with RAG, personalised assistants, MCP connectors and tools —
built as a **client of the gateway**, never as a second path into providers.

## The principle everything hangs on

**Every chat turn is an ordinary metered `/v1` request.** Quotas, redaction, model
access and the ledger apply to a chat message exactly as they apply to an API call,
because it *is* an API call. This is also the scope filter: any Open WebUI feature
that exists because Open WebUI must be its own gateway is explicitly not built here.

| Open WebUI has | We do instead |
|---|---|
| Provider connections, model configuration | The gateway owns it. Nothing in the chat app knows a provider exists. |
| Who may use which model | `access.py`. The chat UI renders `/v1/models`, which is already filtered per caller. |
| User management, signup, RBAC | Keycloak. |
| API keys, usage dashboards | The console, reached by a link. The Phase 2 note stands: two implementations of "mint a key" is one too many, and the second is where the security bug will be. |
| Local model runtime, model marketplace, arena | Not this platform's problem. |
| Image generation UI | Deliberately out of this phase, by decision — the `/v1/images` surface exists, but no chat UI for it yet. |

Forking was considered and rejected on licence before anything else: Open WebUI
relicensed in 2025 to BSD-3 **plus a branding-preservation clause**, which is not
OSI and fails ground rule 1 outright. LibreChat is MIT, but its core is exactly the
provider/auth/model machinery this table deletes. `packages/ui` was built to be
shared with this app; parity is a feature checklist, not a codebase.

## Architecture

```
apps/chat            React SPA on packages/ui tokens, served by apps/chat-api
apps/chat-api        FastAPI: conversations, messages, assistants, knowledge bases,
                     and the orchestration loop (RAG retrieval, MCP tool calls).
                     Own database (same PostgreSQL instance), own migrations, own
                     OIDC client against the same Keycloak realm.
services/ingestion   out-of-process worker (the redaction pattern): parse → chunk →
                     embed via /v1/embeddings → pgvector
gateway              unchanged except one addition: OIDC bearer tokens accepted
                     on /v1 (see below)
```

### Separate service, same monorepo — and why

"Standalone" cuts two ways, and they were weighed separately:

- **Gateway without chat** is free under every arrangement — `ARG INCLUDE_CONSOLE`
  already produces a headless image, and the pattern extends. The standalone
  gateway, the piece we are sure has value, constrains nothing here.
- **Chat without this gateway** — a chat app pointed at any OpenAI-compatible
  endpoint — is a product that does not exist yet and may never. Building the
  works-against-anything abstraction now is speculative generality.

The position taken is the cheap middle: **coupled only through the gateway's public
surfaces.** No shared SQLAlchemy models, no imports from `gateway.*`, no in-process
calls; all inference over HTTP through `/v1`, the model list from `/v1/models`,
platform administration by link to the console. If a standalone chat ever becomes
real, the remaining work is packaging, not surgery.

What the separation costs, measured before accepting it: duplicated scaffolding
(a second FastAPI skeleton, OIDC session handling, alembic, one more container on
the 3 GB host) — a few days once, a small drag forever. What it does **not**
duplicate is the part that made the monolith tempting: chat has **no money domain
at all**. Spend lives in the console. The `Money` conventions, quota plumbing and
reporting SQL — the code with the sharpest correctness requirements in the gateway
— never appear in the chat service. The one extra localhost hop on the streaming
path is noise against a 24–31 ms gateway overhead.

A separate *repo* was rejected for now: it buys coordination cost from the first
commit for an adoption story that doesn't exist. The split stays mechanical later
only if the boundary above is honoured from day one; that is the discipline this
section exists to record.

### Bearer tokens on `/v1` — the decision that makes the boundary clean

The awkward part of a separate service was always how it calls `/v1` *as the user*.
Auto-minted per-user API keys were considered and dropped: machinery invented to
route around a gap. The fix is closing the gap — **the gateway accepts OIDC access
tokens on `/v1`**, resolved to the same `(issuer, subject)` user row console login
already keys on, quota subject the user's default billing group. The chat backend
forwards the token of the user it is serving; attribution and quotas are exact, and
nothing stores a credential.

This is not chat work smuggled into the gateway: the `opencode` device flow already
on the Phase 3 list needs exactly this — device flow yields OIDC tokens and the CLI
must call `/v1` with them. One implementation, two consumers. `Principal.api_key`
is already `ApiKey | None`; the real schema touch is that a usage row must
attribute to a user without an API key.

### Decisions taken in the design conversation

- **Platform admin is a link, not an embedding.** The identity menu shows
  "Gateway console" to admin-role users — same origin under the proxy, same
  Keycloak session, no re-login. Embedding console screens in the chat shell means
  testing every console change in two hosts and watching the shells drift.
  **Chat-domain administration** — curating shared assistants, the MCP connector
  registry, knowledge-base storage limits — lives in the chat app, role-gated,
  because its objects live there and its audience is the person who runs the
  assistants, not the person who runs the gateway.
- **Users pick models**, per conversation, from exactly what `/v1/models` returns
  for them. Assistants may pin a model; bare chat has an admin-set default.
- **In scope beyond the core**: web search and web fetch as built-in tools, voice
  (STT/TTS), and the code sandbox. **Out**: an image-generation UI (surface
  exists, no UI this phase).
- **Web search is a provider API, not a self-hosted engine.** SearXNG was the
  first thought and was dropped: another always-on service to operate, on hosts
  where RAM is already the constraint, to avoid API costs measured in euros per
  thousand queries. Search providers get a small provider abstraction of their
  own in chat-api — the gateway's plugin lesson applies in miniature: the
  provider returns results, the tool decides what enters the prompt. **Jina
  first; Exa and Staan on the roadmap** (Staan is the European index at €2/1k
  requests, which fits the same preference `eu_native` exists for).
- **Web fetch is local by default** — chat-api fetches the URL itself — **with
  Jina Reader as an opt-in** for JavaScript-rendered pages and clean markdown
  extraction. Local-first because routing every fetched page through a third
  party is a data-egress decision an admin must make, not a default.
- **Code execution is Pyodide** (CPython on WebAssembly, MPL-2.0), not a
  container sandbox. See M6 for what that decision buys and costs.

## Sequence

```
  M0  Bearer auth on /v1        ── gateway; unblocks chat-api and opencode alike
  M1  Chat                      ── proves the architecture; everything after is additive
  M2  Assistants                ── system prompt + model + params + sharing
  M3  Knowledge and RAG         ── services/ingestion, pgvector, citations
  M4  MCP and tools             ── connector registry, approval UI, search + fetch
  M5  Voice                     ── new metered /v1 audio surfaces first, UI second
  M6  Code execution            ── Pyodide in the browser, riding the M4 suspension
```

Each milestone ends deployed on the proxy-overlay host with a live script, per the
house rule: more than half the serious bugs in this project were only findable
against the running stack.

### M0 — bearer tokens on `/v1`

Accept `Authorization: Bearer <JWT>` from the trusted issuer alongside API keys.
Validation against the realm's JWKS (cached, refreshed on unknown `kid`), `iss`
must match discovery exactly — the Phase 2 lesson that `iss` is part of a user's
identity applies with force here, since a token from a re-hosted Keycloak must
*fail*, not provision a ghost user. Usage rows gain nullable key attribution.
Tests: this is accounting-adjacent, so the attribution logic gets unit tests
specifically, plus a live check that a device-flow-shaped token meters correctly.

### M1 — chat

Conversation list, streaming turn (SSE relayed through chat-api; Caddy's
`flush_interval -1` already covers the proxy leg), model picker, markdown and code
rendering, stop / regenerate / edit-and-resend, titles (a cheap-model call, itself
metered), search over own history. Schema: `conversations`, `messages`. Also the
scaffolding bill for the whole phase: chat-api skeleton, OIDC client, compose
service, Caddy route, SPA hosting with the console's CSP posture.

### M2 — assistants

Name, system prompt, model and parameters, knowledge attachments deferred to M3.
Sharing: private / group / everyone — groups come from the gateway's user model via
the OIDC token's claims, not a second membership table. Admin curation of shared
assistants happens here, in the chat app.

### M3 — knowledge bases and RAG

pgvector (PostgreSQL licence) in the chat database; `services/ingestion` out of
process because parsing is CPU-heavy and must not sit near the request path —
the same reasoning that put redaction out of process. Parsing via **docling**
(MIT); `unstructured` was passed over because its useful parsers keep migrating to
the paid tier, the open-core smell ground rule 1 exists to catch. Embeddings via
`/v1/embeddings` so they are metered like everything else. KBs attach to
assistants and to single conversations; per-chat file upload is the same pipeline
with a throwaway KB. Citations link answers to chunks.
**Open dependency, to verify at source before building:** an embedding model must
actually be available on a configured provider.

### M4 — MCP connectors and tools

Admin-registered connector registry, per-user OAuth to connectors that need it,
the tool loop in chat-api, and a **tool-approval prompt in the UI by default** —
silent tool execution against a user's credentials is the security bug waiting to
happen. Tool *results* enter the prompt, so they pass redaction like any other
prompt text; the scoped-rules machinery applies unchanged.

Two built-in tools ship here alongside the MCP loop, because they exercise it:

- **Web search**, through the search-provider abstraction: Jina first, Exa and
  Staan behind the same interface. Keys are admin-configured in chat-api's own
  admin area (chat-domain, not gateway-domain), encrypted the way the gateway
  encrypts provider credentials. Search API spend is not in the gateway's ledger
  — it is not LLM spend — and pretending otherwise would muddy the three cost
  figures; if per-user search accounting is ever wanted, it is a chat-api
  feature, recorded here so nobody reaches for `usage_records`.
- **Web fetch**, local by default with Jina Reader opt-in. The local fetcher is
  where the security bug lives, so it is named now: a URL fetched from inside
  the compose network can reach Valkey, PostgreSQL, Keycloak's admin port and
  the cloud metadata address. The fetcher must resolve the name first and refuse
  private, loopback and link-local ranges **after** resolution — checking the
  URL string instead of the resolved address is the classic SSRF miss, and DNS
  rebinding is why.

### M5 — voice

The gateway grows metered `/v1/audio/transcriptions` and `/v1/audio/speech`
surfaces first — protocols.py readers, pricing units that are not tokens (the
`per_image` precedent), plugin awareness. Only then the UI. Which providers
actually serve STT/TTS is a question for their live catalogues, not memory;
this milestone is sequenced last-but-one because that answer is unverified.

### M6 — code execution

**Pyodide, executed in the browser** — not a server-side container sandbox. The
container design was the first plan and was dropped when Pyodide was weighed:
the isolation boundary, which is the whole feature, comes from the browser's WASM
sandbox instead of from container hardening we would own forever, and the
execution cost lands on the user's machine rather than a 3 GB host. Open WebUI's
code interpreter made the same call.

The mechanism is the one M4 already builds: the tool loop is server-side, and the
approval prompt already suspends it mid-stream to wait for the client. Code
execution is a second client-side continuation on that same suspension — the
model emits code, the browser runs it in Pyodide, the result posts back and the
loop resumes. No new control flow, one new executor.

What the decision costs, so it is not rediscovered as a bug: the package set is
what Pyodide ships compiled to WASM (numpy, pandas, matplotlib are there;
arbitrary pip installs are not — pure-Python wheels via micropip only); there is
no filesystem beyond the ephemeral virtual one; network from executed code is
the browser's CORS, which is to say effectively none — a correct default, stated
rather than implied; and execution dies with the tab, acceptable for a code
interpreter whose output feeds a chat turn. If a use case ever needs real
packages or long runs, that is the server-side sandbox from the original plan,
as an *addition* — Pyodide is not a stepping stone to it. Scoped like any tool:
admin-enabled, user-approved per call.

## Still Phase 3 or later, not in these milestones

The desktop app (a wrapper around the chat SPA once it exists) and the `opencode`
device flow (unblocked by M0; the remaining work is Keycloak client configuration
and documentation).
