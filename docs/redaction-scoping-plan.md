# Redaction: making it visible, and making it scoped

- Requested 2026-08-17. **Not started.** This is the requirement, recorded
  before it is designed, so the design is not reverse-engineered from a diff.
- Related: [ADR 0012](adr/0012-redaction-interface.md) (the detection contract),
  [ADR 0026](adr/0026-pluggable-detection.md) (the `llmp.redactors` entry point),
  [ADR 0009](adr/0009-quota-model.md) (the precedence rule to copy).

## 1. The console cannot see the redaction layer at all

Today redaction is **process-global configuration**: `GATEWAY_REDACTION__ENGINE`,
`__ENDPOINT`, `__FAIL_OPEN` and the rest, read at startup into
`RedactionSettings`. There is no API surface and no screen, so an operator using
the console cannot answer:

- is redaction on at all, or is `engine` still `noop`?
- which engine — and is it the Presidio service or something else?
- is that service actually reachable *now*, or is every request quietly
  fail-closing with a 502 (or worse, fail-opening if someone set
  `fail_open=true`)?
- what is it configured to look for — `entity_types`, `score_threshold`,
  `language`?
- is `restore_in_response` on, i.e. do callers get real values back?

The per-request evidence exists — `usage_records.redaction_engine` and
`.redacted_entity_count` — but nothing aggregates or exposes it.

**Minimum worth building:** a read-only panel. Engine name, endpoint (host only,
never a credential), reachability with latency from a real call to the service's
health endpoint, the detection settings in force, the fail-open/fail-closed
posture flagged as a warning when open, and a count of entities redacted over a
recent window from `usage_records`. That last number is the one that tells an
operator it is genuinely working rather than configured.

The provider screen's connection test is the model to copy: run it against the
row as stored, report what came back.

## 2. Admin-configurable engines

Only **Presidio** exists today, behind the `http` engine contract, and the
contract is deliberately generic — anything that speaks the detection API in
`llmp_shared.redaction` works, and third parties register through the
`llmp.redactors` entry point (ADR 0026). So "which do we support beyond
Presidio" is, honestly: none yet, and the plugin seam is already there for when
there is one.

That makes the useful first step narrow — let an admin point the `http` engine
at a **different endpoint** and change the detection parameters from the
console, rather than invent a provider-registry for engines that do not exist.
An engine dropdown listing exactly one option is not worth a migration; an
editable endpoint plus a test button is.

Two things this must not do:

- **Never let the console turn redaction off silently.** Moving from a working
  engine to `noop` is the one change that makes the system quietly stop
  protecting anything, which is the same argument `fail_open=false` already
  makes. It needs to be loud, and probably needs to be a deployment-level
  decision rather than a console toggle.
- **Never accept an endpoint without testing it.** A saved endpoint that does
  not answer means every subsequent request fails closed. Test-before-save, as
  providers do.

Configuration currently lives in environment variables, so anything editable
needs a database row that overrides them, and a clear precedence between the
two. Worth an ADR of its own — this is the same "environment upstream became a
provider row" migration shape as `providers`.

## 3. Where redaction applies — the actual request

It is all-or-nothing today. Wanted, in the requester's words:

- **always on for any call** — admin-imposed, cannot be overridden;
- **per model** — some models are hosted somewhere that must never see
  personal data, others are local and may;
- **per provider** — the more natural axis for the same concern, since the
  trust boundary is the endpoint, not the model;
- **per user or group** — a research group handling clinical data redacts, an
  engineering group testing prompts does not.

### The precedence rule to use

Copy the quota engine, which already solved this shape: *all matching rules must
pass, so adding one can only tighten a budget.* The redaction equivalent:

> Redaction applies if **any** applicable scope requires it. Adding a scope can
> only ever increase what is redacted, never decrease it.

That makes the global "always on" switch simply the broadest scope rather than a
special case, and it makes every other scope safe to add — because no
combination of them can produce *less* redaction than before. The alternative,
most-specific-wins, means a per-model exemption can silently switch off a
group-level requirement, and the failure is invisible: prompts flow unredacted
and nothing errors.

An explicit exemption (per model, say, for a local endpoint) is then a
deliberate, separate, auditable thing — not a side effect of ordering.

### Things that will need deciding

- Which scopes compose, and where the row lives — one `redaction_rules` table
  scoped like `limit_rules` (`scope` + `scope_id`) is the obvious parallel, and
  reuses a pattern the console already renders.
- Whether a rule can also *narrow* `entity_types` per scope, or only turn
  redaction on. Narrowing per scope reintroduces the "can this make it weaker"
  question and should probably wait.
- `restore_in_response` per scope, or global. It is a correctness property of
  the conversation, not a policy one, so probably global.
- What `usage_records` should record so a past request can be explained: it
  already stores the engine and the entity count, but not *which rule* caused
  redaction to apply. Without that, "why was this redacted" is unanswerable
  after the fact.

## Sequencing

Visibility first — it is read-only, needs no migration, and it is the thing
currently missing that makes the rest hard to trust. Then scoping, which needs a
table, a precedence rule with tests, and admission-path changes. Configurable
endpoints last, since only one implementation exists to point at.
