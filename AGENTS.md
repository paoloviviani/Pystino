# AGENTS.md: working on this repository

Pystino is a self-hosted OpenAI-compatible model gateway with accounting,
quotas, redaction and OIDC sign-in, plus its admin console. Read this before
changing anything: it is the context that is not recoverable from the code.
It is written to be enough on its own: the rules, how to make a change, how
to release, how this repository relates to Cerea, and the traps.

## Non-negotiable rules

Break none of these, whatever a task seems to need. If a task cannot be done
without breaking one, stop and ask the owner.

1. **No AI attribution in git.** Commit messages and pull requests carry no
   `Co-Authored-By` line naming an AI or a tool, no "Generated with" line, and
   nothing of the kind. Never pass `--author`; commits use the configured git
   identity.
2. **Licences.** All first-party code is Apache-2.0. A dependency must be under
   an OSI-approved licence, with no CLA and no open-core model; ask before
   adopting anything else. When describing the project's licensing, say
   "OSI-approved licences (Apache-2.0)" and nothing more: no licensing history,
   no employer names.
3. **Never change visibility unasked.** Do not make a repository, a GHCR
   package or anything else public or private unless the owner asked for that
   specific change.
4. **No work in progress on `main`.** Never push, fast-forward or merge a
   `WIP:` commit (or any unfinished work) to `main`. A topic branch reaches
   `main` only as `git merge --no-ff <branch>` with a message that says what
   the branch does, and only when the owner asked for the merge. Do not tag or
   release unless the owner asked for that release.
5. **Report what happened, with the output.** A failing test or command is
   reported as failing, with its output. A step you did not run is "not run",
   never "passed". A failure is "pre-existing" only once you have seen it fail
   on `main` too.
6. **New features are the owner's decision.** Fix what is broken; do not add a
   feature, an endpoint, a setting or a dependency because it seems useful.
   Propose it and wait.

## Ground rules

1. **Where you are unsure whether a library or version is current, say so.**
   Check at source rather than from memory; several choices here turned on
   details found only in a provider's live schema.
2. **Accounting and quota logic get tests specifically.** A wrong answer there
   is a wrong invoice, not a stack trace.
3. **The tree is `ruff format`-clean, and CI keeps it so.** Format what you
   change with the locked ruff (`uv run ruff format <files>`; the pre-commit
   hook does it on commit), and CI's `ruff format --check .` refuses anything
   else. A formatting-only commit goes in `.git-blame-ignore-revs`.
4. **Nothing but the proxy is published.** The compose file binds the gateway
   to `127.0.0.1`; the world reaches it through Caddy.
5. **One heavy job at a time on a small machine.** The full pytest run, the
   docs build, `pnpm build`, image builds and compose stacks each take most of
   a small box. On the shared build machine run them under
   `flock /tmp/heavy.lock <command>`, and never wrap a script that takes that
   lock itself in another `flock` on it (it deadlocks).

Commit messages and comments carry the *reasoning*, not a summary of the diff:
say why, especially where the obvious approach was rejected; name the failure
a decision prevents; record bugs found while building. In reports, no hedging:
if a test fails, say so with the output.

## Layout

```
apps/gateway        the gateway: /v1 surfaces, /api management, console hosting
apps/console        React admin SPA, served by the gateway at /console
packages/ui         design tokens and primitives, shared with the console
packages/shared-py  detection contract and the deterministic placeholder scheme
services/redaction  Presidio behind a swappable contract, out of process
deploy/             the Pystino-only deployment: compose.yaml, caddy/ and
                    authelia/ (mounted read-only into stock images),
                    .env.example, pin.py, release.env
deploy/dev/         development-only fixtures: fake upstream, Keycloak
deploy/ci/          e2e_login.py, a browser-shaped sign-in check run by hand
scripts/            live checks against a running stack, the fake upstream
docs/               how to run, deploy and operate it (mkdocs)
```

