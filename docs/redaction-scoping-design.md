# Redaction: the entity catalogue, and scoping it per model, group and user

- Design brief, 2026-08-27. **No code has been written for any of this.** It
  exists so the decisions below are taken deliberately rather than discovered in
  a diff.
- Builds on [ADR 0037](adr/0037-redaction-policy.md) (four modes per entity type,
  thresholds, allow-list, stored on `redaction_config`) and on
  [redaction-scoping-plan.md](redaction-scoping-plan.md) §4, which is where the
  requirement was written down and which this supersedes in the parts marked.
- Facts here were established against the running deployment
  (`llm-platform-redaction-1`, Presidio 2.2.364, `en_core_web_lg`) and against
  the code at the line references given. Where something could not be settled
  from the code it is in **Open questions**, not guessed at.

## 1. The entity catalogue

What the deployment can actually detect, from `GET /healthz` on the live service
(`services/redaction/src/llmp_redaction/app.py:190`, which reports
`analyzer.get_supported_entities()` rather than a constant — see the bug in
`detector.py:112`). Thirty-two types. The **Source** column was determined by
enumerating the registry in the running container and reading each recogniser's
`supported_entities`; the pattern recognisers are Presidio's predefined ones plus
the nine registered by `EXTRA_PATTERN_RECOGNIZERS` (`app.py:60`), and the NER
column is `SpacyRecognizer`, whose `ner_strength` is **0.85** — which is exactly
the score the Italian verb *Riassumi* was given as a `PERSON`.

| Entity | Source | On by default |
|---|---|---|
| `CREDIT_CARD` | pattern + Luhn | yes |
| `CRYPTO` | pattern + checksum | yes |
| `EMAIL_ADDRESS` | pattern | yes |
| `IBAN_CODE` | pattern + checksum | yes |
| `IP_ADDRESS` | pattern | yes |
| `MAC_ADDRESS` | pattern | yes |
| `MEDICAL_LICENSE` | pattern + checksum | yes |
| `PHONE_NUMBER` | pattern (`phonenumbers`, regions `IT,US,GB,DE,FR,ES,CH,AT` — `app.py:78`) | yes |
| `UK_NHS`, `UK_NINO` | pattern + checksum | yes |
| `US_SSN`, `US_ITIN`, `US_PASSPORT`, `US_BANK_NUMBER`, `US_DRIVER_LICENSE` | pattern | yes |
| `IT_FISCAL_CODE`, `IT_VAT_CODE`, `IT_DRIVER_LICENSE`, `IT_IDENTITY_CARD`, `IT_PASSPORT` | pattern + checksum | yes |
| `ES_NIF`, `ES_NIE`, `PL_PESEL` | pattern + checksum | yes |
| `URL` | pattern | **no** (ADR 0037) |
| `DATE_TIME` | pattern **and** NER (`DATE`/`TIME`) | **no** (ADR 0037) |
| `PERSON` | **NER** (0.85 fixed) | yes |
| `ORGANIZATION` | **NER** | yes |
| `LOCATION` | **NER** (`GPE`/`LOC`) | **no** (ADR 0037) |
| `NRP` | **NER** (`NORP`) | **no** (ADR 0037) |
| `AGE`, `EMAIL`, `ID` | **advertised, unproducible** | yes, vacuously |

Three things this table says that the console does not:

- **`AGE`, `EMAIL` and `ID` cannot fire on this deployment.** They are in
  `get_supported_entities()` because Presidio's `NerModelConfiguration`
  `model_to_presidio_entity_mapping` contains `AGE→AGE`, `EMAIL→EMAIL`,
  `ID→ID` — mappings for transformer and clinical models. `en_core_web_lg` emits
  only `CARDINAL DATE EVENT FAC GPE LANGUAGE LAW LOC MONEY NORP ORDINAL ORG
  PERCENT PERSON PRODUCT QUANTITY TIME WORK_OF_ART`. Three checkboxes on the
  screen therefore do nothing, and nothing says so.
- **The repo's own classification is stale.** `PATTERN_ONLY_ENTITIES` in
  `services/redaction/src/llmp_redaction/detector.py:32` omits `ES_NIE`,
  `UK_NINO` and `MAC_ADDRESS` — all three registered and live — while listing
  `AU_*`, `IN_*` and `SG_NRIC_FIN`, which this deployment does not register. It
  is used by one test (`test_detector.py:228`). If the screen is going to label
  patterns, that constant is the wrong source: ask the service, the same rule
  `capabilities()` already follows.
