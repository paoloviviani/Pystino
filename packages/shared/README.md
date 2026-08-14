# shared (TypeScript) — placeholder

**Nothing is built here yet.** Phase 2, alongside `apps/web`.

## What goes here

TypeScript types shared between `apps/web` and `apps/desktop`.

The important rule: **API types are generated from the gateway's OpenAPI schema,
not written by hand.** The gateway serves it at `/openapi.json`. Hand-written
mirrors of a server's types drift silently, and the first symptom is a runtime
error in production rather than a compile error in CI.

Pydantic models in `apps/gateway` are the single source of truth. This package is
downstream of them.

## Why the Python half is separate

`packages/shared-py` exists for contracts that two *Python* processes must agree
on byte-for-byte — above all the deterministic placeholder derivation, which the
gateway and the Phase 2 detection sidecar each compute independently. A Python
package and a TypeScript package cannot be the same package, so the layout has
both rather than pretending one `packages/shared` could serve both languages.
