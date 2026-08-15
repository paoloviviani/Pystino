# 0023 — The console: a SPA served by the gateway, optional at build time

- Status: accepted
- Date: 2026-08-15
- **Supersedes the UI stance of [0022](0022-administration-surface.md)**, whose API
  design decisions all still hold.

## Context

[0022](0022-administration-surface.md) argued against a UI in the gateway on the
grounds that presentation belonged to `apps/web`, arriving in Phase 2. That premise
no longer holds: the chat frontend has moved to Phase 3, and operators need billing,
pricing, quotas and usage reporting before then. `/docs` is a fine console for an
engineer and not one for anyone else.

Two requirements pull in opposite directions:

- the experience should stay **coherent** with the eventual chat frontend;
- the gateway must be **deployable without** that frontend.

A third requirement changes the shape of the thing: **every authenticated user sees
their own spend**, not just administrators. So this is not an admin panel with a
login — it is a console whose administrative section is gated.

## Options considered

| | Coherent with chat | Gateway alone | Cost |
|---|---|---|---|
| **SPA built into the gateway image** | via a shared component package | one deployable | Node stage in the gateway build |
| Separate `apps/admin` | same | two deployables | CORS or BFF auth bridging |
| Jinja2 + HTMX in the gateway | no — different idiom, drifts | cheapest, no Node | design language diverges |
| One Next.js app, admin flagged | literally the same app | no — admin tied to chat releases | a bad chat deploy takes out billing |

The last was rejected specifically: coupling the availability of the billing console
to the chat application's release cadence is tidy right up until you cannot check
spend because someone shipped a chat regression.

## Decision

**A React SPA, built at image-build time and served by the gateway**, sharing a
`packages/ui` component library with the Phase 3 chat app.

- **Same origin**, so the existing OIDC session cookie authenticates it directly. No
  CORS, no BFF, no second auth path to get wrong.
- **Optional at build time.** `ARG INCLUDE_CONSOLE` controls whether the Node stage
  runs and the assets are copied, producing two images from one Dockerfile:
  `gateway` (headless) and `gateway-console`. The HTTP API is byte-for-byte identical
  between them; the console is additive.
- **Gated at runtime too.** The static route mounts only if the assets are present
  *and* `GATEWAY_CONSOLE_ENABLED` allows it. Belt and braces: an image built without
  assets cannot half-serve a console, and an operator can turn it off without
  rebuilding.
- **Multi-role.** `/` is any authenticated user: their own spend, their groups'
  spend, their API keys, their default billing group. `/admin/*` requires `is_admin`,
  which already follows an identity-provider group ([0022](0022-administration-surface.md)).

## Consequences

- **The console becomes the account-management surface for the whole platform.** The
  Phase 3 chat app should *link* to it for key minting and spend, not reimplement
  them. Two implementations of "mint an API key" is one too many, and the second one
  is where the security bug will be.
- The gateway now serves HTML, which it did not before. That brings a Content
  Security Policy, static asset caching headers, and a SPA fallback route into scope
  — none hard, all easy to forget.
- `packages/ui` is designed before its second consumer exists. That is a real risk of
  designing in a vacuum; the mitigation is to keep it to tokens and primitives
  (colour, spacing, type, buttons, tables, forms) and resist inventing chat-shaped
  components for an app that does not exist yet.
- The gateway image gains a Node build stage, so builds get slower. The headless
  image skips it entirely, which is part of why the flag is worth having.
- A Python-maintained repository now contains a frontend toolchain earlier than
  planned. Accepted deliberately: the alternative (Jinja2 + HTMX) is cheaper to
  maintain but guarantees the console and the chat app drift apart visually, and
  coherence was an explicit requirement.

## What did not change

Everything in [0022](0022-administration-surface.md) about the API still stands, and
the console is only a client of it: prices remain append-only, models deactivate
rather than delete, admin follows an IdP group, and `/docs` remains available for
anything the console does not surface.
