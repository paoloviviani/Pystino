# 0041 — A Vite SPA on `packages/ui`, with assistant-ui for the thread

- Date: 2026-08-28 (revised the same day, see below)
- Status: **accepted, built**
- Supersedes the framework half of [0015](0015-frontend-stack.md). What 0015 got
  right and this keeps: **assistant-ui**, the frontend never holding provider
  credentials, and the server being authoritative for conversations.
- Related: [0034](0034-bauhaus-design-language.md) (the design language),
  [0016](0016-pwa.md) (the PWA, now built), [0023](0023-admin-console.md) (the
  SPA arrangement this copies), [phase-3-plan.md](../phase-3-plan.md).

## Context

ADR 0015 chose Next.js 16 with assistant-ui and the Vercel AI SDK, in August
2026, before `packages/ui`, the console or `chat-api` existed and while account
management was still going to live in the chat frontend.

**This ADR was first written to reject all three, and was wrong about two of
them.** The first version argued that assistant-ui expects to own the transport
and brings its own visual language, and that the AI SDK came with it. Each claim
was checked afterwards against the published packages, and each is false:

| First claimed | Actually |
|---|---|
| Expects to own transport | `useExternalStoreRuntime` exists for exactly the opposite case. The adapter's only required member is `onNew`. |
| Brings its own visual language | `@assistant-ui/react` is headless — radix primitives. The styled layer is a separate package, not installed here. |
| Requires the Vercel AI SDK | `@assistant-ui/react-ai-sdk` is a separate package. The core does not depend on it. |

All MIT, all published 2026-08-27. The reasoning that produced the first version
was written from memory rather than from the packages, against ground rule 2,
and it reached three wrong conclusions in a row. That is recorded rather than
edited away, because the failure was the method, not the conclusion.

## Decision

`apps/web` is a **Vite + React SPA**, built to static files, served by
`chat-api` at `/chat`, styled with `packages/ui` — and it uses **assistant-ui**
for the thread.

### Not Next.js

This part of the first version stands. Next.js's value is server-side: RSC, SSR,
route handlers. The server here is FastAPI, the deliverable is a static bundle
behind a CSP with no `'unsafe-inline'`, and both live on one origin because that
is what makes the session cookie work without CORS. Keeping Next.js means a Node
process in the stack — a third runtime to build, ship and patch on a 3 GB host —
or `next export`, which discards the reason to have chosen it. `apps/console` is
already a Vite SPA, and two frameworks for two SPAs is a cost paid at every
dependency bump.

### assistant-ui, over an external store

`useExternalStoreRuntime` is what makes it fit: the messages, the transport and
the persistence stay ours, and assistant-ui renders them. It is a view handed our
own data, not an architecture adopted — the same relationship the gateway has
with provider plugins, and the same rule 0015 wrote about the AI SDK, applied one
layer up.

It supplies the three things hand-rolling would have been worst at, in order of
how badly:

- **Markdown with code blocks**, streaming-aware. Solved elsewhere, repeatedly.
- **Chain-of-thought**, collapsed. Reasoning arrives as its own message *part*,
  which is what lets it collapse — and, more to the point, what keeps it out of
  the answer. `content` and `reasoning` are separate columns server-side for the
  same reason: reasoning is never replayed to the model as history, and a scheme
  that had to parse it back out of the prose would replay it the first time the
  parsing was wrong.
- **Stop**, as a composer state rather than a separate control, wired to an
  `AbortController` and to `onCancel`.

**What it costs, stated rather than argued away.** The bundle goes from about
180 kB to 590 kB, 179 kB gzipped. And `assistant-cloud` — the client for
assistant-ui's paid managed-persistence service, the open-core relationship 0015
flagged — is a hard dependency of the core: bundled, never configured, never
reached. 0015's instruction holds and is worth repeating: our server is
authoritative for conversations, and if thread state ever starts living in the
client or in a third-party service, the desktop app's "sync is free" property
goes with it.

### Not HuggingFace chat-ui

Raised as an alternative and worth recording, because it is the strongest one and
the reasons against are structural rather than about quality. It is Apache-2.0 —
properly OSI, unlike Open WebUI — 10.9k stars, commits daily, and it powers
HuggingChat, so it has the production history this project does not.

But it is an **application**, not a library: SvelteKit, MongoDB, Tailwind 4,
89 dependencies, with its own provider configuration, its own OIDC and its own
admin — the layers the gateway exists to make redundant. Adopting it means a
third runtime, a fourth stateful service against
[0018](0018-vector-store.md)'s "one less stateful service", Svelte beside React
for `packages/ui` and the console, Tailwind against
[0034](0034-bauhaus-design-language.md)'s one token file, and a permanent fork to
re-merge. Every one of those is a decision this repository has already made
differently.

It remains the best **reference** for how configurable RAG should be, which is
what 0015 said of Open WebUI before the licence made that the only permitted use.

## Consequences

- One frontend toolchain, one design language, one CSP posture across both SPAs.
- **The PWA is built** ([0016](0016-pwa.md)): `vite-plugin-pwa`, scoped to
  `/chat/`, with the API and login paths kept off the offline shell — a cached
  `index.html` returned for `/chat/auth/callback` breaks sign-in in a way that
  survives a reload. Nothing about a conversation is precached: transcripts are
  the server's, and an offline copy of somebody's chat history in a browser cache
  is a question nobody has asked.
- Found building it, and silent in every way: `sw.js` has a dot and does not live
  under `assets/`, so the SPA fallback 404'd it. The application would have
  installed, opened, and had no offline shell, with nothing in any log to say so.
  Root-level files are served now — and never with an immutable cache, because
  `sw.js`'s name never changes and a year-long cache pins a browser to whichever
  worker it saw first, with the update that would fix it being the thing that is
  stuck.
- Types are still hand-written rather than generated from `/openapi.json` as 0015
  wanted. `chat-api`'s surface is small enough that it has not hurt; generation is
  worth doing when assistants and knowledge bases widen it.
