# 0015 — Frontend stack: Next.js 16, assistant-ui, AI SDK v6

- Status: **partly superseded** by [0041](0041-chat-frontend-stack.md): Next.js
  and the AI SDK were dropped, **assistant-ui was kept**. What still stands
  unchanged: the frontend never holds provider credentials, the server is
  authoritative for conversations, and Assistant Cloud is never configured.
- Date: 2026-08-14

## Context

A chat UI with OIDC, spend visibility, key management, and a mandatory PWA. The
assumption was Next.js App Router plus a chat UI library rather than one written from
scratch, with assistant-ui and the Vercel AI SDK to be evaluated.

## Findings (verified 2026-08-14)

| Project | Licence | State |
|---|---|---|
| Next.js | MIT | 16.3.1 current. App Router is the **default** in 16; Turbopack stable; React 19.2; up to 90% less dev memory. |
| assistant-ui | **MIT** | 11.7k stars, commits as recent as 2026-08-12. Actively maintained. |
| Vercel AI SDK | **Apache-2.0** (verified at the LICENSE file, © Vercel, Inc.) | v6 current; adds the v3 model spec, agents, tool approval; migration from v5 described as low-breakage. |

## Decision

- **Next.js 16, App Router.** Validated — the assumption was correct and the App Router
  is now the default rather than an opt-in.
- **assistant-ui** for the chat surface, rather than hand-rolling one. MIT.
- **Vercel AI SDK v6** as a *library* for streaming and transport primitives — not as an
  architectural centre. The gateway owns the model interaction; the SDK should not be
  allowed to become a second place where provider behaviour is decided.
- API types **generated** from the gateway's `/openapi.json`.

## The one flag worth remembering

assistant-ui has an optional commercial **"Assistant Cloud"** for managed thread
persistence and analytics. Per [0001](0001-licensing.md) that is an open-core
relationship worth noting — but it is irrelevant here, and pleasingly so: what Assistant
Cloud sells is exactly what our architecture already has. **Our server is authoritative
for conversations.** The MIT core is self-sufficient.

Keep it that way. If thread state ever starts living in the client or in a third-party
service, the desktop app's "sync is free because the server is authoritative" property
disappears with it.

## Consequences

- The frontend never holds provider credentials. It talks to the gateway only.
- Spend display, billing-group switching and key minting all exist as gateway endpoints
  already (`/api/me`, `/api/me/keys`, `/api/me/usage`), so the frontend is a client of a
  working API rather than a co-design.
- Open WebUI is a **reference for RAG configurability scope only**. Since v0.6.6 (April
  2025) it carries a branding clause and a CLA and is not OSI open source; no code may be
  copied. See [0019](0019-document-conversion.md).
- Revisit if assistant-ui's maintenance changes. The chat surface is the most replaceable
  part of the system; the gateway is not.
