# web — the chat application

The chat UI. Built with Vite and React, styled with `packages/ui`, served as
static files by `chat-api` at **`/chat`**.

Not Next.js — [ADR 0015](../../docs/adr/0015-frontend-stack.md) chose it in
August 2026, before `packages/ui`, the console or `chat-api` existed. But
**assistant-ui, which 0015 also chose, is kept**: it renders the thread over
`useExternalStoreRuntime`, so the messages, the transport and the persistence
stay ours. [ADR 0041](../../docs/adr/0041-chat-frontend-stack.md) records both,
including the three claims the first draft of it got wrong.

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
`/v1/models` through `chat-api`, sign-in and sign-out. Markdown with code blocks,
a stop button, and the model's thinking in a collapsible section — all three from
assistant-ui.

**Installable**, with an offline shell, scoped to `/chat/`. The API and login
paths are deliberately excluded from it: a cached `index.html` returned for
`/chat/auth/callback` breaks sign-in in a way that survives a reload. Nothing
about a conversation is precached — transcripts belong to the server.

Reasoning is a separate field all the way down: its own SSE event, its own
column, its own message part. That is what keeps it out of the history sent back
to the model on the next turn.

## Not built yet

- Regenerate, edit-and-resend, and search over your own history. assistant-ui's
  adapter has `onReload` and `onEdit` waiting for the first two.
- Titles from a model rather than the first sixty characters.
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
