# web — the chat application

The chat UI. Built with Vite and React, styled with `packages/ui`, served as
static files by `chat-api` at **`/chat`**.

Not Next.js, and not assistant-ui, though
[ADR 0015](../../docs/adr/0015-frontend-stack.md) chose both in August 2026 —
before `packages/ui`, the console or `chat-api` existed.
[ADR 0041](../../docs/adr/0041-chat-frontend-stack.md) records the reversal, what
it costs (message rendering is plain text) and what remains worth reconsidering.

## What it talks to

Only `chat-api`, on the same origin, with a session cookie. There is no token in
JavaScript and nothing in `localStorage`: the access token that reaches the
gateway is minted server-side, per request, from a refresh token the browser
never sees.

Everything lives under `/chat`, including the API calls (`/chat/api/…`) and the
login route (`/chat/auth/login`). Behind the TLS proxy the gateway owns the root
of the origin, so `/api` and `/auth` there belong to *it*. The Vite `base` must
match, or the built page loads and then fetches every script from the gateway.

## What is deliberately not here

Providers, models, quotas, spend and API keys. Those are the console's, and the
identity menu links to it. Two implementations of "mint an API key" is one too
many, and the second is where the security bug will be.

## Built

Conversation list, one conversation, a streamed turn, a model picker fed by
`/v1/models` through `chat-api`, sign-in and sign-out.

## Not built yet

- Markdown and code rendering in a message — see ADR 0041 for the shape.
- Regenerate, edit-and-resend, stop, and search over your own history.
- Titles from a model rather than the first sixty characters.
- **The PWA.** [ADR 0016](../../docs/adr/0016-pwa.md) calls it mandatory and it
  is not done; `vite-plugin-pwa` (MIT) is the route.
- Assistants, knowledge bases, MCP — M2 onwards in
  [docs/phase-3-plan.md](../../docs/phase-3-plan.md).

## Working on it

```bash
pnpm --filter @llmp/web dev        # :5174, proxying /chat/api to :8100
pnpm --filter @llmp/web test
pnpm --filter @llmp/web typecheck
```

**Do not copy code from Open WebUI.** Since v0.6.6 it carries a branding clause
and a CLA and is not OSI open source. It is a reference for how configurable RAG
should be, and that is all.
