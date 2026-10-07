# Contributing and development

Thanks for helping. A short conversation first saves work on both sides.

- **Bugs and ideas:** open an issue. For a bug, include what you sent, what you
  expected, what happened, and the gateway version.
- **Changes:** open a pull request against `main`, focused on one thing, saying
  what problem it solves and how you checked it.
- **Security issues** go to [SECURITY.md](https://github.com/paoloviviani/Pystino/blob/main/SECURITY.md),
  not to a public issue.

By contributing you agree that your contribution is licensed under the
Apache License 2.0, the licence of this repository. First-party code is
Apache-2.0; a dependency must be under an OSI-approved licence, with no CLA and
no open-core model. Ask before adopting anything that is not.

Two rules matter more than any others here:

- **Money is never a float.** Amounts are `Decimal` in Python and strings on the
  wire; `accounting/cost.py` is the only code that multiplies a count by a rate.
- **Accounting, quotas and redaction need tests.** A wrong answer there is a
  wrong invoice or a leak, not a stack trace.

## Setup

| Tool | Version | Used for |
|---|---|---|
| Python | 3.13 or newer (`requires-python`, ruff's and mypy's target) | the gateway, the shared package, the redaction service |
| [uv](https://docs.astral.sh/uv/) | recent (the image builds with 0.12) | Python environments, the lockfile, running every Python tool |
| Node | 24 or newer (`engines` in `package.json`) | the console and the shared UI package |
| pnpm | 11 (`packageManager` in `package.json`; enable it with `corepack enable`) | the JavaScript workspace |
| Docker with Compose | recent | the compose deployment, the live checks, the stack workflow |

The repository is two workspaces. Python is a `uv` workspace of `apps/gateway`
and `packages/shared-py`; `services/redaction` is deliberately **not** a member
(spaCy must never enter the gateway's lockfile), but its tests run from the
repository root. JavaScript is a `pnpm` workspace of `packages/ui` and
`apps/console`.

```sh
uv sync                  # the Python workspace and the dev tools, into .venv
pnpm install             # the console and the UI package, into node_modules
```

CI installs with `uv sync --locked` and `pnpm install --frozen-lockfile`, which
fail instead of updating a lockfile. Use them when you want what CI will see.

| Path | What it holds |
|---|---|
| `apps/gateway` | the gateway: `/v1` surfaces, `/api` management, console hosting, Alembic migrations |
| `apps/console` | the React admin SPA, built into the gateway image and served at `/console` |
| `packages/ui` | design tokens and primitives (see "The shared tokens" below) |
| `packages/shared-py` | the detection contract and the deterministic placeholder scheme |
| `services/redaction` | Presidio behind that contract, out of process |
| `deploy/` | the Pystino-only compose deployment: `compose.yaml`, `caddy/`, `authelia/`, `.env.example`, `pin.py`, `release.env` |
| `scripts/` | the live checks against a running stack, and the fake upstream |
| `docs/` | this documentation site |

### Running it locally

The quickest complete run is meant to need no services. `./scripts/smoke_test.sh`
starts a fake upstream and the gateway on ports 9099 and 8099 with a temporary
SQLite database, drives the endpoints, prints the ledger and cleans up. It needs
`uv sync` first.

For a gateway you can click on, against a real PostgreSQL:

```sh
export GATEWAY_DATABASE_URL=postgresql+asyncpg://gateway:gateway@localhost:5432/gateway
export GATEWAY_SESSION_SECRET=$(openssl rand -hex 32)
export GATEWAY_UPSTREAM__BASE_URL=http://127.0.0.1:9099/v1 GATEWAY_UPSTREAM__API_KEY=anything
uv run alembic -c apps/gateway/alembic.ini upgrade head
uv run gateway seed                    # a demo group, model, user and API key (printed once)
uv run gateway serve --reload          # http://localhost:8000
```

`GATEWAY_VALKEY_URL` defaults to a local Valkey; set it to an empty string for
the in-memory counters the tests use. Every setting is in
`apps/gateway/src/gateway/config.py` (`GATEWAY_*`, nested with `__`).
`GATEWAY_ENVIRONMENT` defaults to `dev`; the compose deployment runs
`production`, where a missing `GATEWAY_SESSION_SECRET` or `GATEWAY_SECRET_KEY`
stops the gateway at startup and says which.
The console's sign-in is OIDC, so a working console needs an identity provider:
bring up the compose deployment instead (below), or develop against
`deploy/dev/keycloak/`.

The console in development:

```sh
pnpm dev                                # vite on :5173, proxying /api, /auth and /v1 to a gateway on :8000
```

The compose deployment from your checkout, with a fake upstream so no provider
account is needed (`getting-started` has the `.env` values for a trial origin):

```sh
cp deploy/.env.example deploy/.env && chmod 600 deploy/.env     # fill it in: every secret names its command
export PYSTINO_SRC="$PWD"
cd deploy && docker compose -f compose.yaml -f dev/smoke.yml up -d --wait
```

The gateway and redaction images that `compose.yaml` pulls are the published
ones; to run your own code, build the image (`docker build -f apps/gateway/Dockerfile
--build-arg INCLUDE_CONSOLE=true -t local/pystino-gateway:dev .`) and set
`PYSTINO_REGISTRY=local`, `PYSTINO_VERSION=dev` in `deploy/.env`. From a git
worktree, `deploy/.env` does not exist (it is git-ignored): symlink it. Without
a Node toolchain on the host, run `pnpm` in a `node:24-bookworm-slim` container
with `CI=true` and `corepack enable`, and never `pnpm config set --location project`:
it writes the container's store path into `pnpm-workspace.yaml`, a committed file.

Database changes are Alembic migrations in `apps/gateway/migrations/versions/`:
`uv run alembic -c apps/gateway/alembic.ini revision -m "…"` adds one and
`… upgrade head` applies it. The compose `migrate` service runs them on every
`up`, and they only go forward.

## Git hooks

`.pre-commit-config.yaml` defines the commit hooks. They are not installed by
cloning: install them once per clone. `pre-commit` is not in the project's
dependency group (so `uv run pre-commit` does not work); run it through uv's
tool runner, or use [prek](https://github.com/j178/prek), a drop-in
reimplementation that reads the same file:

```sh
uvx pre-commit install       # or: prek install
uvx pre-commit run --all-files
```

What runs on a commit, on the staged files:

| Hook | Files | What it does |
|---|---|---|
| `trailing-whitespace`, `end-of-file-fixer` | all | fixes them |
| `check-yaml`, `check-toml` | YAML, TOML | parses them |
| `check-added-large-files`, `check-merge-conflict`, `detect-private-key` | all | refuses the commit |
| `ruff check --fix` | Python | lint, applying the safe fixes |
| `ruff format` | Python | **reformats the file** |
| `mypy` | `apps/gateway/src/` and `packages/shared-py/src/` | `--strict` type check, in an environment of its own |

The two ruff hooks run `uv run ruff`, so they use the version in `uv.lock`, the
same one CI uses. The mypy hook still pins its own (1.13.0) in an environment of
its own; CI's `uv run mypy` is the one that counts.

There are no JavaScript hooks: Prettier and ESLint are not configured in this
repository, and nothing lints or formats the console's TypeScript beyond `tsc`.
The root `package.json` has no `lint` script, on purpose: with no linter
configured, a `pnpm -r lint` had nothing to run and failed with
`ERR_PNPM_RECURSIVE_RUN_NO_SCRIPT`. Add the script back with the linter, not
before.

## Formatting and linting

CI **checks, never rewrites**:

```sh
uv run ruff check .                                   # E, W, F, I, B, UP, C4, SIM, RUF, ASYNC, S; line length 100
uv run ruff format --check .                          # the layout ruff format would produce
uv run mypy apps/gateway/src packages/shared-py/src services   # strict, with the pydantic plugin
```

All three are clean on `main`. `pyproject.toml` has the rule set and the
per-file ignores; Alembic migrations are excluded from both tools. mypy needs
the workspace installed, so run it through `uv run`. CI and the commit hook
agree on `packages/shared-py/src`; the hook leaves out `services`, which CI
checks.

**The tree is `ruff format`-clean** (it was formatted once, in the commit listed
in `.git-blame-ignore-revs`). The pre-commit hook formats what you stage; without
the hook, run `uv run ruff format <files>` before committing, or CI's
`ruff format --check .` fails the push. To make `git blame` skip the formatting
commit locally: `git config blame.ignoreRevsFile .git-blame-ignore-revs`
(GitHub does it on its own).

TypeScript: `pnpm -r typecheck` (`tsc --noEmit`) is the check. The console is
styled with Tailwind utilities over the tokens, and the interaction layer
(dialog, menu, toast, tooltip) is Base UI; components use tokens and never
colour or size literals.

## Testing

```sh
uv run pytest -q                       # gateway, shared package and redaction service: about 1,900 tests (about nine minutes on a busy four-core machine)
pnpm -r test && pnpm -r typecheck      # packages/ui (26 tests) and the console (261)
uv run mkdocs build --strict           # this site
```

- **Gateway tests** (`apps/gateway/tests`, `packages/shared-py/tests`,
  `services/redaction/tests`) need no PostgreSQL, Valkey or network: SQLite, a
  fake upstream transport and an in-memory counter store. Run pytest from the
  repository root (`testpaths` and `pythonpath` live in the root `pyproject.toml`).
  One file: `uv run pytest apps/gateway/tests/test_cost.py -q`. Warnings are
  errors (`filterwarnings = ["error", …]`), and `asyncio_mode = "auto"`.
- **SQLite is more forgiving than PostgreSQL** in ways that hide real bugs, and
  the in-memory counter store is atomic only because it never awaits. Anything
  dialect-shaped or concurrency-shaped needs a live script (below), or a test
  that compiles against the dialect.
- **Console tests** are vitest on jsdom: `pnpm --filter @llmp/console test`, and
  `pnpm --filter @llmp/console typecheck`. `pnpm --filter @llmp/ui test` and
  `typecheck` cover the UI package.
- **The query budget** is a test: `apps/gateway/tests/test_query_counts.py` pins
  three SELECTs to authenticate and five SELECTs plus two writes for a metered
  request. Prefer `joinedload` to `selectinload` on many-to-one relations.
- **Live checks** run against a real stack, because more than half of the
  serious bugs in this project could only be found there. Bring the compose
  deployment up with the fake upstream (above), then:

  ```sh
  cd deploy && set -a; . ./.env; set +a      # the scripts read the deployment's own variables
  export PYSTINO_LIVE_ADMIN_PASSWORD=…       # the password behind AUTHELIA_ADMIN_PASSWORD_DIGEST
  cd .. && uv run python scripts/test_console_live.py
  ```

  The scripts are `scripts/test_*_live.py` (reporting, redaction, console,
  providers, surfaces, quota race, cache accounting, web search, `x-bill-to`,
  usage, citations and public TLS), plus `scripts/benchmark_live.py`. The
  [Operations](https://paoloviviani.github.io/Pystino/operations/#the-live-checks)
  page says what each covers and what they need. Two traps: do **not** source
  `deploy/.env` before `docker compose` (the shell's copy wins over `--env-file`,
  and quote removal mangles values with quotes or JSON; the *scripts* want it
  sourced, compose does not), and a whole run exhausts the demo group's cost
  ceiling because the fake upstream bills a million tokens per request, which the
  scripts report as skipped.
- **The full stack** is the `stack` workflow below; to try it by hand, run
  cerea-deploy's `./configure` and `docker compose up` with
  `PYSTINO_REGISTRY=local` and your gateway image.

A regression test must fail without the fix. A failure is "pre-existing" only
once it also fails on `main`. Say in the pull request which of the above you ran;
a step you could not run is reported as not run, never as passed.

**Small machines.** The gateway tests are light. Image builds, the console build
and a compose stack are not: do not run two heavy jobs at once, and prune the
images and build cache after a build (`docker builder prune -f`).

## CI

Four workflows in `.github/workflows/`.

| Workflow | Runs on | What it does |
|---|---|---|
| `ci` | every push to `main` and every pull request (changes to `docs/**` and `*.md` are ignored), and by hand | a `changes` job decides what to run. **gateway** (when `apps/gateway`, `packages/shared-py`, `services`, `deploy`, `uv.lock`, `pyproject.toml` changed): `uv sync --locked`, `ruff check`, `mypy apps/gateway/src packages/shared-py/src services`, `pytest`, then `docker compose config` for every profile of `deploy/compose.yaml`, the Caddyfile adapting in all three TLS modes, and `deploy/pin.py --check`. **console** (when `apps/console`, `packages/ui`, `pnpm-lock.yaml` changed): `pnpm install --frozen-lockfile`, then `typecheck` and `test` for `packages/ui` and for the console. A manual run does both. |
| `docs` | pushes to `main` that touch `docs/**` or `mkdocs.yml`, and by hand | `uv run mkdocs build --strict`, published to GitHub Pages at <https://paoloviviani.github.io/Pystino/> |
| `images` | by hand (`workflow_dispatch`) only | builds and pushes `pystino-gateway` and `pystino-redaction` (the `-pattern` flavour) to GHCR; on a release tag, a second job runs `pystino release-pin --check` |
| `stack` | a pushed `v*.*.*` tag, and by hand | builds the gateway and redaction images from the tag, brings up cerea-deploy's `main` with them and the Cerea image named in `deploy/release.env`, checks `/healthz` and `/chat/`, and brings up the Pystino-only `deploy/compose.yaml` too |

One gap worth knowing: the commit hooks and `docs` links to other repositories
are not checked by CI.

## Releasing

A release is a git tag `vX.Y.Z` (the version in `apps/gateway/pyproject.toml`
and `uv.lock`, without the `v`), paired with a Cerea release. Pystino and Cerea
are tagged separately; the kit that pins them is cerea-deploy.

1. **Green CI** on the commit to be tagged, and the live checks if the change
   touched the request path, money or SQL.
2. **The release commit**, once the Cerea release it names is published, named `release: Pystino X.Y.Z, paired with Cerea A.B.C`.
   It changes the version in `apps/gateway/pyproject.toml` and `uv.lock`; the
   `PYSTINO_VERSION` defaults in `deploy/compose.yaml`; and `deploy/release.env`
   (`PYSTINO_VERSION`, and `CEREA_VERSION` for the Cerea release this one was
   tested with). A unit test fails if `release.env` and `compose.yaml` disagree.
   Run `uv run --package gateway pystino release-pin` to pin the manifest's
   upstream images by digest (it needs `docker buildx` and registry access);
   `pystino release-pin --check` fails if any image is unpinned. Pystino's own
   images are pinned by version tag, not digest: the gateway image carries this
   manifest, so it cannot contain its own digest.
3. **Tag and push:** `git tag -a vX.Y.Z -m "…"`, `git push origin vX.Y.Z`. The
   `stack` workflow runs on the tag.
4. **Publish the images:** `gh workflow run images.yml --ref vX.Y.Z`. On a tag it
   pushes `pystino-gateway:X.Y.Z` and `:X.Y` (and `pystino-redaction:X.Y.Z-pattern`),
   refuses to overwrite a version that already exists (a release tag is
   immutable), then runs `pystino release-pin --check`. The packages are public:
   check that the image pulls with an empty Docker login.
5. **Hand over to cerea-deploy**, which pins the new pair, runs its tests and
   the fresh-kit sign-in (`E2E_OK`), writes its CHANGELOG entry and tags: see its
   [CONTRIBUTING.md](https://github.com/paoloviviani/cerea-deploy/blob/main/CONTRIBUTING.md#releasing).
   The sign-in check is `uv run python deploy/ci/e2e_login.py <kit dir> <password> --chat`
   from this repository; it prints `E2E_OK`.

**Versions.** `MAJOR.MINOR.PATCH`, one tag per release, never moved. A change
that needs the chat to move too names the Cerea release in the release commit.
The opencode release the gateway's setup script installs
(`opencode_pin` in `apps/gateway/src/gateway/client_scripts/opencode-install.sh`)
is kept in step by hand with Cerea's `agent/packaging/opencode-version`: when
Cerea bumps opencode, change it here in the same cycle.

## Documentation

The site is `docs/` built by mkdocs-material, strict (`uv run mkdocs build
--strict`), which fails on a broken link or anchor. `mkdocs.yml` has the
navigation. The `docs` workflow publishes `main` to GitHub Pages; the site is
not versioned per release. Links to another repository's page are absolute URLs
(strict cannot check them, so open them). This page, **Development**, is
`CONTRIBUTING.md` included into the site, so write its links as absolute URLs too.

Write plainly and concretely: say what the thing does, name the setting, give the
default. Check every claim that names a variable, a flag, a route, a label or a
default against the code before you commit it.

## Repository conventions

- **`AGENTS.md`** is the context file for coding agents working on this
  repository: the ground rules, the layout, the commands, and the traps that
  are not recoverable from the code. Read it before changing anything; agent-
  specific files for a particular tool are kept local and are not committed.
- **Commit messages and comments carry the reasoning**: why, especially where
  the obvious approach was rejected; the failure a decision prevents; the bug
  found while building. Release commits are named as above, merges of a topic
  branch are real merge commits with a message that says what the branch does.
- **The look lives in one file.** `packages/ui/src/tokens.css` carries the
  palette (light and dark), the type and the geometry, and the Inter fonts
  beside it. The Cerea chat vendors a **byte copy** of that file and of the font
  files, so it stays independently clonable. After changing `tokens.css` or
  `packages/ui/src/fonts/`, copy them into the Cerea checkout's `src/styles/`
  and `src/styles/fonts/`: Cerea's `src/styles/tokens.drift.test.ts` fails when
  the copy and this file differ (it runs only when the two repositories are
  checked out side by side as `Cerea/` and `Pystino/`, and skips otherwise).
  Never edit the copy alone.
- **Design records (ADRs)** are kept with the maintainer's planning material,
  not in this repository. Commit messages and `AGENTS.md` say what a decision
  was and why.
- **`deploy/pin.py`** keeps the `pystino.config-rev` labels in `deploy/compose.yaml`
  in step with `deploy/caddy/` and `deploy/authelia/`: run it after editing either,
  and CI runs it with `--check`.
