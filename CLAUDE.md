# Working on this repository

A self-hosted model gateway.
Read this before changing anything; it is the context that is not recoverable
from the code.

## Ground rules, in priority order

1. **EUPL-1.2 for all first-party code, and licensing is a hard requirement.**
   Ask before adopting anything with a non-OSI licence, a CLA, or an open-core
   model. Not a preference to be traded off — see
   ADR 0001.
2. **Where you are unsure whether a library or version is current, say so.**
   Research at source rather than from memory; several decisions here turned on
   details found only by reading the provider's live schema. Guessing a version
   number and being wrong is worse than asking.
3. **Accounting and quota logic get tests specifically.** That is where
   correctness actually matters, and where a wrong answer is a wrong invoice
   rather than a stack trace.
4. **The dev stack goes on a routable address only with the proxy overlay.**
   The rule used to be "never", and the reason was a list of specific things,
   not a principle: there is no TLS, and the session cookie is not `Secure`.
   The base compose files bind `127.0.0.1` because both are true of them.
   `docker-compose.proxy.yml` closes both — Caddy terminates TLS, and the only
   management credential that shape seeds (the local admin's, ADR 0043) comes
   from `deploy/.env`, which is not in the repository
   (ADR 0035); it is still not a
   production deployment, and that ADR says exactly why. Without it, reach the
   stack over the SSH tunnel. **Nothing else may be published.**
   `scripts/test_public_tls_live.py` is what checks that, by
   requiring every other port to be refused on this host's routable address.

## Explaining the work

Commit messages and code comments here carry the *reasoning*, not a summary of
the diff. The house style, worth matching:

- Say **why**, especially where the obvious approach was rejected and why it
  was wrong. A comment that restates the code earns nothing.
- Name the failure a decision prevents. "Refused rather than converted: an
  exchange rate applied silently produces invoices that look right and are
  wrong."
- Record bugs found while building, in the commit and in the ADR. Several of
  the most valuable notes in this repo are of that shape.
- No hedging in reports. If a test fails, say so with the output. If something
  is unverified, say which part.

## Three repositories, since 2026-09-08

This one is **the gateway and its console**, and nothing else. Two others hold
what used to share the monorepo:

| Repository | What | Why it is not here |
|---|---|---|
| the decision record (internal) | ADRs 0001–0062, the whole-stack architecture, the roadmap and the scope of unstarted work | The series spans a gateway, a chat app, a RAG pipeline and three design languages. A numbered sequence cannot be split without renumbering, which its index forbids |
| [pystino-chat](https://gitlab.linksfoundation.com/viviani/pystino-chat) | The chat application, a `/v1` **client** | It imports nothing from the gateway: it talks over `/v1` with a key or an OIDC bearer (ADR 0040, ADR 0046). On the day of the split its monorepo branch was 56 commits behind, and every gateway change made the merge worse |

**Citing a decision:** by number, in prose — `(ADR 0032)`, never as a link.
The record is **not public and never will be**, so a URL into it promises a
source the reader cannot open and advertises a private repository's address; the
number names the reasoning so it can be asked for. Roughly 690 such citations
here were untouched by the repository split, which is the whole point of
numbering them. The next number is the next number in the record, whichever
repository the work lands in.

## Where things are

```
apps/gateway     the gateway: /v1 proxy surfaces, /api management, console hosting
apps/console     React admin SPA, served by the gateway at /console
packages/ui      design tokens and primitives, shared with the console
packages/shared-py  detection contract and the deterministic placeholder scheme
services/redaction  Presidio behind a swappable contract, out of process
deploy/compose   the stack: base + smoke + redaction + playwright + proxy +
                 keycloak + chat overlays. The playwright one is a headless
                 browser for fetching a URL somebody named — publishing no
                 port, and docs/browser.md says why it never can
deploy/caddy     the TLS reverse proxy's one config file, for both configurations
scripts/         live checks against a running stack (see below)
docs/            how to run, deploy and operate this. The ADRs are not here
                 and are not linked — cite them by number; see below.
```

Inside the gateway, the pieces that carry the most weight:

| Path | What it owns |
|---|---|
| `routers/_metered.py` | resolve → reserve → record → settle, shared by every metered `/v1` route **and by knowledge-base ingestion** |
| `protocols.py` | per API surface: where usage, the served model and assistant text live in a frame |
| `accounting/cost.py` | the money arithmetic, and the three prompt slices |
| `quota/engine.py` | admission; `counters.py` has the three stores |
| `access.py` | one predicate for "may this caller use this model" |
| `sharing.py` | its sibling: "may this caller reach this shared resource" (ADR 0062) |
| `knowledge/` | extract → chunk → embed → store, and the retrieval contract |
| `pagination.py` | the listing envelope every management route returns |

## Non-obvious things that will bite you

- **The two prompt conventions are opposites.** OpenAI's `prompt_tokens`
  *includes* cached tokens; Anthropic's `input_tokens` *excludes* them. Each
  surface has its own named reader in `accounting/cost.py` for this reason. Do
  not "simplify" them into one tolerant parser.
- **But cache-write *spellings* are tolerated, deliberately.** There are four
  names for that one quantity — `_CACHE_WRITE_KEYS` in `accounting/cost.py` —
  and reading only one bills those tokens at the input rate. The distinction
  from the rule above is the point: those differ in **meaning**, these differ
  only in **spelling**. See
  [docs/cache-accounting-findings.md](docs/cache-accounting-findings.md).
- **Three cost figures, one meaning each.** `cost` is what we charge and is what
  quotas and reports read; `computed_cost` is always our arithmetic; and
  `upstream_cost` is always the counterparty's. `cost_source` says which one
  billed, and `own_prices_fallback` means a pass-through provider reported
  nothing — never silent, because that would be billing from a price table
  nobody maintains. An unpriced model reserves nothing, so it has no cost
  ceiling at all; `unpriced_model_count` on the provider listing is the warning.
- **A provider's own reported cost is its plugin's to read.** `usage.cost` is
  micro-EUR from Cortecs and credits from OpenRouter, and nothing in the payload
  says which — so the unit lives in the plugin, not in a column an operator fills
  in. A plugin that does not read a cost reports none, which is the safe default.
  `ReportedCost.authoritative` is separate and is what gates pass-through
  billing: reporting a figure is not claiming it is the charge.
- **SQLite is more forgiving than PostgreSQL** in ways that hide real bugs. It
  will `SELECT DISTINCT` over a JSON column; PostgreSQL has no equality
  operator for `json` at all. The unit suite runs on SQLite, so anything
  dialect-shaped needs a live script or a compile-against-the-dialect test.
- **The look is a neutral greyscale with one indigo accent, and it lives in one
  file.** `packages/ui/src/tokens.css` carries the palette (light **and dark**),
  the type and the geometry, and components reference tokens and never literals
  — restyles have landed three times now as a change to that file alone
  (ADR 0042 twice, and
  ADR 0047). Since 0047 the
  components are styled with **Tailwind utilities** and the interaction layer
  (Dialog, Menu, Toast, Tooltip) is **Base UI**, both verified MIT at source;
  the tokens stay plain custom properties, so a consumer that runs no Tailwind
  can still import the file. Three things worth knowing: **dark mode is one
  `.dark` block in that file plus a class on `<html>`** — nothing else knows it
  exists, and utilities follow it because they read the custom properties
  (`@theme inline`); **the decorative half is still deliberately absent** — no
  blur, glass, glow, or scale-on-hover; and **Tailwind's content detection is
  rooted at the Vite project root**, so `apps/console/src/index.css` declares
  `@source` for `packages/ui/src` — if a class "does nothing", that line not
  naming its file is the first thing to check. Roboto is vendored under the
  Apache License 2.0, licence beside it in `packages/ui/src/fonts/`.
- **Money is a string end to end.** `Numeric(24,12)` round-trips zero as
  `Decimal("0E-12")`; the `Money` annotated type in `schemas.py` forces plain
  digits. Never parse an amount into a float, including in the browser.
- **Stored precision is twelve places; *displayed* precision is three.** The
  ledger's precision is real and is kept; putting it on a screen is not. Both
  formatters default to milli-units — `formatMoney` in `packages/ui` and
  `format_money_prose` in `gateway/types.py`, for figures embedded in a
  disclosure the console renders verbatim. Three things about that default:
  it is **opt-out** (`{ exact: true }`), because when it was opt-in four of the
  five screens leaked twelve decimals by saying nothing; it is *at most* three,
  so €12.50 is not written €12.500; and a real amount **never rounds to zero** —
  it reads `< €0.001`, because "€0.00" for genuine spend makes the ledger look
  broken. Admins reach full precision with the **Exact figures** toggle in the
  identity menu, which is a `MoneyPrecisionContext` — a `formatMoney` call made
  outside `<Money>` has to read it by hand (`useExactMoney`).
- **Provider-side web search is a billable unit, and it is a *surcharge***
  (ADR 0058). `per_search` on the price
  row, `search_count` on the usage row. Unlike a page or an image it lands on
  an ordinary chat request, which is why it went unbilled: the tool passes
  through `extra="allow"` untouched, so search already worked and cost nothing.
  Three things to know. The count comes **only** from
  `usage.server_tool_use.web_search_requests` — never from counting
  `server_tool_use` blocks, because an errored search produces a block and is
  not billed. The searches are recorded **even when the model has no
  `per_search` rate**, charging nothing but leaving the gap findable here
  instead of on an invoice. And the ceiling is two mechanisms: `max_uses`
  written into the outgoing tool bounds *this* request (only on Anthropic's
  date-versioned tool family — OpenAI's `web_search` has no such field and a
  guess there is a 400), while the reservation makes search spend count so the
  limit engages for the *next* request. OpenAI's own search is deliberately
  unpriced: no documented usage field, and a rate that varies by
  `search_context_size`. Phases 2 and 3 — our own search backends, and a loop
  that executes them — are in
  [docs/web-search-plan.md](docs/web-search-plan.md), not started.
- **There are two search counts, and adding them together destroys a
  distinction.** `usage_records.search_count` is the **counterparty's**
  server-side search, billed per search as a surcharge (ADR 0058, the bullet
  above). `usage_records.own_search_requests` is **ours** — a call this gateway
  made to Exa, Jina, Staan or Linkup — and it is **counted, never priced**.
  Half the four vendors' rates cannot be read at source (Jina publishes no
  per-token figure publicly; Staan's dearer "for AI" tier is neither a request
  parameter nor reported back), so a rate table here would be a guess in half
  its rows; a count is never wrong and reconciles against a vendor dashboard
  with no currency and no rounding. Three things follow.
  `LimitMetric.OWN_SEARCH_REQUESTS` is a quota metric across the same scopes as
  cost, and it **defaults to zero** where `QuotaAmounts.requests` defaults to
  one — a default of one would put a search on every embedding and a search
  budget would become a request budget. The count is held on the **recorder**,
  not inside `TokenCounts`: that structure is what `compute_cost` multiplies by
  a rate, and its failure paths return a bare `TokenCounts()`, which would
  silently forgive searches a request had already spent. And a request ceiling
  bounds **volume, not spend** — Exa `deep-reasoning` is $15/1k against
  `instant` at $7, Linkup `deep` is 10x `flash` — which is why the quota form
  says so on the screen.
  `own_search_backend` and `own_search_tier` are label columns with no rate;
  they exist because they cannot be backfilled.
- **Restoring a placeholder moves every offset after it**
  (ADR 0059). A real name is rarely the
  same length as the placeholder that stood in for it, and a provider's
  citations are *character offsets into the answer* — OpenAI's `url_citation`
  is `start_index`/`end_index`. So `restore_with_edits` reports where it wrote,
  and `shift_citations` on each surface protocol moves what that surface holds.
  Three things worth knowing. `Shift` is **`(choice index, offset)`**, because
  one placeholder map edits different positions in each choice of an `n: 2`
  request. **Anthropic needs nothing moved** — its web-search citations carry
  `encrypted_index` and their own `cited_text`, and its document citations
  index the caller's document rather than the answer. And **the streamed case
  needs the whole answer**: a streamed citation's offsets are into the
  accumulated message, so the rewriter keeps the provider's text per choice and
  builds the map lazily, only for the frames that actually carry offsets. It
  cannot fire at all when nothing was substituted, which is why it was latent.
- **Redaction is ~90% of the CPU, and it scales with prompt length** — about
  0.1ms per prompt token, against a gateway cost that stays flat at 24-31ms.
  Capacity planning is redaction planning; see
  [docs/performance.md](docs/performance.md). Its detection cache is a
  *per-process* LRU, so adding workers lowers the hit rate.
- **Vendor quirks belong in a plugin, not in the accounting.** `gateway/plugins/`
  owns which header names a counterparty uses, what unit it reports cost in, and
  whether it is a provider or a router. The rule that keeps this safe is that a
  **plugin returns facts and never computes money** — there is deliberately
  nothing it can return that would let it price a request (ADR 0032).
- **The final ledger write happens after the response is sent**
  (ADR 0060). Six non-streamed
  routes attach `metered.completed_after_response(...)` to the response instead
  of awaiting it: −16% p50 and −33% p95 with headroom, CPU unchanged. Three
  things follow. A failure there is **logged, not raised** — the caller already
  has a 200 — so the row stays `in_progress` rather than becoming a 500. It is
  **worse under saturation** (p95 at concurrency 12 went 135ms → ~200ms),
  because awaiting the settle was accidental backpressure; that is accepted
  since the deployment is meant to stay below the knee. And **streaming is not
  included**: its `finally` needs `spawn_finalisation` and its own tests, which
  is still the open item below. Note the trap the tests guard: httpx's ASGI
  transport awaits background tasks, so through the client deferred and awaited
  look identical — `test_deferred_settlement.py` drives raw ASGI to assert the
  body goes out first.
- **A knowledge base pins its own embedding model, and that is the whole
  design** (ADR 0062). ADR 0020 said a fixed
  `vector(N)` column made changing the embedding model *a migration*; measured
  against pgvector 0.8.6 that is false — `vector` takes **no dimension
  modifier** and rows of differing length coexist in one column, and comparing
  two lengths **raises** rather than mis-ranking. So changing the deployment
  default is safe: existing bases keep answering from their own vectors until
  somebody reindexes. `dimensions` is *learned* from the first vector, never
  typed. Three more measured facts you cannot guess: HNSW refuses `vector`
  above **2000** dimensions and `halfvec` above **4000**, so the common
  3072-dimension model is only indexable through a halfvec cast; the planner
  does use a halfvec-cast index for a halfvec-cast `ORDER BY`; and ranking
  therefore happens in half precision while storage stays full.
- **There are two admin consoles, split by whose decision it is.** The
  gateway's own console owns providers, models, prices, quotas, redaction and
  users. The **chat's** admin console owns the knowledge pipeline — which
  embedding model, which extractor, the chunk geometry — because that is a
  product decision rather than an operational one (ADR 0062, corrected on
  request after being built the wrong way round first). The consequence to know
  before moving anything else: `/api` reads a **session cookie and nothing
  else**, so nothing a bearer-authenticated client needs can live there. The
  knowledge configuration is at `/v1/knowledge/config`, and **an API key is
  refused there even when its owner is an admin** — a key is what a program
  holds, an access token is evidence a person just signed in.
- **A pgvector type modifier cannot be a bind parameter.** `::halfvec(:dims)`
  fails with `type modifiers must be simple constants or identifiers` — a
  *syntax* error raised when the statement is prepared, so it is invisible to
  any test on another dialect. The first version of `PgVectorStore.search`
  bound it, passed all 24 SQLite tests, and answered 500 on the first real
  search. The width is formatted into `_SEARCH_SQL` instead (an `int()` in a
  checked range; every caller-influenced value stays bound), and
  `scripts/test_knowledge_live.py` is what stops it coming back. This is the
  sharpest example yet of the SQLite/PostgreSQL note above.
- **Indexing is billed, and it goes through `_metered` rather than beside it.**
  `_metered.begin` takes `fx` and `session_factory` instead of a `Request` for
  exactly this reason: ingestion runs in a detached task (ADR 0019 — OCR
  belongs nowhere near a request path) and a second private copy of the
  reserve → record → settle path would make indexing spend invisible to the
  reports built to catch it. A large ingestion run can cost more than the chat
  traffic it serves. Ingestion bills the **base's owner and group**, not
  whoever triggered it, so an admin pressing "reindex" does not move someone
  else's spend onto their own budget.
- **Redaction on a knowledge base is asymmetric, deliberately.** The text sent
  to the embedding provider is **redacted** (it is an egress, and deterministic
  placeholders are what let an indexed document and a later query still match —
  that is what the note in `embeddings.py` was for). The text **stored** in the
  chunk is what was extracted, unredacted: placeholders would be theatre while
  `file_blobs` holds the original document three tables away, and would hand a
  reader `<PERSON_…>` in place of a name in their own file. Protection stays at
  the boundary rather than being duplicated into the store.
- **`resource_shares` has no foreign keys, and cannot.** Both addresses are
  polymorphic — the resource is one of three kinds, one of which lives in the
  chat's MongoDB, and the principal is a user *or* a group. That is what gives
  sharing one implementation instead of two. The cost: deleting a shareable
  resource must delete its grants by hand, and `sharing.py` is the only place
  that does it. Read `may_reach` before touching it: the two halves of a
  principal must be joined with `and_`, and an `or_` there makes a resource
  readable by everybody the moment it is shared with anybody. **Fourteen tests
  passed against that bug** — every negative one failed closed for an unrelated
  reason — so reintroduce a fault and watch the test fail before believing it.
- **An MCP gateway was tried for connectors and rejected**
  (ADR 0063). ToolHive was built into the
  stack and taken back out, and the reason generalises past that one product:
  a gateway of that kind exists to *run* MCP servers — container isolation, a
  registry, policies — and our connectors are remote SaaS endpoints, so we
  would pay for the half we do not use. The specific blocker was that its
  per-user OAuth lives in the Kubernetes operator only: the compose path gives
  one credential per workload, a `localhost` redirect, and no token
  persistence without an OS keyring. The ADR carries the measurements, because
  the next person to reach for LiteLLM's MCP gateway or anything similar
  should read them first. Nothing of it remains in the tree.
- **Sourcing `deploy/.env` mangles `CHAT_OPENID_CONFIG`.** It holds JSON, and
  `set -a; . deploy/.env; set +a` strips the quotes, so the value in the
  environment no longer parses. A live script that needs a token should build
  it from `GATEWAY_OIDC__ISSUER` / `__CLIENT_ID` / `__CLIENT_SECRET`, which
  survive sourcing — every client `deploy/keycloak/setup.sh` creates has
  `directAccessGrantsEnabled`.
- **Do not run `ruff format` across this repository.** Verification is `ruff
  check`; the tree has never been `ruff format`-clean, and running it rewrapped
  27 unrelated files. Format only files you have just created.
- **There is no Node toolchain on the 130.192.84.103 host.** `pnpm -r test` and
  `tsc` run in a container: `docker run --rm -v <repo>:/w -w /w
  node:24-bookworm-slim`, with `CI=true` (pnpm will not purge a modules
  directory without a TTY) and `corepack enable`. Do **not** use `pnpm config
  set --location project` there: it writes the container's store path into
  `pnpm-workspace.yaml`, which is a committed file, and breaks every other
  install.
- **Per-request round trips are pinned by a test.** `test_query_counts.py`
  bounds them at 3 selects to authenticate and 5 + 2 writes for a metered
  request. `selectinload` on a many-to-one relation costs a round trip that
  `joinedload` does not; that is how the budget was set.
- **`InMemoryCounterStore` is atomic for an uninteresting reason** — it never
  awaits. It cannot prove anything about `MULTI`/`EXEC`, which is why
  `scripts/test_quota_race_live.py` exists.
- **There is no bundled identity provider** (ADR 0044).
  The console's default way in is local email + password (ADR 0043); OIDC is
  configured against whatever provider `deploy/.env` names
  ([docs/oidc-generic-provider.md](docs/oidc-generic-provider.md)). Consequences
  of the shape that remain true: the gateway reads OIDC discovery **once at
  startup**, so changing any `GATEWAY_OIDC__*` value needs a restart; and
  **`iss` is part of a user's identity** — users are keyed on
  `(issuer, subject)`, so changing the issuer re-provisions everyone as new
  rows with no memberships at their next login.
- **A request may choose which group pays, and a key may not**
  (ADR 0061). `x-bill-to: <group name>`,
  honoured only for a caller authenticated with an OIDC access token, because a
  key already carries its answer — a key sending it is **refused rather than
  ignored**, since a caller billed somewhere it did not ask for finds out from
  an invoice. It grants no capability: `resolve_billing_group` re-checks
  membership on every request anyway, so the header only reaches groups the
  caller could already bill by changing their default. Three things worth
  knowing. The lookup walks the user's **own memberships**, so a real group
  they do not hold is indistinguishable from one that does not exist —
  otherwise a billing header enumerates every group in the deployment. The
  options come from **effective memberships and never the token's `groups`
  claim**, which is the same trap ADR 0057 records: a hand-granted group is in
  no token, and `test_bill_to.py` has one test that fails only for a
  claim-reading implementation. And **sticky belongs to the client** — the
  gateway persists nothing, because a second stored preference would disagree
  with `default_billing_group` with no rule for which wins.
  `GET /v1/billing/groups` is what a client reads to build the choice, and it
  deliberately reports no spend or quota: it is the safe slice of the deferred
  `/v1` usage work, not a down payment on its shape.
- **A directory owns the memberships it granted, and no others**
  (ADR 0057). `memberships.source`
  is `oidc` or `manual`; a login's sync grants and revokes the first kind and
  never touches the second, so an administrator's group assignment survives a
  sign-in. `identity_providers.group_sync` says how often the directory gets to
  answer — `every_login` (the default, and what this always did), `first_login`,
  `never`. Three things worth knowing before touching it. The test is the
  **provenance of the membership**, not of the group: "whoever created the group
  owns it" was implemented first and killed by the bearer revocation test, since
  it would let a directory add people to an admin-created group and never remove
  them. `is_admin`, the default billing group and the sole-group rule now read
  the **effective** memberships rather than the token — all three were wrong for
  a manually-added user. And `_claims_diverge` asks "would a sync change
  anything" rather than comparing sets, because a user with one manual group is
  permanently unequal to their token and equality meant a write per `/v1`
  request.
- **A directory login can adopt the local account with the same address, and
  only if the operator says so** (ADR 0056).
  `identity_providers.link_local_by_email`, per provider and off by default. It
  reverses a refusal that used to be absolute, so read the ADR before touching
  either side; three things about it will save time. The gate is
  **`email_verified` boolean `true`** — absent and the string `"true"` are both
  declined, and every decline is logged with the value that caused it: inside a
  directory an administrator registered, an address is worth what that
  directory's word for it is worth, and an unchecked claim should not select an
  existing account. (Registering the directory is itself admin-only and needs
  its issuer, client id and secret; an earlier version of that ADR described
  the risk as if a stranger could add a provider, which is not true and is
  corrected there.) The link is a **`user_identities` row beside the identity, never a
  rewrite of it**: `issuer == "local"` is read in eight places as "this account
  has a password", and rewriting a linked user's issuer would flip all eight
  silently. And adoption hands that account to the directory — **memberships
  are replaced and `is_admin` follows `admin_groups`**, so a local admin
  adopted by a directory that does not place them in an admin group loses the
  flag on that login; the recovery is the local door, which is exactly what the
  shape keeps working. Linking never happens on `/v1`: an access token resolves
  an existing link but makes no userinfo request, so it does not hold the claim
  that would justify a new one.
- **Three things bite anything served behind the TLS proxy** (all found
  building it, all recorded in ADR 0035).
  **SNI may not carry an IP address**, so an address-only deployment offers no
  certificate at all until `default_sni` names one — every handshake fails with a
  TLS "internal error" and nothing above debug in the log. **uvicorn trusts
  forwarded headers from `127.0.0.1` only**, and the proxy arrives from the
  compose network, so without `FORWARDED_ALLOW_IPS` the app believes every
  request is http; the only place that shows is the post-logout URL built from
  `request.base_url`, which an https-registered provider then refuses with a
  400 after a login that worked. And **the session cookie is scoped to the
  origin the login happened on**, so the address you *sign in* on must be the
  address you keep using — `localhost` still serves `/v1` and the API either
  way, which is what the live scripts need. They follow `PUBLIC_HOST` when it
  is set, so source `deploy/.env` before running them.
- **An abandoned stream is billed from our price table, and the report blames
  the provider for it.** Found reconciling this deployment against Cortecs'
  dashboard on 2026-08-25. Their console said 34 requests / 244.3K tokens /
  €0.02; the ledger said 39 / 295,873 / €0.025510. Nothing was lost — the 34
  `upstream_exact` rows matched Cortecs exactly, to the token and to
  €0.019410. The other five were all `status=client_disconnected`,
  `upstream_status=200`, streamed: Cortecs served them for 2–15 seconds and the
  client hung up before the terminal SSE frame, so **no usage arrived and no
  reported cost did either**. Three consequences, none of them visible on the
  screen: tokens are counted locally (`usage_source=estimated`), cost falls back
  to our prices (`cost_source=own_prices_fallback`) on a provider configured to
  pass through, and `_reconciliation` — which requires `upstream_cost IS NOT
  NULL` on both sides, correctly — excludes them, so the drift row reads "34
  requests, ours equals theirs" beside a total covering 39. The one sentence
  that does mention them is wrong about why: `_disclosures` says *"the provider
  did not report usage"* when the row itself records that the client left. That
  wording is what sends an operator to the provider's dashboard to look for
  requests that were never missing.
- **The demo user's cap is EUR 1/hour and the fake upstream bills 1M tokens per
  request.** Running several live scripts back to back exhausts it legitimately;
  they report that as skipped. Flushing Valkey alone does not reset it — the
  counters rebuild from `usage_records`, so clear both.

## Verifying a change

```bash
uv run ruff check . && uv run mypy apps/gateway/src services
uv run pytest -q                       # gateway tests, SQLite
pnpm -r test                           # packages/ui + console
```

Then, for anything touching the request path, money, or SQL, against the real
stack:

```bash
docker compose --env-file deploy/.env \
  -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.smoke.yml \
  -f deploy/compose/docker-compose.redaction.yml up -d --build

# The live scripts sign in with local password auth (ADR 0043); set
# GATEWAY_LOCAL_ADMIN_PASSWORD (and GATEWAY_LOCAL_USER_* for the 403 checks)
# in deploy/.env, and create the accounts:
#   docker compose ... exec gateway gateway passwd admin@local
#   docker compose ... exec gateway gateway passwd --no-admin user@local

./scripts/test_reporting_live.py    # dialect-specific SQL the suite cannot reach
./scripts/test_redaction_live.py
./scripts/test_console_live.py      # console, CSP, pagination
./scripts/test_providers_live.py    # credentials encrypted in PostgreSQL, routing
./scripts/test_surfaces_live.py     # responses, anthropic messages, images
./scripts/test_quota_race_live.py   # admission under concurrency, real Valkey
./scripts/test_cache_accounting_live.py  # a real cache hit, and the ledger
./scripts/test_web_search_live.py    # per-search billing, the report and its CSV
./scripts/test_citations_live.py     # a citation still quotes its words after
                                    # redaction; needs an active redaction rule
./scripts/test_bill_to_live.py      # x-bill-to against a real OIDC token, and
                                    # that it refuses a group you do not hold
./scripts/test_knowledge_live.py    # the pgvector query, the real extractor and
                                    # the ledger — none of which SQLite can reach
./scripts/benchmark_live.py         # per-layer cost; see docs/performance.md
./scripts/test_public_tls_live.py   # only with the proxy overlay: TLS, the
                                    # rotated credentials, and that nothing else
                                    # is on a routable address
```

With the proxy overlay the live scripts need the deployment's own variables and
Caddy's CA, because they verify the certificate rather than skipping
verification:

```bash
docker compose ... exec proxy cat \
  /data/caddy/pki/authorities/local/root.crt > deploy/tls/caddy-root.crt
set -a; . deploy/.env; set +a       # PUBLIC_HOST, HTTPS_PORT, the admin password
./scripts/test_console_live.py
```

Sourcing `deploy/.env` is also what points them at the https origin: the
session cookie is scoped to the origin the login happened on, so sign in and
check on the same address.

**Run the live scripts.** More than half the serious bugs in this project's
history were only findable against the running stack: a counter seeded at zero,
a migration given the wrong environment, a 500 on `/v1/models` that every unit
test passed through.

The `130.192.84.52` host has 3 GB of RAM and 2 cores. The compose stack plus a
`pnpm test` will swap there, and the symptom is tests that fail having done
nothing wrong — check `free -g` before believing a frontend failure.
`130.192.84.103` has 14 GB and 5 cores and does not have this problem.

## Deployment

Development host `130.192.84.52`, console over an SSH tunnel — the README's
"Reaching the console from another machine" section has the command and why
both ports must match. A second VM, `130.192.84.103`, runs the same stack behind
the proxy overlay at <https://130.192.84.103:8443/console>; note that its
firewall permits **8443 and 22 and nothing else**, which is why that deployment
serves one origin with a single TLS port rather than two, and why the
Let's Encrypt configuration cannot be used there until 80 and 443 are opened.
Git remote is GitLab again (`viviani/pystino`, since 2026-09-03; `viviani/ai-stack`
was the original home, and GitHub `paoloviviani/Pistin-Gateway` held the origin
2026-08-29 → 2026-09-03); the tokens are in `.gitlab-token` and `.gh-token`
(legacy — the GitHub remote is gone), which may be sourced but should not be
read. Both are gitignored by name and glob.

## Where the project is

The gateway and its console are the substance and are done through Phase 2:
gateway, redaction, providers, quotas, reporting, five `/v1` surfaces, the admin
console. `the decision record records what was planned and what was added
afterwards, including the bugs each addition surfaced.

**The chat application lives on the `chat` branch**, not here: `apps/chat-api`
and `apps/web` were moved off main so this branch is the gateway only. The
branch carries its own plan (Phase 3, M1-foundation state), its ADRs (0015,
0016, 0041) and its compose overlay; ADR 0046 — the local door issuing `/v1`
credentials — is a *gateway* feature and stays here, as does ADR 0043.
Merge order when the chat resumes: gateway features land here first, the
`chat` branch rebases on them.

Two pieces of work with their reasoning written down rather than left to be
rediscovered — one being built, one not started:

- **ADR 0032** *(accepted; being built)* — providers and
  routers are different kinds, distinguished by whether the serving endpoint is
  implied by the model or chosen per request. Vendor knowledge moves into
  plugins, pricing with it, and the load-bearing rule is that **a plugin returns
  facts and never computes money** — `accounting/cost.py` stays the only code
  that multiplies a count by a rate. Cortecs is the router reference
  implementation. Note the measurement recorded there: Cortecs charges its
  listed price whichever sub-provider serves, so per-endpoint pricing buys
  attribution and drift detection rather than different rates. Billing has two
  configurable modes — our prices, or the counterparty's reported figure — and
  **both figures are recorded in both modes**, so a divergence is always
  reconstructable and a fallback is never silent.

  Built so far: the plugin protocol and registry (`gateway/plugins/`, in-tree
  plus the `llmp.providers` entry point), the generic, Anthropic and Cortecs
  plugins, `providers.plugin` / `providers.kind`, `upstream_provider` finally
  populated for routers, both billing modes with `computed_cost` / `cost_source`
  / `upstream_cost_details`, the console's provider-type selector, and the three
  reactive columns removed — `auth_scheme`, `forward_stream_options` and
  `upstream_cost_unit` are now `auth_headers` / `prepare_payload` and the
  plugin's own knowledge of its counterparty's unit. **Note what that costs:** a
  per-row knob became per-plugin behaviour, so two providers of the same type
  that need different answers now need two plugins. Migration 0010 translates
  `auth_scheme = x_api_key` into `plugin = anthropic` and prints a line naming
  every row whose behaviour changes. Still to come: `catalogue()` replacing
  `scripts/import_cortecs_pricing.py`.


- **[docs/redaction-scoping-plan.md](docs/redaction-scoping-plan.md)** —
  visibility is **done** (`GET /api/admin/redaction`, the Redaction screen), and
  so is **choosing the engine** (ADR 0033):
  the registry describes every installed engine, `redaction_config` is an
  append-only row that overrides `GATEWAY_REDACTION__ENGINE`, and
  `RedactionResolver` polls it every 10s so a change reaches the other worker
  without a restart and without a query on the request path. Switching to an
  engine that redacts nothing **no longer needs a written reason** — that
  requirement was dropped on request (ADR 0033, item 1, which now records why);
  the console still confirms and the row still names the engine, the admin and
  the moment. Note
  `_engine_redacts` asks the registry rather than comparing against `"noop"` — an
  installable engine could redact nothing under any name.
  **What is redacted is now an admin decision**
  (ADR 0037): a per-entity policy — four
  modes on two axes (what the model sees, what the reader gets back), a
  threshold per type, an allow-list — stored as JSON on that same append-only
  row and picked up by the same poll. Two things easy to get wrong: the policy
  is applied **before** overlap resolution, or a discarded `URL` span takes the
  `PERSON` it overlapped with it; and a reason is required only when a change
  protects *less*. The default protects everything the engine finds except
  `URL`, `DATE_TIME`, `LOCATION` and `NRP` — which is what fixed *"Riassumi le
  notizie del giorno da ilpost.it"* reaching the upstream as `<PERSON_…> le
  notizie del giorno da <URL_…>`, the English model calling the Italian verb a
  person at 0.85 and the news site a URL.
  Still to do: **scoping** — redaction is deployment-wide when it needs to be
  scopeable per model, provider, user or group — and **showing an operator which
  spans were replaced**, which is the feature that would have caught that bug in
  an afternoon. Read §4 and §5 of the plan first.
  The precedence rule to copy is the quota engine's, inverted — quotas
  are *all rules must pass*, redaction is *any applicable scope requiring it
  wins* — so adding a scope can only tighten. The doc carries the table shape,
  where it plugs into `_metered`, and why the first version has no exemptions.

Known open items, none of them blocking:

- **A stream settled at the moment the client disconnects loses the write.**
  Found on 2026-08-28 building the chat app, on the live stack. `body_iterator`
  in `routers/chat.py` sets `completed = True` and then, in its `finally`,
  awaits `metered.completed(...)` — a database write — *inside the request
  task*. A client that closes the connection the instant it reads the terminal
  `data: [DONE]` frame causes uvicorn to cancel that task mid-write: the
  connection is torn down with `CancelledError`, and the row stays
  `status=in_progress`, zero tokens, zero cost, for a request the provider
  served in full. Two rows in this deployment's ledger are exactly that.
  The disconnect branch of the same `finally` was already made
  cancellation-proof, with a comment explaining why — `spawn_finalisation`
  detaches the write into a task precisely because "we are very likely inside a
  cancelled task". The reasoning was never applied to the branch beside it.
  The chat service now drains the stream rather than breaking at `[DONE]`, which
  removes the common case, but any browser closing a tab at the wrong
  millisecond still reaches it. The fix is to route the `completed` branch
  through the same detached mechanism — and it needs a session that does not
  belong to the request scope, which is why it is not a two-line change and gets
  its own work with tests, per ground rule 3.

- **The estimated-usage disclosure attributes every case to the provider.**
  `_disclosures` in `reporting.py` cannot tell "the provider reported no usage"
  from "the client disconnected mid-stream", and says the former for both. The
  row knows: `status` is `client_disconnected`. Splitting the sentence by status
  — and saying that such requests are charged from our prices while the
  counterparty charged nothing — is the fix. See the trap above for the
  measurement it came from.

- **Pagination**: done. **Concurrency**: done and verified. Both were the last
  outstanding items from Phase 2.
- Cortecs **accepts** `stream_options` and reports usage with or without it, so
  `CortecsRouterPlugin.prepare_payload` adds nothing (checked against the live
  API). Still unverified is whether sending it narrows the routing pool — Cortecs
  does not name the serving provider in its stream frames, so
  `scripts/check_cortecs_stream_options.py` reports that part as unknown.
- Deferred by the user: per-provider default body params (`eu_native`,
  `allow_zero_data_retention`), image editing and variations, per-size image
  pricing, reranking.
- **The OCR surface is built and its console half is not.** `POST /v1/ocr`
  (ADR 0055) meters by the page, with two backends chosen by the model's
  provider: an upstream OCR model, or this deployment's own extractor
  (markitdown, in the redaction image with its NLP engine switched off, so a
  `.docx` or a text-layer PDF never leaves). Outstanding: there is no live
  script yet — the note that said the price form has no `per_page` field is
  **stale**, it has had one since the per-search work
  (`AdminModelDetail.tsx`) — and
  `usage_info.credits` — what Cortecs reports on an OCR *response* — is **not
  read**, because whether it is micro-EUR like their chat surface or something
  else needs one real call with a key, and a figure in an unverified unit is
  worse than none.
- **Cortecs' `/v1/models` defaults to `tag=Instruct`, and that hid two whole
  kinds of model.** An earlier note here claimed OCR models were absent from
  the catalogue and had to be entered by hand; they were never absent, only
  filtered out by a default the response does not mention. `tag=OCR` returns
  three, `tag=Embedding` eleven. Their prices are published too —
  `pricing.ocr_cost` is per **1,000** processed pages, not per million of
  anything, which is why `_kind_of` reads the tag and the parser divides by a
  thousand.
- **Usage reporting on `/v1` is deferred: passing the provider's `usage` object
  through is enough for now** (asked and answered 2026-09-04). What that leaves
  undone, so nobody re-derives it: there are no `x-ratelimit-*` headers (only
  `retry-after` on a quota refusal), no `/v1/usage` and no credits or balance
  endpoint — "balance" is not a concept here, since quotas are ceilings with
  counters rather than a prepaid sum. Every spend and quota figure is therefore
  reachable only under `/api` behind a **session cookie**
  (`get_management_user`), so a program holding a `gwk_` key cannot read its own
  usage and learns the ceiling by being refused. Two things to settle before
  building it, whenever it comes back: quotas are cost- *and* token-based across
  five scopes where all rules must pass, so "remaining" is the minimum over
  every applicable rule and cost has no standard header at all — a header that
  looks like OpenAI's and means something subtly different is worse than none;
  and OpenAI's costs response has no field for "our arithmetic versus the
  counterparty's", so a strictly-standard export flattens `cost_source` and
  `usage_source`, which is the distinction this gateway exists to keep.