Two workspaces. Python is a `uv` workspace of `apps/gateway` and
`packages/shared-py`; `services/redaction` is deliberately **not** a member
(spaCy must never enter the gateway's lockfile), but its tests still run from
the repository root. JS/TS is a `pnpm` workspace of `packages/ui` and
`apps/console` (packages `@llmp/ui` and `@llmp/console`). Alembic migrations
(`apps/gateway/migrations/versions/NNNN_*.py`) are excluded from ruff and mypy.

The gateway accepts a caller-supplied `x-request-id` and never returns the one
it uses: a caller who wants a transcript tied to cost mints it and sends it.

The deployment CLI in the gateway image (`pystino`, `gateway/deploy/cli.py`,
whose `build_parser` is the list to trust) runs inside a deployment and never
writes `.env`; every command takes its answers as flags, so none prompts:

| Command | What it is |
|---|---|
| `bootstrap` | the one-shot compose service, run on every `up` |
| `admin grant\|revoke <email> [--issuer]` | the ordinary-case admin recovery |
| `break-glass --email …` | the deeper recovery to the bundled Authelia; prints a login and password once, to stdout only; the deploy kit's `./configure --break-glass` runs it |
| `idp check` | a live probe of the configured identity provider |
| `email export-env` | the mail configuration in force as `KEY=VALUE` lines, password included, for `./configure --import-smtp` |
| `erasure list\|retry <id>` | the chat erasure queue the background retry loop owns: see it, or force one attempt now |
| `quota health` | each quota rule's counter against the ledger, as JSON; read-only |
| `release-pin [--manifest] [--check]` | a maintainer tool: pin the release manifest's images by digest |

The separate `gateway` command (`gateway/cli.py`) is the development one:
`serve` and `seed`. The chat, Cerea, and its machine agent, galopin, live in
their own repository; the full-stack deployment is the Cerea repository's
deploy kit (`kit/`).

Inside the gateway, the pieces that carry the most weight:

| Path | What it owns |
|---|---|
| `routers/_metered.py` | resolve → reserve → record → settle, shared by every metered `/v1` route |
| `protocols.py` | per API surface: where usage, the served model and assistant text live in a frame |
| `accounting/cost.py` | the money arithmetic, and the three prompt slices |
| `quota/engine.py` | admission; `quota/counters.py` has the three stores |
| `access.py` | one predicate for "may this caller use this model" |
| `pagination.py` | the listing envelope every management route returns |

## How Pystino and Cerea relate

Two repositories, released separately, shipped together by Cerea's deploy kit.

- **Cerea (the chat) talks to the gateway only through `/v1`**, with the
  signed-in person's own OIDC access token: chat completions, embeddings, OCR,
  models, `GET /v1/me`, `GET /v1/me/identities`, `GET /v1/billing/groups`,
  `GET /v1/pystino/usage`, and `POST /v1/session/announce` (the chat's sign-in
  door). It never calls `/api`, which reads a session cookie and nothing else.
  The one call in the other direction is erasure: deleting a person here calls
  the chat at `GATEWAY_CHAT__ERASURE_URL`. A change to any of those `/v1`
  routes is a change to Cerea's contract.
- **Cerea's `contract` workflow tests a pinned Pystino.** It runs the
  published `pystino-gateway` image at the version in Cerea's
  `deploy/ci/pystino-contract.env` (`PYSTINO_CONTRACT_VERSION`) against the
  chat's gateway-facing code, on Cerea pull requests that touch that code and
  by hand. A Pystino release that changes what the chat relies on needs that
  file bumped in Cerea.
- **The deploy kit lives in Cerea** (`kit/`, released with Cerea's tags; operators
  follow its `stable` branch or a tag; `kit/get-kit.sh` fetches it). The
  separate `cerea-deploy` repository is archived: never send anyone there.
  This repository's `deploy/` is the Pystino-only deployment, with the same
  variable names.
- **`packages/ui/src/tokens.css` is mirrored byte for byte into Cerea's
  `src/styles/tokens.css`**, and `packages/ui/src/fonts/` into
  `src/styles/fonts/`. After changing either here, copy them into the Cerea
  checkout in the same cycle and never edit Cerea's copy alone. Cerea's
  `src/styles/tokens.drift.test.ts` fails on a difference, but only when the
  two repositories are checked out side by side as `Cerea/` and `Pystino/`.
- **The opencode pin** (`opencode_pin` in
  `apps/gateway/src/gateway/client_scripts/opencode-install.sh`) follows
  Cerea's `agent/packaging/opencode-version` by hand.
- **Releases are paired.** `deploy/release.env` here names the Cerea version a
  Pystino release was tested with; Cerea's kit pins the Pystino version it
  ships (`kit/tools/pin --pystino X.Y.Z`). See "Releasing Pystino" below.

## Making a change, step by step

1. **Branch from an up-to-date `main`:**
   `git fetch origin && git switch -c fix/<topic> origin/main`
   (`docs/…`, `chore/…` for other kinds). Never commit to `main` directly.
2. **Read first.** This file; the code you will change and its tests; the
   docs page that describes the behaviour. `grep -rn` the setting, route or
   function name across `apps/`, `packages/`, `services/`, `deploy/` and
   `docs/` so you know everything that refers to it.
3. **Make the smallest change that fixes the problem.** No drive-by
   refactors, no renames, no reformatting of files you did not otherwise
   touch.
4. **Write the failing test first, and see it fail.** Add the test, run it,
   and keep the failure output: it must fail for the reason the bug describes.
   Then fix, and run it again. For a bug fix, confirm the test fails without
   the fix (`git stash` the fix, run the test, `git stash pop`).
5. **Run the checks**, from the repository root, and keep their output:

   ```bash
   uv sync
   uv run ruff format <the .py files you changed>
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy apps/gateway/src packages/shared-py/src services       # --strict; CI runs exactly this
   flock /tmp/heavy.lock uv run pytest -q                             # ~1,900 tests, SQLite, no services
   uv run pytest apps/gateway/tests/test_migrations_sqlite.py -q      # the whole migration chain on SQLite
   ```

   A single test file: `uv run pytest apps/gateway/tests/test_cost.py -q`.
   The mypy pre-commit hook covers `apps/gateway/src` and
   `packages/shared-py/src` only; `services` is checked by the command above
   and by CI.

   If you touched the console or `packages/ui`:

   ```bash
   pnpm install --frozen-lockfile
   pnpm -r typecheck && pnpm -r test                 # packages/ui and the console (vitest)
   flock /tmp/heavy.lock pnpm build                  # tsc + vite build of the console
   ```

   If you touched `docs/`, `mkdocs.yml`, `CONTRIBUTING.md` or a README the
   site includes:

   ```bash
   flock /tmp/heavy.lock uv run mkdocs build --strict
   ```

   If you touched `deploy/caddy/` or `deploy/authelia/`: `deploy/pin.py`, then
   `deploy/pin.py --check` (CI runs the check). Editing those directories, even
   a comment, changes the service's `pystino.config-rev` label, which recreates
   the container on every deployment's next `up`.
6. **Run the live checks** if the change touches the request path, money or
   SQL (below). They are the only place PostgreSQL and Valkey are exercised.
7. **Update the docs in the same branch.** The page that describes the
   behaviour must match the code when the branch lands; check each name,
   flag, route and default you write against the code.
8. **Commit** with a plain message that says why (no AI trailer, rule 1), and
   push the branch: `git push -u origin <branch>`. Do not merge it unless the
   owner asked. When asked:
   `git switch main && git pull --ff-only && git merge --no-ff <branch> -m "Merge <branch>: <what it does>" && git push origin main`.
9. **Report**: what changed and why, each check you ran with its result, and
   each check you did not run, said as "not run".

### Migrations

Alembic, under `apps/gateway/migrations`:
`uv run alembic -c apps/gateway/alembic.ini revision -m "…"` to add one,
`… upgrade head` to apply. The compose `migrate` service runs them on every
`up`, and they only go forward. A migration must also run on SQLite (the smoke
test and the quick start use it): branch on `op.get_bind().dialect.name`, as
0027 and 0047 do, and put any `ALTER` of a constraint through
`op.batch_alter_table`. `test_migrations_sqlite.py` runs the whole chain there.

### The console

Built into the gateway image (`INCLUDE_CONSOLE=true`); for development,
`pnpm dev` (vite on :5173, proxying `/api`, `/auth` and `/v1` to a gateway on
:8000). Without a Node toolchain on the host, run `pnpm` in a container
(`node:24-bookworm-slim`, with `CI=true` and `corepack enable`), and never
`pnpm config set --location project`: it writes the container's store path
into `pnpm-workspace.yaml`, a committed file.

### The live checks

For anything touching the request path, money or SQL, against a real stack:
fill `deploy/.env` from `deploy/.env.example`, bring it up with the fake
upstream, and run the live checks:

```bash
PYSTINO_SRC=$PWD docker compose -f deploy/compose.yaml -f deploy/dev/smoke.yml up -d --wait
set -a; . deploy/.env; set +a            # the scripts read the deployment's own variables
export PYSTINO_LIVE_ADMIN_PASSWORD=…     # the password behind AUTHELIA_ADMIN_PASSWORD_DIGEST
uv run python scripts/test_reporting_live.py         # dialect-specific SQL the suite cannot reach
uv run python scripts/test_redaction_live.py
uv run python scripts/test_console_live.py           # console, CSP, pagination
uv run python scripts/test_providers_live.py         # credentials encrypted in PostgreSQL, routing
uv run python scripts/test_surfaces_live.py          # responses, Anthropic messages, images
uv run python scripts/test_quota_race_live.py        # admission under concurrency, real Valkey
uv run python scripts/test_cache_accounting_live.py  # a real cache hit, and the ledger
uv run python scripts/test_web_search_live.py        # per-search billing, the report and its CSV
uv run python scripts/test_bill_to_live.py           # x-bill-to against a real OIDC token
uv run python scripts/test_pystino_usage_live.py     # GET /v1/pystino/usage under both credentials
uv run python scripts/test_citations_live.py         # citation offsets after a real placeholder
uv run python scripts/test_public_tls_live.py        # TLS, the IdP, a Secure cookie, nothing else exposed
uv run python scripts/benchmark_live.py              # per-layer cost; see docs/performance.md
```

They sign in through the bundled Authelia (`scripts/live_session.py`), so the
stack needs the `authelia` profile. **Run them.** More than half the serious
bugs in this project were only findable against a running stack: a counter
seeded at zero, a migration given the wrong environment, a 500 on `/v1/models`
that every unit test passed through.

Traps when running them:

- **Do not source `deploy/.env` before `docker compose`.** Compose prefers the
  shell environment over `--env-file`, and bash's quote removal mangles
  values that contain quotes or JSON. The *scripts* want it sourced; compose
  does not.
- **A whole suite exhausts the test group's cost ceiling**, because the fake
  upstream bills a million tokens per request. The scripts report that as
  skipped. Flushing Valkey alone does not reset it: the counters rebuild from
  `usage_records`, so clear both, or raise the rule temporarily.
- **From a git worktree, `deploy/.env` does not exist** (it is gitignored).
  Symlink it, or scripts that shell out to `docker compose --env-file` do
  nothing.

## Releasing Pystino

Only when the owner asked for a release. A release is an annotated git tag
`vX.Y.Z`, the version in `apps/gateway/pyproject.toml` without the `v`. It is
paired with a published Cerea release `A.B.C` (the one it was tested with),
and it reaches operators only when a later Cerea release pins it in the deploy
kit. Each step names what to check before going on; stop at the first failure
and report it with its output. Derived from `.github/workflows/images.yml`,
`stack.yml`, `ci.yml`, `deploy/release.env`, `pystino release-pin`
(`gateway/deploy/release.py`), the 0.3.x release commits and runs, and Cerea's
`scripts/release/release.sh`.

1. **Green CI on `main`**:
   `gh run list --workflow ci.yml --branch main --limit 3` shows success for
   the commit you will release (a docs-only commit has no `ci` run; check the
   last code commit). If the release contains changes to the request path,
   money or SQL, the live checks have been run against it.
2. **The release commit**, on `main` (the owner asked for the release; this is
   the one commit that goes there without a branch, as every 0.3.x release
   did), named `release: Pystino X.Y.Z, paired with Cerea A.B.C`. It changes
   exactly:
   - `apps/gateway/pyproject.toml`: `version = "X.Y.Z"`;
   - `uv.lock`: run `uv lock`, which moves the `gateway` package's version;
   - `deploy/compose.yaml`: both `${PYSTINO_VERSION:-…}` defaults (the gateway
     and redaction image lines);
   - `deploy/release.env`: `PYSTINO_VERSION=X.Y.Z`, `CEREA_VERSION=A.B.C`, and
     the tag inside `CEREA_IMAGE` (`…/cerea:A.B.C@sha256:…`). `release-pin`
     keeps whatever tag `CEREA_IMAGE` already has and only refreshes its
     digest, and `--check` does not compare it with `CEREA_VERSION`, so a
     forgotten tag pins the old chat silently. Either edit the tag, or delete
     the `CEREA_IMAGE` line and let `release-pin` derive it.

   Then:

   ```bash
   uv run --package gateway pystino release-pin          # rewrites release.env digests; needs docker buildx
   uv run --package gateway pystino release-pin --check  # prints nothing, exits 0
   uv run pytest apps/gateway/tests/test_deploy_pystino_only.py apps/gateway/tests/test_deploy_release.py -q
   git diff                                              # only the four files above
   ```

   The first test fails if `release.env` and `compose.yaml` disagree on the
   version. Pystino's own images are pinned by version tag, not digest: the
   gateway image carries this manifest, so it cannot contain its own digest.
   Commit, `git push origin main`, and wait for `ci` on that commit to pass.
3. **Tag and push the tag:**

   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z
   ```

   The push starts the `stack` workflow on the tag: it builds the tag's
   gateway image itself, brings it up inside the deploy kit (Cerea's `kit/`
   from Cerea's default branch, sparse checkout, with the Cerea image named by
   `CEREA_VERSION` in `release.env`) and checks `/healthz` and `/chat/`, and
   brings up `deploy/compose.yaml` alone and checks `/healthz`.
4. **Publish the images** (automatic publishing is on hold, so this is by
   hand):

   ```bash
   gh workflow run images.yml --ref vX.Y.Z
   gh run list --workflow images.yml --limit 1           # then: gh run watch <id>
   ```

   On a tag it pushes `ghcr.io/paoloviviani/pystino-gateway:X.Y.Z` and `:X.Y`,
   and `pystino-redaction:X.Y.Z-pattern` and `:X.Y-pattern`; it refuses to
   overwrite a version that already exists (a release tag is immutable). Its
   second job, `release-pins`, runs `pystino release-pin --check`. Both jobs
   must pass.
5. **Check the images pull with no login** (the packages are public; do not
   change that):

   ```bash
   anon=$(mktemp -d); echo '{}' > "$anon/config.json"
   DOCKER_CONFIG=$anon docker manifest inspect ghcr.io/paoloviviani/pystino-gateway:X.Y.Z >/dev/null && echo gateway OK
   DOCKER_CONFIG=$anon docker manifest inspect ghcr.io/paoloviviani/pystino-redaction:X.Y.Z-pattern >/dev/null && echo redaction OK
   rm -rf "$anon"
   ```

6. **`stack` green for the tag**: `gh run list --workflow stack.yml --limit 1`
   shows success on `vX.Y.Z`.

   No GitHub "release" object is created for Pystino tags (the last one is
   v0.2.0); the tag and the images are the release.

7. **Ship it through Cerea.** In the Cerea repository, following its
   `AGENTS.md` "Releasing" (authoritative there):
   - the Cerea release commit on its `main`: the version in `package.json`,
     `kit/tools/pin --cerea <new Cerea version> --pystino X.Y.Z`, then
     `kit/tools/pin --check` and the kit's unit tests
     (`cd kit && python3 -m unittest discover -s tests`), and a `## v<version>`
     entry in `kit/CHANGELOG.md`; if this Pystino release changes what the
     chat relies on, `PYSTINO_CONTRACT_VERSION=X.Y.Z` in
     `deploy/ci/pystino-contract.env` as well;
   - push it, then run `scripts/release/release.sh <new Cerea version>`. It
     waits for Cerea's `ci` and `kit` workflows, tags, publishes the chat
     image, checks the anonymous pull, runs the fresh-kit sign-in
     (`scripts/release/fresh-kit-check.sh`, which must print `E2E_OK`), moves
     `stable` and creates the GitHub release. It is done only when it prints
     `released v<version>`.

**Not verified end to end** (this file was written without running a
release): the `stack` workflow's checkout of Cerea's `kit/` instead of the
archived `cerea-deploy` repository has not run on a tag yet, so watch the first
release's `stack` run closely; `pystino release-pin` against the registries
was not run while writing this. Every other step above matches the workflow
files and the successful v0.3.1 and v0.3.2 runs.

## Non-obvious things that will bite you

- **The two prompt conventions are opposites.** OpenAI's `prompt_tokens`
  *includes* cached tokens; Anthropic's `input_tokens` *excludes* them. Each
  surface has its own named reader in `accounting/cost.py`. Do not "simplify"
  them into one tolerant parser.
- **But cache-write *spellings* are tolerated, deliberately.** There are four
  names for that one quantity (`_CACHE_WRITE_KEYS`), and reading only one bills
  those tokens at the input rate. Those differ only in spelling; the prompt
  conventions differ in meaning.
- **Three cost figures, one meaning each.** `cost` is what we charge, and what
  quotas and reports read; `computed_cost` is always our arithmetic;
  `upstream_cost` is always the counterparty's. `cost_source` says which one
  billed, and `own_prices_fallback` means a pass-through provider reported
  nothing. An unpriced model reserves nothing, so it has no cost ceiling at
  all; `unpriced_model_count` on the provider listing is the warning.
- **A plugin returns facts and never computes money.** Vendor quirks live in
  `gateway/plugins/`: header names, the unit a provider reports cost in
  (`usage.cost` is micro-EUR from Cortecs and credits from OpenRouter, and
  nothing in the payload says which), provider or router.
  `accounting/cost.py` is the only code that multiplies a count by a rate.
  `ReportedCost.authoritative` is what gates pass-through billing: reporting a
  figure is not claiming it is the charge.
- **SQLite is more forgiving than PostgreSQL** in ways that hide real bugs. It
  will `SELECT DISTINCT` over a JSON column; PostgreSQL has no equality
  operator for `json`. The unit suite runs on SQLite, so anything
  dialect-shaped needs a live script or a compile-against-the-dialect test.
- **Money is a string end to end.** `Numeric(24,12)` round-trips zero as
  `Decimal("0E-12")`; the `Money` type in `schemas.py` forces plain digits.
  Never parse an amount into a float, including in the browser.
- **Stored precision is twelve places; displayed precision is three.**
  `formatMoney` in `packages/ui` and `format_money_prose` in `gateway/types.py`
  default to milli-units, opt-out with `{ exact: true }`, and never round a real
  amount to zero (it reads `< €0.001`). Admins reach full precision with the
  **Exact figures** toggle, a `MoneyPrecisionContext`: a `formatMoney` call
  outside `<Money>` reads it with `useExactMoney`.
- **The look lives in one file.** `packages/ui/src/tokens.css` carries the
  palette (light and dark), the type and the geometry; components use tokens,
  never literals. Components are styled with Tailwind utilities, and the
  interaction layer (Dialog, Menu, Toast, Tooltip) is Base UI. Dark mode is one
  `.dark` block plus a class on `<html>`. Tailwind's content detection is
  rooted at the Vite project root, so `apps/console/src/index.css` declares
  `@source` for `packages/ui/src`: if a class "does nothing", check that line.
  Cerea carries a byte copy of `tokens.css` (see "How Pystino and Cerea
  relate").
- **Provider-side web search is a surcharge.** `per_search` on the price row,
  `search_count` on the usage row. The count comes only from
  `usage.server_tool_use.web_search_requests`, never from counting blocks (an
  errored search produces a block and is not billed). Searches are recorded
  even when the model has no rate, so the gap is findable here rather than on
  an invoice.
- **There are two search counts; do not add them together.**
  `usage_records.search_count` is the counterparty's server-side search,
  billed per search. `usage_records.own_search_requests` is a call this gateway
  made to its own search backends (Linkup, Exa, Jina, DuckDuckGo), **counted, never
  priced**, and limited by the `OWN_SEARCH_REQUESTS` quota metric; only
  `/v1/search` reserves against it (one per call), every other request
  reserves zero. A request ceiling bounds volume, not spend, and the quota
  form says so.
- **A search backend is a provider, and a search "model" is a tier.**
  `POST /v1/search` resolves a `ModelKind.SEARCH` model whose `upstream_model`
  is the vendor's depth or type, behind a provider whose plugin implements
  `SearchPlugin`. A search is counted before the call and never refunded. The
  query is redacted, which genuinely degrades it; the fix is the redaction
  scope, not an exemption. Exa's `costDollars` is logged, never stored.
- **Restoring a placeholder moves every offset after it.** Provider citations
  are character offsets into the answer, so `restore_with_edits` (in
  `packages/shared-py`) reports where it wrote and each surface protocol's
  `shift_citations` moves what it holds. A `Shift` maps `(choice index,
  offset)` to the new offset. Anthropic needs nothing moved. The streamed case
  needs the whole answer, so the rewriter keeps the provider's text per choice.
- **Redaction is most of the CPU, and it scales with prompt length**: about
  0.1 ms per prompt token, against a gateway cost that stays flat. Its detection
  cache is a per-process LRU, so more workers lower the hit rate.
- **The final ledger write happens after the response is sent** for the
  non-streamed routes (`metered.completed_after_response`). A failure there is
  logged, not raised, so the row stays `in_progress`. It is worse under
  saturation, because awaiting the settle was accidental backpressure.
  httpx's ASGI transport awaits background tasks, so `test_deferred_settlement.py`
  drives raw ASGI to prove the body goes out first.
- **A finished stream settles in a detached task.** The streaming routes'
  `finally` calls `settle_completed` (in `routers/chat.py`), which runs the
  write as a task and awaits it through `asyncio.shield`, because a client that
  hangs up the instant it reads the last frame gets the request task cancelled
  mid-write. The disconnect branch uses `spawn_finalisation` for the same
  reason. Never turn either back into a bare `await`;
  `test_stream_settlement.py` covers it.
- **`/api` reads a session cookie and nothing else.** Anything a
  bearer-authenticated client needs cannot live there. `GET /v1/me` is how a
  bearer caller learns who it is: `is_admin` comes from effective memberships,
  never the token's claim, and an API key never gets `is_admin: true`.
- **A request may choose which group pays, and a key may not.**
  `x-bill-to: <group name>` is honoured only for OIDC access tokens; a key
  sending it is refused, not ignored. The lookup walks the caller's own
  memberships, so a group they do not hold is indistinguishable from one that
  does not exist. `GET /v1/billing/groups` is what a client reads to offer the
  choice.
- **A directory owns the memberships it granted, and no others.**
  `memberships.source` is `oidc` or `manual`; a login's sync never touches
  manual ones. `identity_providers.group_sync` says how often the directory
  answers. `is_admin`, the default billing group and the sole-group rule read
  effective memberships, not the token.
- **A claimed group name nothing carries is recorded, not created**
  (`GATEWAY_OIDC__GROUP_IMPORT=manual`, the default; `group_import.py`):
  `seen_groups` per issuer, `seen_group_users` for who carries it, and the
  console imports or dismisses. `users.unresolved_group_names` is what keeps
  the bearer path write-free: `_claims_diverge` subtracts it, or one
  unimported name would re-provision on every `/v1` request (12 selects
  instead of 5; `test_group_import.py` pins it). The default group
  (`GATEWAY_OIDC__DEFAULT_GROUP`, `users`) is granted once per person,
  stamped in `users.default_group_granted_at`, so an administrator's removal
  sticks; it never counts as a billing "choice" in the sole-group rule.
- **A deployment may keep no ledger.** `GATEWAY_ACCOUNTING__ENABLED=false`
  writes no `usage_records`, never a row of zeros, and the report announces
  that metering is off. Quotas without a ledger are refused at startup, since
  counters rebuild from `usage_records`.
- **A quota refusal writes no ledger row.** The `429` (`quota_exceeded`) is
  returned before the in-progress row is created; the statuses a row can carry
  are `in_progress`, `completed`, `client_disconnected`, `upstream_error` and
  `blocked`.
- **OIDC discovery is read once at startup**, so changing the issuer needs a
  restart, and **`iss` is part of a user's identity**: users are keyed on
  `(issuer, subject)`, so a new issuer re-provisions everyone.
- **Behind the proxy, uvicorn must trust the forwarded headers**
  (`FORWARDED_ALLOW_IPS`), or the app believes every request is http, and the
  only symptom is a post-logout URL a provider refuses. The session cookie is
  scoped to the origin the login happened on, so sign in and check on the same
  address.
- **Per-request round trips are pinned by a test.** `test_query_counts.py`
  bounds them: 4 selects for `GET /v1/models` (authentication plus the
  catalogue and the reachable agents), 5 selects + 2 writes for a metered
  request, and a bearer token may cost no more than a key. `selectinload` on a
  many-to-one relation costs a round trip that `joinedload` does not.
- **`InMemoryCounterStore` is atomic only because it never awaits.** It proves
  nothing about `MULTI`/`EXEC`, which is why `test_quota_race_live.py` exists.
- **An abandoned stream is billed from our price table.** A client that hangs
  up before the terminal SSE frame gets no usage and no reported cost from the
  provider, so tokens are estimated and cost falls back to our prices, even on
  a pass-through provider, and the reconciliation excludes those rows.
- **`deploy/caddy/` and `deploy/authelia/` are hashed into a label.**
  `deploy/pin.py` writes a `pystino.config-rev` digest of each directory onto
  its service in `compose.yaml`, so any edit there, a comment included,
  recreates that container on the next `up` and needs `deploy/pin.py` run (CI
  runs `--check`). A few comments there still name the archived
  `cerea-deploy`; they are left until a real change touches those files.

## Known open items

- **The estimated-usage disclosure blames the provider for client
  disconnects.** `_disclosures` in `reporting.py` should split its sentence on
  `status`.
- **Deleting a person does not blank `assistant_text`** on their ledger rows;
  only the retention sweep (`GATEWAY_TRANSCRIPT_RETENTION_HOURS`, default 24)
  clears it.
- **Reranking, image editing and variations, and per-size image pricing** are
  not implemented (`image_size` is recorded on every image row; nothing prices
  by it).
- **Comments in `deploy/cli.py` still name `cerea-deploy`**: the deploy kit
  is now Cerea's `kit/`. Fix them with the next change to that file.