- **"Adds latency" cannot be a per-checkbox label.** `AnalyzerEngine.analyze`
  runs `nlp_engine.process_text` unconditionally, *before* recogniser selection —
  the `entities=` filter picks recognisers, it does not skip spaCy. Measured on
  the live service, one 2.1 KB text, median of five, cache defeated:

  | asked for | p50 |
  |---|---|
  | everything | 114 ms |
  | `PERSON` only | 64 ms |
  | `EMAIL_ADDRESS` only | 69 ms |

  Turning `PERSON` off saves nothing on the NER pass — asking for *only* a
  pattern type still costs 69 ms, which is the pass. Narrowing halves the total
  by removing recogniser and context-enhancement work, not by removing the model.
  So the honest label is on the deployment ("this deployment runs an NER model:
  ~65 ms floor per 2 KB of prompt"), and the per-type label is "pattern" or
  "model", not a latency claim. This corrects ADR 0037 §Consequences, which says
  turning `URL` off means "the detector does less work as well" — true, and a
  smaller effect than it sounds.

## 2. `redaction_rules`

`limit_rules` (`apps/gateway/src/gateway/models.py:687`) is the shape to mirror,
because an operator already understands it and the console already renders it:
`scope` + `scope_id`, `is_active`, an expression-unique index that `COALESCE`s
the nullable columns to sentinels, and a `CHECK` tying `scope_id` presence to
`scope`.

| column | why, and where it differs from `limit_rules` |
|---|---|
| `id`, `name` | as `limit_rules`. |
| `scope` | new enum `RedactionScope`: `provider` / `model` / `group` / `user`. **Not** `LimitScope`: quotas need `api_key`, redaction needs the two axes that are trust boundaries — the endpoint the text reaches and the person who wrote it. An `api_key` scope is omitted from v1 because a key is not a trust boundary for personal data; the person and the endpoint are. |
| `scope_id` | as `limit_rules`: not a real FK, it points at one of four tables. |
| `policy` | **JSON, a whole `RedactionPolicy`** — not the `require_redaction` boolean the plan proposed. ADR 0037 replaced the on/off switch with a per-type document, and a boolean can no longer say what a rule means. Same shape as `redaction_config.policy`, so one editor, one validator, one combiner. |
| `is_active`, `created_by`, `created_at`, `updated_at` | as `limit_rules`, plus `created_by` because this is a decision about personal data. |
| `reason` | optional, kept for the record. Not required: every scoped rule can only tighten (§3), and ADR 0033's argument holds — a prompt with no reader trains people to type "x". |

No `metric`, `window_seconds`, `period` or `resets`: redaction has no time axis.

**There is no `global` scope in this table.** `redaction_config` already *is* the
global scope — append-only, with the reason and the author, polled by the
resolver. Adding a second home for the same value is how a screen ends up lying
about which one is in force. The fold in §3 therefore always starts from
`resolver.policy`, which preserves the property that matters: **no rules means
behave exactly as today.**

Unique index, matching `uq_limit_rules_identity`'s trick because NULLs are
distinct in SQL:

    unique (scope, coalesce(scope_id, '000...0')) where is_active

so a duplicate is a 409 rather than two rules that disagree.

`usage_records` gains `redaction_scope` and `redaction_rule_id` beside the
existing `redaction_engine` / `redacted_entity_count` (`models.py:673`). Without
them, "why was this request redacted" is unanswerable six weeks later, which is
precisely the question a data-protection review asks.

## 3. Combining an applicable set

The applicable set for one request is: the deployment policy, plus at most one
active rule for each of the model, its provider, the billing group, and the user.
`EntityMode` is ordered weakest to strongest already
(`apps/gateway/src/gateway/config.py:98`), and the whole algorithm is that
ordering:

- **mode** for type *t*: `max(rank(P.mode_for(t)) for P in applicable)`.
  `mode_for` already falls back to each policy's own `default_mode`, so a rule
  that names only `IBAN_CODE` contributes its default for everything else and can
  never lower another scope's answer. `max` is the whole safety property: adding
  a scope cannot weaken.
- **default_mode**: `max` of the defaults, by the same rank.
- **threshold** for *t*: `min` over the policies where *t* is not `off`. A lower
  threshold means more spans caught, so *min* is the strict direction. Prevents:
  a group rule setting `PERSON` to 0.95 hiding names the deployment wanted at
  0.5, while looking like it was tightening.
- **allow-list**: **not a per-scope field in v1.** It is the one field that
  weakens, and neither combination rule is safe. Union is a hole — a user
  exempting `acme.com` overrides the admin. Intersection is worse in a way that
  is invisible: the moment any scoped rule exists with an empty list, the
  deployment's own exemptions vanish and the failure looks like over-redaction
  with no cause on screen. So the allow-list stays where ADR 0037 put it, on
  `redaction_config`, admin-only. If it is ever wanted per scope, the shape is
  `null` = "says nothing" and intersection over the non-null lists, with user
  rules forbidden from carrying one.

Enforcement is therefore **by construction**, not by validation: even a rule
saved wrong cannot weaken anything, because `max`/`min` are the only combiners.

## 4. "Only more restrictive", as an invariant

A user-scoped rule *U* is accepted only if, against the admin-effective policy
*A* (deployment ∘ provider ∘ model ∘ group, folded as above):

> for every entity type *t* in `named(U) ∪ named(A) ∪ service.entities`:
> `rank(U.mode_for(t)) >= rank(A.mode_for(t))` and
> `U.threshold_for(t) <= A.threshold_for(t)`; and `U.allow_list` is empty.

`RedactionPolicy.weakens` (`config.py:203`) is nearly this predicate and is
already tested and in use by `PUT /api/admin/redaction/policy`
(`routers/admin.py:2183`). Two gaps to close before reusing it as the invariant,
both of which pass silently today:

1. it compares `default_mode` only in the `not-off → off` direction, so a user
   whose default is `anonymise_restore` against an admin default of `redact` is
   not flagged; and
2. it iterates `set(self.entities) | set(previous.entities)` — the types
   *nobody named* are exactly where a default downgrade hides. Folding in the
   service's reported entity list closes it.

The validator exists for the **error message**, not for safety: §3 already makes
a bad rule inert. Say so where it is written, because "accepted and silently
ignored" reads as the feature not working.

Constraint on the *surface*, not just the value: a user may write a rule only for
`scope = user, scope_id = their own id`. `PUT /api/me/default-billing-group`
(`routers/me.py:93`) is the precedent for the check — it refuses a group the
caller is not a member of — and `routers/me.py` is where a
`PUT /api/me/redaction` belongs. Note that `/api/me` authenticates from the OIDC
session cookie (`deps.py:231`), so this is a browser-only surface: an API key
cannot set its owner's policy, which is the right default for a control that can
only tighten.

## 5. The request-path cost

Redaction runs in the route at `routers/chat.py:180`, *after*
`_metered.resolve_model` and *before* `_metered.begin` — so at the moment the
policy is needed, the request already holds, with no further queries:
`model.id`, `model.provider_id` and the loaded `model.provider`
(`joinedload`, `access.py:106`), `principal.billing_group.id` and
`principal.user.id` (`deps.py:39`). **Every scope a rule can name is already in
hand.** The budget to respect is `apps/gateway/tests/test_query_counts.py:97`:
5 selects and 2 writes for a metered request.

**Option A — fold the rules into the resolver's poll. Zero queries.**
`RedactionResolver` already reads `redaction_config` every 10 s off the request
path (`redaction/resolver.py:157`) precisely so `get_redactor` stays one
attribute read. `redaction_rules` will hold single digits of rows; load all of
them in the same poll, hold them in a dict per scope, and fold in Python per
request (four dict lookups and a `max` over ~30 types — microseconds against a
65 ms detection floor). Staleness is bounded by the same interval an admin
already sees as `propagation_seconds`. Cost: the table is now unbounded in
memory, and a deployment that grows to thousands of per-user rules is a silently
growing per-worker footprint. Bound it and log when the bound is hit.

**Option B — one indexed query in the admission path.** Exactly
`QuotaEngine.load_rules` (`quota/engine.py:203`): one `OR` over the four scopes
against `ix_redaction_rules_lookup`. Correct and immediate, budget goes 5 → 6
selects, and `test_query_counts.py` has to be raised — which is the point of the
test. On the measurements in [performance.md](performance.md) one round trip is
~0.5 ms against a request whose detection is 85–95% of the cost, so this is
affordable; it is a decision, not a regression.

There is no free third option. Folding user or group rules into the principal
load, or model rules into `accessible_model_by_name`, costs a `selectinload`
round trip either way — which is how the current budget was set in the first
place.

Recommendation: **A**, with B as the fallback if rules ever need to take effect
instantly. A also keeps the pattern the codebase already argues for twice
(resolver docstring, `quota/engine.py:205`).

## 6. What varies per request, and what must not

`app.state.redactor` is a single shared `HttpDetectionRedactor`
(`deps.py:78`, `main.py:144`), holding an httpx pool and a 2048-entry detection
LRU. That cache is the difference between 13 ms and 135 ms on a 1 000-token
prompt (performance.md).

**The redactor must stay one object; the policy travels per call.** Constructing
a redactor per scope per request gives every request a cold LRU *and* a new
connection pool — the failure the resolver's docstring already names for a
10-second rebuild, arriving once per request instead. So the change is a
signature change: `redact_request(messages, *, policy=None)` on the `Redactor`
protocol (`redaction/base.py:52`), with `apply_spans` already taking a `policy`
argument (`redaction/http.py:127`) and the noop ignoring it.

What that does to the cache: `_Cache.key` is
`sha256(language, threshold-floor, sorted-types, text)`
(`redaction/http.py:80`), so per-scope policies are **already keyed correctly** —
there is no correctness bug, only fragmentation. The same conversation seen under
*N* distinct effective policies occupies up to *N* × the entries. Two mitigations
worth knowing: `detected_types()` returns `None` for any policy whose default is
not `off` (`config.py:187`), so most policies share that key part and the real
fragmentation driver is the **threshold floor** — a handful of distinct values,
not a per-user explosion; and `cache_size` should be scaled by the number of
distinct effective policies, which the resolver can count.

`restore_in_response` stays global (`http.py:355`): it is a correctness property
of the conversation, not a policy one, and the plan already reached that
conclusion.

## 7. Block, and where it plugs in

Make it a **fifth `EntityMode`, `block`, ordered last** — strongest of all. It
then composes with §3 for free: `max` keeps "adding a scope can only tighten"
true, the console stays one dropdown per pattern, and no second axis has to be
combined.

The refusal shape already exists. `RedactionUnavailableError`
(`redaction/http.py:44`) is a `GatewayError` with `status_code`, `error_type` and
`code`, rendered by `gateway_error_handler` into OpenAI's `{"error": {...}}`
envelope (`errors.py:100`) — raised from inside the redactor, which is exactly
where a block is decided. A `ContentBlockedError` with `status_code = 403`,
`error_type = "invalid_request_error"`, `code = "content_blocked"` is the whole
mechanism. Two rules for its message: it names the entity type and the scope that
blocked, and it **never quotes the matched text** — echoing the value into an
error body and a log is the failure this feature exists to prevent.

Why raise rather than return, when a quota refusal returns a `JSONResponse`
value (`_metered.py:246`): the quota check is inside `_metered.begin`, one
function, so a value is cheap. `redact_request` is called from five routes, and
threading a refusal value through all five is how one of them forgets.

What it costs, and it is not nothing: `redact_request` runs *before*
`_metered.begin`, so at block time **there is no reservation and no usage row**.
A blocked request is therefore invisible in every report — the ledger will not
show that the deployment refused 400 prompts last month. Recording it means
either opening the usage row before redaction (which reorders the path that
`_metered`'s docstring exists to protect) or a separate audit table. Neither is
free; see the open questions.

## 8. Custom regex patterns

The detection contract carries `texts`, `language`, `score_threshold`,
`entity_types` and nothing else (`packages/shared-py/src/llmp_shared/redaction.py:165`).
Two ways to add user-defined patterns:

- **Extend the contract.** Every engine must then implement it, and a
  catastrophically backtracking regex stalls a worker thread in the shared
  detection service.
- **Run them in the gateway**, producing `EntitySpan`s locally and merging them
  with the service's *before* overlap resolution — the ordering ADR 0037 §5 makes
  load-bearing. No contract change, works for any engine, and because regexes are
  cheap and deterministic they can be applied **outside** the detection cache, so
  they add no key fragmentation at all.

The second, then. The risk it moves rather than removes: Python's `re` has no
timeout, so an admin-supplied pattern can hang the gateway's event loop. That is
a licensing-adjacent decision (`google-re2` is BSD-3 but is a new dependency) and
belongs in the open questions rather than in this brief.

## 9. The preview box

An admin-only `POST /api/admin/redaction/preview` taking a sample text and
optionally a scope subject, returning the spans *and* the rewritten text. It must
not be a proxy to `/detect`: the value is showing what the **model would
receive**, which means running the effective policy and `apply_spans` — filtering
before overlap resolution, the modes, the thresholds and the allow-list — over a
throwaway `PlaceholderMap`.

This is also, finally, item 4 of ADR 0037's "not done": nothing today shows an
operator which spans were replaced. One caution to write next to it: the sample
box will contain real personal data within a week of shipping, so the request
body must not be logged.

## Open questions

1. Should `redaction_rules` be append-only like `redaction_config`, or mutable
   with `is_active` like `limit_rules`? Mutable loses "who weakened the group's
   policy in March", which is the question a review asks.
2. Do we record a blocked request in `usage_records` (needs the row opened before
   redaction, reordering `_metered`), in a separate audit table, or nowhere?
3. Is `google-re2` (BSD-3, new dependency) acceptable for user-supplied patterns,
   or do we bound them with a length limit, a compile-time check and a thread
   deadline instead?
4. Should a user's own rule apply to requests made with their **API keys**, or
   only to the console/chat app? (It applies to the user id, so today's answer
   would be "both".)
5. Per-scope `language` — the other half of the original Italian bug — in this
   table, or a separate decision? It is per-process today and silently wrong for
   half the prompts here.
6. `api_key` as a fifth scope: genuinely useful for "this CI key needs stricter
   handling", or a scope nobody will set?
7. Should the three unproducible types (`AGE`, `EMAIL`, `ID`) be hidden from the
   console, or shown greyed with "this model cannot produce it"?
