# AGENTS.md

Pystino is an OpenAI-compatible gateway with accounting, quotas and
redaction, and the admin console for it. The chat application, Cerea, is its
own repository and a `/v1` client of this one. Licence: **EUPL-1.2** for all
first-party code.

`CLAUDE.md` is the deep-context file: ground rules, the layout, how to verify
a change, and the traps that only show up on a running stack. Read it before
changing anything. This file is the fast layer.

## Ground rules

- **Licence is a hard requirement.** Ask before adopting any dependency with a
  non-OSI licence, a CLA, or an open-core model.
- **Never guess a library or version from memory**; check at source.
- **Accounting and quota logic get tests specifically.**
- **Never run `ruff format` across the tree.** The pre-commit hook formats the
  files you stage; that is the only formatting that should happen.
- **Nothing but the proxy is published**: the gateway binds `127.0.0.1`.

## Layout: two workspaces

- Python: a `uv` workspace, `apps/gateway` and `packages/shared-py`.
  `services/redaction` is deliberately **not** a member (spaCy must never enter
  the gateway's lockfile), but its tests still run from the repository root.
- JS/TS: a `pnpm` workspace, `packages/ui` (design tokens and primitives) and
  `apps/console`.
- Alembic migrations: `apps/gateway/migrations/versions/NNNN_*.py`, excluded
  from ruff and mypy. Against a real database:
  `uv run alembic -c apps/gateway/alembic.ini upgrade head`.
- The gateway accepts a caller-supplied `x-request-id` and never returns the
  one it uses: a caller who wants a transcript tied to cost mints it and sends
  it.

## Verify a change

```bash
uv run ruff check . && uv run mypy apps/gateway/src services   # mypy is --strict
uv run pytest -q                                               # SQLite, a fake upstream, no services
pnpm -r test && pnpm -r typecheck                              # packages/ui and apps/console
uv run mkdocs build --strict                                   # the docs site
```

Tests need no PostgreSQL, Valkey or network. Run pytest from the repository
root; a single file is `uv run pytest apps/gateway/tests/test_cost.py -q`. The
mypy pre-commit hook covers `apps/gateway/src` and `packages/shared-py/src`
only, so run mypy on `services` by hand. For changes touching the request
path, money or SQL, run the live scripts against a real stack (see
`CLAUDE.md`).
