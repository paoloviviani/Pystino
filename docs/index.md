# Pystino

A self-hosted OpenAI-compatible **gateway** with per-user and per-group
accounting, quotas and policy, plus the **console** that operates it. The name
is *pistino* — Turin dialect for a nitpicker, the person who checks every last
detail.

One origin, one port: the gateway serves the `/v1` API surfaces, the management
API and the console at `/console`. Everything is published on loopback unless
deliberately put behind the TLS proxy
([ADR 0035](https://gitlab.linksfoundation.com/viviani/ai-stack/-/blob/main/docs/adr/0035-public-tls-exposure.md)).

## Status

| Component | State |
|---|---|
| `apps/gateway` | **Built and tested.** Five `/v1` surfaces (chat completions, responses, Anthropic messages, embeddings, image generation, streaming and not), models, API keys, OIDC, accounting, quotas, redaction. |
| `services/redaction` | **Built and tested.** Presidio behind a swappable detection contract; PII never reaches the upstream. |
| `apps/console` | **Built.** Spend, reports, quotas, providers, models with prices and access, users, redaction rules. |
| `packages/ui` | **Built.** Material Design 3 tokens and primitives ([ADR 0042](https://gitlab.linksfoundation.com/viviani/ai-stack/-/blob/main/docs/adr/0042-material-design-3.md)), shared by console and chat. |
| the chat application | **Elsewhere.** [pystino-chat](https://gitlab.linksfoundation.com/viviani/pystino-chat) is a `/v1` client of this gateway and imports nothing from it. |
| `packages/shared-py` | **Built.** The detection contract and the deterministic placeholder scheme. |
| the desktop shell, the RAG pipeline | Not started, and not this repository's. Their scope is recorded in [ai-stack](https://gitlab.linksfoundation.com/viviani/ai-stack). |

## The four ideas that carry the design

1. **PostgreSQL is the ledger of record; Valkey is a rebuildable cache.** Quotas
   still evaluate correctly with Valkey gone, just more slowly. Nothing about
   money is stored only in a cache. ([ADR 0006](https://gitlab.linksfoundation.com/viviani/ai-stack/-/blob/main/docs/adr/0006-counter-store.md))
2. **Quota check before the upstream call, accounting after, a reservation in
   between.** Without the reservation, concurrent requests each read the same
   under-limit total and collectively blow the budget. ([ADR 0009](https://gitlab.linksfoundation.com/viviani/ai-stack/-/blob/main/docs/adr/0009-quota-model.md))
3. **Accounting never silently reports zero.** Usage is forced out of the
   upstream, and if it never arrives the tokens are counted locally and the row
   is stamped `estimated`. ([ADR 0008](https://gitlab.linksfoundation.com/viviani/ai-stack/-/blob/main/docs/adr/0008-accounting-model.md))
4. **A plugin returns facts and never computes money.** Vendor quirks live in
   `gateway/plugins/`; the arithmetic stays in `accounting/cost.py`, the only
   code that multiplies a count by a rate. ([ADR 0032](https://gitlab.linksfoundation.com/viviani/ai-stack/-/blob/main/docs/adr/0032-provider-plugins.md))

## Where to go next

- [Getting started](getting-started.md) — run the stack, seed a model, make a
  first billed request, sign in to the console.
- [Architecture](https://gitlab.linksfoundation.com/viviani/ai-stack/-/blob/main/docs/architecture.md) — the topology, the components and the
  boundaries that are deliberate.
- [Gateway](gateway.md) — every surface, the two authentication schemes, and
  the streaming traps with the file that handles each.
- [Accounting and quotas](accounting-and-quotas.md) — the three cost figures,
  the two prompt conventions, and what money looks like end to end.
- [Redaction](redaction.md) — the engine, the policy model, and why a scope can
  only tighten.
- [Deployment](deployment.md) — compose overlays, loopback-only by default, and
  what it takes to serve a real address.
- [Operations](operations.md) — verifying a change, the live checks, measured
  performance, and the known sharp edges.
- [Design decisions](https://gitlab.linksfoundation.com/viviani/ai-stack/-/blob/main/docs/adr/README.md) — 45 ADRs with the licence, version and CVE
  evidence behind each, dated. Start here before changing anything.

## Licence

[EUPL-1.2](https://github.com/paoloviviani/Pistin-Gateway/blob/main/LICENCE) for all
first-party code. This is a hard requirement, not a preference — every inbound
dependency must be OSI-licensed, without a CLA and without an open-core model
([ADR 0001](https://gitlab.linksfoundation.com/viviani/ai-stack/-/blob/main/docs/adr/0001-licensing.md)).
