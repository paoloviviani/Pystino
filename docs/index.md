# Pystino

A self-hosted OpenAI-compatible **gateway** with per-user and per-group
accounting, quotas, redaction and OIDC sign-in, plus the **console** that
operates it. The name is *pistino*: Turin dialect for a nitpicker, the person
who checks every last detail.

One origin: the gateway serves the `/v1` API, the management API and the
console at `/console`. [Cerea](https://github.com/paoloviviani/Cerea), the chat,
is a `/v1` client of this gateway and a separate repository.

## The four ideas that carry the design

1. **PostgreSQL is the ledger of record; Valkey is a rebuildable cache.** Quotas
   still evaluate correctly with Valkey gone, just more slowly. Nothing about
   money is stored only in a cache.
2. **Quota check before the upstream call, accounting after, a reservation in
   between.** Without the reservation, concurrent requests each read the same
   under-limit total and collectively blow the budget.
3. **Accounting never silently reports zero.** Usage is forced out of the
   upstream, and if it never arrives the tokens are counted locally and the row
   is stamped `estimated`.
4. **A plugin returns facts and never computes money.** Vendor quirks live in
   `gateway/plugins/`; the arithmetic stays in `accounting/cost.py`, the only
   code that multiplies a count by a rate.

## Where to go next

- [Configure opencode with Pystino](coding-agents.md#point-opencode-at-the-gateway-with-a-script):
  one command points the opencode coding agent at your gateway.
- [Getting started](getting-started.md): run it, add a model, make a first
  billed request, sign in to the console.
- [Gateway](gateway.md): every surface, the two authentication schemes, and
  the streaming traps.
- [Accounting and quotas](accounting-and-quotas.md): the three cost figures,
  the two prompt conventions, and what money looks like end to end.
- [Redaction](redaction.md): the engine, the policy model, and why a scope can
  only tighten.
- [Deployment](deployment.md): the Pystino-only compose deployment, TLS,
  identity, upgrades and backups.
- [Identity](identity.md): one provider from `.env`, who is an administrator,
  linking, merging, and break-glass. [Bundled accounts](bundled-accounts.md)
  is the Users page; [the OIDC provider](oidc-generic-provider.md) is
  registering your own issuer.
- [Coding agents](coding-agents.md): what the gateway provides to agents on
  people's own machines.
- [The console](console.md): a map of the admin screens, and which page
  explains each.
- [Operations](operations.md) and [Measured performance](performance.md):
  verifying a change, the live checks, and what the gateway costs.

## The rest of the stack

Pystino is one of three repositories. **[Cerea](https://github.com/paoloviviani/Cerea)**
is the chat, with its own [documentation](https://paoloviviani.github.io/Cerea/)
(using it, the agent machines, configuration). **cerea-deploy** is the
deployment that runs both, [with its README](https://github.com/paoloviviani/cerea-deploy#readme)
as the runbook.

## Versions

This site follows `main` and is not versioned per release. What changed between releases, and which Pystino and Cerea
versions a given release of the stack pins together, is recorded in
[cerea-deploy's CHANGELOG](https://github.com/paoloviviani/cerea-deploy/blob/main/CHANGELOG.md).

## Licence

[Apache-2.0](https://github.com/paoloviviani/Pystino/blob/main/LICENSE) for all
first-party code. Dependencies must be OSI-licensed, without a CLA or an
open-core model.
