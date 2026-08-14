# 0002 — Monorepo layout and tooling

- Status: accepted
- Date: 2026-08-14

## Context

One repository holds three deployables and several supporting services, in two
languages. Python and JavaScript cannot share a package manager, so the question is
what each half uses and whether anything sits above them.

## Options considered

- **uv workspaces + pnpm workspaces, nothing above them.** Two native workspace
  mechanisms, each idiomatic for its language.
- **Add Turborepo or Nx** on top for a task graph and remote caching. Turborepo is
  MIT (Vercel); Nx is open-core.
- **Bazel / Pants.** Correct at large scale, and a significant tax before then.

## Decision

**uv workspaces for Python, pnpm workspaces for JavaScript, and no orchestrator
above them.**

- `uv` — verified actively released (a release dated 2026-08-13), Cargo-style
  workspaces are exactly the primitive needed. Members: `apps/gateway`,
  `packages/shared-py`. The root `pyproject.toml` is a *virtual* root: it is not an
  installable package, it only ties members together and holds shared tool config.
- `pnpm` workspaces for `apps/web`, `apps/desktop`, `packages/shared`.
- **No Nx or Turborepo.** A build-graph layer earns its keep when task fan-out and
  cache misses are the bottleneck. With two applications, it would add a
  configuration surface and a caching failure mode in exchange for nothing. Nx is
  also open-core, which per [0001](0001-licensing.md) needs approval we do not need
  to seek. Revisit if CI time becomes a real complaint.
- `ruff` for linting and formatting, `mypy --strict`, `pytest`. Pre-commit config
  provided; **`prek`** (Rust reimplementation of pre-commit, same config file) is a
  faster drop-in if the Python-based runner annoys.

## Layout

```
apps/gateway/        FastAPI service              (Phase 1 — built)
apps/web/            Next.js frontend             (Phase 2 — placeholder)
apps/desktop/        Tauri shell                  (Phase 4 — placeholder)
services/rag/        indexing + retrieval         (Phase 3 — placeholder)
services/redaction/  Presidio detection service   (Phase 2 — placeholder)
packages/shared/     shared TS types              (Phase 2 — placeholder)
packages/shared-py/  shared Python contracts      (Phase 1 — built)
scripts/             pricing importer, opencode bootstrap
deploy/compose/      docker compose
docs/adr/
```

Two deviations from the brief's suggested layout:

1. **`packages/shared-py` added.** The brief's single `packages/shared` implies one
   shared package, but the gateway (Python) and the web app (TypeScript) cannot
   share one. `shared-py` holds contracts two *Python* processes must agree on
   byte-for-byte — above all the deterministic placeholder derivation, which the
   gateway and the Phase 2 detection sidecar each compute independently. If that
   lived in `apps/gateway`, the sidecar would have to duplicate it, and a
   duplicated HMAC is a divergence waiting to happen.
2. **`deploy/compose/` subdirectory** with base + override files, so the dev
   conveniences (bind mounts, `--reload`, exposed database ports) are not the same
   file as the production topology.

## Consequences

- TypeScript API types are **generated from the gateway's `/openapi.json`**, never
  hand-written. Pydantic models are the single source of truth and TS is downstream.
- `uv.lock` is committed, and the Docker build uses `uv sync --frozen`, so an image
  cannot silently resolve different dependency versions from CI.
- No cross-language task runner means `make`-style convenience commands live in each
  app's README rather than a shared task graph. Accepted: two apps.
