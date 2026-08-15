# 0022 — Administration is an API, and admin follows an IdP group

- Status: accepted
- Date: 2026-08-15

## Context

Phase 1 shipped with no way to administer anything over HTTP. Models, prices, group
access and quota rules could only be changed by `gateway seed`, the pricing importer,
or SQL. `/api/admin/limits` existed and was read-only.

Worse, it was unreachable in practice: `is_admin` was true only for the `local` seed
user, which has no OIDC identity and therefore cannot log in. Every user who *could*
authenticate had `is_admin = false`, so the one admin route could not be called by
anyone. The `platform-admins` group existed in the seeded realm and was wired to
nothing.

## Options considered

- **An HTML admin panel in the gateway.** Fastest to click around, and immediately a
  second frontend to maintain, style and migrate away from when `apps/web` arrives.
- **An admin API, with `/docs` as the console.** FastAPI already generates an
  interactive OpenAPI UI from the same schemas the endpoints validate against.
- **Keep using SQL.** Honest for a demo, indefensible for an operator who has to price
  a model at 2am.

## Decision

**An admin API under `/api/admin`, with Swagger at `/docs` as the operator console.
No HTML panel in the gateway.** Presentation belongs to `apps/web` in Phase 2; a
second UI here would be a thing to delete later.

Routes: models (list, create, patch), prices (list, append), group access (grant,
revoke), limit rules (list, create, patch, delete), users (list, patch), and usage
grouped by group.

**`is_admin` follows an identity-provider group**, configured by
`GATEWAY_OIDC__ADMIN_GROUPS`:

- Set, and the IdP is authoritative **both ways** — joining the group grants admin,
  leaving it removes admin at the next login. This mirrors the rule already applied to
  group membership in [0011](0011-oidc-integration.md): the directory is the source of
  truth, or revoking access there does not revoke it here.
- Unset, and login never touches the flag, so it stays a manual database decision.
  That is what keeps `gateway seed`'s local admin usable in a deployment with no IdP.
- When it *is* set, `PATCH /api/admin/users/{id}` **refuses** to change `is_admin` for
  an OIDC-backed user rather than accepting a change the next login would silently
  undo. Local accounts (`issuer = "local"`) can still be edited.

## Two invariants the endpoints enforce rather than trust

- **Prices are append-only.** There is no price edit — only a new effective-dated row.
  A future date schedules a change; a past one cannot rewrite what already-recorded
  requests cost. `POST /models/{id}/prices` is the only way to change a price, and
  `GET` returns the full history.
- **Models are deactivated, never deleted.** There is deliberately no `DELETE` route
  (it returns 405, and a test asserts that). Usage rows reference models with
  `ON DELETE SET NULL`, so deleting one would quietly turn historical spend into an
  unattributable row.

Other refusals worth noting: a price in the wrong currency is rejected with the same
reasoning as everywhere else — a silent exchange rate produces invoices that look
correct and are not. A limit rule naming a group or user that does not exist is
refused up front, rather than persisting happily and matching nothing.

## Consequences

- `/docs` is only useful once you are logged in, because the management API uses a
  session cookie. Sign in at `/auth/login` first; the cookie then travels with the
  Swagger requests.
- There is still **no self-service group management**, by design: the IdP owns
  membership ([0011](0011-oidc-integration.md)).
- Verified against the live stack: alice (not in `platform-admins`) gets 403; dave
  gets in on the strength of his group alone, creates and prices a model, grants it to
  a group and watches it appear on `/v1/models` for a member of that group, then
  revokes it and watches it disappear.
- The admin routes are covered by 32 tests. Those use a dependency override rather
  than a real cookie — the cookie path is covered end to end by
  `scripts/test_oidc_flow.py`, and repeating it here would test Keycloak rather than
  these routes.
- `routers/admin.py` used to hold the *self-service* `/api/me` endpoints, which was a
  misnomer. Those moved to `routers/me.py`; `admin.py` is now genuinely administrative.
