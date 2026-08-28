# 0041 — The chat frontend is a Vite SPA on `packages/ui`, not Next.js

- Date: 2026-08-28
- Status: **accepted, built** (M1's foundation)
- Supersedes the framework and chat-library halves of
  [0015](0015-frontend-stack.md). What 0015 got right and this keeps: the
  frontend never holds provider credentials, and the server is authoritative for
  conversations.
- Related: [0034](0034-bauhaus-design-language.md) (the design language this
  reuses), [0023](0023-admin-console.md) (the SPA arrangement this copies),
  [phase-3-plan.md](../phase-3-plan.md).

## Context

ADR 0015 chose Next.js 16 with assistant-ui and the Vercel AI SDK, in August
2026, when none of the things it now has to fit existed: there was no
`packages/ui`, no console, no `chat-api`, and account management was still going
to live in the chat frontend.

**This ADR is written after the fact.** The Vite SPA was built without first
re-reading 0015, which is the mistake this document is partly here to stop being
silent. The reasoning below is real and was checked afterwards; the process was
wrong, and a reversal that nobody records is worse than one that is argued badly.

## Decision

`apps/web` is a Vite + React SPA, built to static files, served by `chat-api` at
`/chat`, styled with `packages/ui`. No Next.js, no assistant-ui, no AI SDK.

### Why not Next.js

Its value is server-side: RSC, SSR, route handlers, image optimisation. None of
it applies. The server here is FastAPI, the deliverable is a static bundle
behind a CSP with no `'unsafe-inline'`, and the two live on one origin because
that is what makes the session cookie work without CORS. Keeping Next.js means
either a Node process in the stack — a third runtime to build, ship and patch on
a 3 GB host — or `next export`, which discards the reason to have chosen it.

`apps/console` is already a Vite SPA. Two frameworks for two SPAs in one
monorepo is a cost paid on every dependency bump, and it buys nothing here.

### Why not assistant-ui

This is the part 0015 got most right and the loss is real, so it is stated
plainly: assistant-ui would have supplied markdown and code rendering, message
actions, attachments and a composer — and M1 has none of those. That is the bill
for this decision, not an argument that they are unnecessary.

Two reasons it still does not fit. It is built around the AI SDK's runtime and
expects to own transport, and our transport is an SSE relay from our own backend
carrying our own event names — so the part it does best is the part we do not
need. And it brings its own visual language, against a repository whose look is
[ADR 0034](0034-bauhaus-design-language.md): one token file, components that
reference tokens and never literals. A chat surface that did not look like the
console would be the most visible screen in the platform disagreeing with the
rest of it.

**Reconsider it for the message body specifically.** Markdown, syntax
highlighting and copy-a-code-block are genuinely solved problems and writing
them again is not a good use of anyone's time. Adopting a rendering library —
assistant-ui's or a smaller one — for the inside of a message, while the shell
stays ours, is the shape to look at, and it does not need this ADR reversed.

### Why not the Vercel AI SDK

It exists to talk to providers, or to a Next.js route handler. We do neither: the
browser talks to `chat-api`, which talks to the gateway. Its streaming
primitives would be wrapping forty lines of SSE parsing that already has tests
for the two things that actually break — an event split across a chunk boundary,
and CRLF from a proxy.

## Consequences

- One frontend toolchain, one design language, one CSP posture across both SPAs.
- **Still outstanding from 0015 and [0016](0016-pwa.md): the PWA.** "Installable,
  offline shell" was called mandatory and is not built. Vite has a maintained
  plugin (`vite-plugin-pwa`, MIT) so nothing about this decision blocks it — it
  is simply not done, and saying so here is better than letting a superseded ADR
  carry the only mention of it.
- Message rendering is plain text today. See the note above: that is a debt with
  a named shape, not a design.
- Types are still hand-written, not generated from `/openapi.json` as 0015
  wanted. `chat-api`'s surface is small enough that this has not hurt yet, and
  generation is worth doing when assistants and knowledge bases widen it.
