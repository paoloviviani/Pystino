# 0045 — Public models: access for every authenticated caller

- Date: 2026-08-30
- Status: **accepted, built**
- Extends [0027](0027-inference-providers.md) (whose access rule this widens by
  one clause) and [0010](0010-api-keys.md) (whose billing invariants it does
  not change).
- Related: [docs/oidc-generic-provider.md](../oidc-generic-provider.md) — a
  deployment with no identity provider reaches the same place with local
  accounts and this flag.

## Context

Until now a model was reachable only through an explicit grant — to a group or
to a person — and absence of a grant meant no access. That is the right default
for a platform billing other people's money, and it stays the default.

But this platform is also run by a person for a household or a team, where the
operator *wants* everything authenticated to reach the catalogue without
maintaining a grants table for an audience of three. The choice was SQL against
the ledger's own database or a feature; the feature is cheaper to audit.

## Decision

`models.is_public` (migration 0016, default **false**). One clause added to
`access.py`'s union:

    a caller may use a model if their billing group has been granted it, or
    they have been granted it personally, **or the model is public**

### Access is granted; billing is not changed

The invariant that makes this safe: a public model is not a free model. A
request still needs a caller with a **default billing group** — a groupless
user listing the model can see it and is refused at the billing step with the
same 403 they would get for any model. Spend lands on the caller's own group,
counts against that group's quotas, and appears in the ledger attributed
exactly as any other request. Nothing about *who pays* changes; only *who may
ask*.

### A flag, not a synthetic group

A "public" group that everyone is a member of was considered and rejected:

- it would appear on every user's membership and in the console's group list
  as a member-bearing row it is not;
- "which models can everyone use" becomes a join over grant rows instead of a
  column read;
- groups are created by the identity provider (ADR 0011) and reconciled
  authoritatively at login — a synthetic one would need guarding against
  deletion, renaming, and re-provisioning wipes.

The flag is one column, one toggle, and one predicate.

### Defaults closed, listed like everything else

`false` is the default: widening access is a decision, never a default. Public
models appear in `GET /v1/models` for every authenticated caller — the listing
is the request surface, and hiding a usable model from its listing would make
the API lie. Unauthenticated callers still see nothing; the flag grants
access to *authenticated* users, never to the internet.

## What was rejected

- **Anonymous access.** Even a "truly public" model needs an identity to bill
  and a rate to limit by. The gateway's floor is authentication, and this
  feature does not dig under it.
- **Per-model price overrides for public access** — the existing price row
  already answers "what does a request cost"; who is allowed to make one is a
  different question.

## Consequences

- The console's model page gains a Visibility row with a one-click toggle; the
  models listing badges a public model. The admin API takes `is_public` on
  create and update.
- Quotas work unchanged: a public model's spend counts against each caller's
  own group, so one heavy user cannot exhaust a public model for everybody.
- `unpriced_model_count` on the provider listing matters more now: a public
  model with no price serves everyone at zero. The console already warns.
- Revoking public access is the same toggle; the next listing excludes the
  model and the next request 404s, as with any inaccessible model.
