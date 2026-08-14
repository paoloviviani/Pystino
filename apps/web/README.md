# Web frontend — placeholder

**Nothing is built here yet.** Phase 2. This directory exists so the monorepo
layout is settled and so the decisions already researched are not researched
again.

## What goes here

A chat UI that talks to the gateway, and the only place a human manages their own
account:

- OIDC login using the same group model as the gateway
- shows the user their own spend (`GET /api/me/usage`)
- lets them change their default billing group (`PUT /api/me/default-billing-group`)
- lets them mint and revoke API keys (`/api/me/keys`)
- **PWA is mandatory** — installable, offline shell
- admin-configurable web search (Exa, Jina, Staan)
- MCP connections with OIDC
- RAG configuration surface: search mode, OCR endpoint, embedding endpoint
- code execution sandbox, file upload, artifacts

## Decisions already taken

| Choice | Decision | ADR |
|---|---|---|
| Framework | Next.js 16, App Router (default in 16, Turbopack stable) | [0015](../../docs/adr/0015-frontend-stack.md) |
| Chat UI | assistant-ui (MIT) rather than hand-rolled | [0015](../../docs/adr/0015-frontend-stack.md) |
| Streaming/transport | Vercel AI SDK v6 (Apache-2.0) as a library, not an architecture | [0015](../../docs/adr/0015-frontend-stack.md) |
| PWA | Serwist (MIT). `next-pwa` is archived. | [0016](../../docs/adr/0016-pwa.md) |
| Types | Generated from the gateway's OpenAPI schema, never hand-written | [0002](../../docs/adr/0002-monorepo-tooling.md) |

## Constraints worth knowing before starting

- **The server is authoritative for conversations.** There is no client-side
  conversation store to reconcile; the desktop app wraps this same frontend and
  gets sync for free precisely because of that.
- **Do not copy code from Open WebUI.** It is a useful reference for how
  configurable RAG should be, and that is all it may be used for: since v0.6.6 it
  carries a branding clause and a CLA and is not OSI open source. See
  [0019](../../docs/adr/0019-document-conversion.md).
- Talk to the gateway's `/v1` surface with a user-scoped API key or a session,
  never with the upstream provider's key.
