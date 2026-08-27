# 0038 — Redaction scoped per provider, model, group, user and key

- Date: 2026-08-27
- Status: **accepted, built**
- Requested as: "we need to configure all the possible entities […] and we need
  to configure it by model, by group and by user. And also users can configure,
  although only more restrictively."
- Related: [0037](0037-redaction-policy.md) (the per-entity policy this scopes),
  [0033](0033-redaction-engine-selection.md) (the append-only row it reuses),
  [0009](0009-quota-model.md) (the scoping shape it copies, and inverts),
  [redaction-scoping-design.md](../redaction-scoping-design.md) (the research and
  the measurements).

## Decision

### 1. `redaction_rules`, shaped like `limit_rules`

Scope, `scope_id`, `is_active`, an expression-unique index — an operator already
understands that table and the console already renders its shape. Three
differences, each for a reason:

- it carries a whole `RedactionPolicy` as JSON, not a boolean, because ADR 0037
  made "redact or not" the wrong question;
- there is no time axis, so no metric, window or period;
- **there is no `global` scope**, because `redaction_config` already is one. A
  second home for one value is how a screen ends up disagreeing with itself
  about which is in force.

Scopes are `provider`, `model`, `group`, `user` and `api_key`. Not `LimitScope`:
quotas scope to who pays, redaction scopes to **where the text goes** and **who
wrote it**, which is why `provider` and `model` appear here and nowhere in the
quota model.

### 2. The fold is the safety property, and it is not a validation rule

Applicable policies combine as: `max` over the mode ranking, `min` over
thresholds (a lower threshold catches more), union over custom patterns (one more
regex only finds more), and the allow-list taken from the deployment alone.

So **adding a scope can only tighten, by construction**. A rule saved wrong is
inert rather than dangerous — the difference between a bug and an incident — and
that is why nothing here needs a priority column or a "most specific wins" rule.
ADR 0009 records the same reasoning inverted: quotas are *all rules must pass*
because a permissive quota rule would raise a ceiling; redaction is *the
strictest answer wins* because a permissive redaction rule would remove
protection.

**The allow-list is the exception that proves it.** It is the one field that
weakens, so it is not per-scope at all: union would let a user exempt what an
admin redacts, and intersection is worse in a way nobody would see — the
deployment's own exemptions would vanish the moment any scoped rule carried an
empty list, and the symptom is over-redaction with nothing on screen to explain
it.

**A silent policy is silent.** Writing the tests found this: a policy that does
not *name* a type was voting for its own `default_mode`, so a group rule about
`IBAN_CODE` alone switched `URL` redaction back on — reintroducing ADR 0037's bug
by a new route. A policy now contributes only where it names a type, and its
default applies only to types nobody names.

### 3. Resolution costs no queries

Every subject a rule can name is already loaded when redaction runs: the model,
its provider (joined), the billing group, the person, the key. The rules
themselves arrive on the resolver's existing ten-second poll, so `policy_for` is
five dictionary lookups and a fold. A lookup in the request path would have been
a sixth `SELECT` and would have broken the budget `test_query_counts.py` pins.

Rules reload as a **dictionary swap, never a redactor rebuild**. The redactor owns
the connection pool and the detection LRU that is the difference between 13 ms
and 135 ms on a 1,000-token prompt, and rebuilding it on every rule edit would
quietly destroy the hit rate — the failure the resolver's own docstring already
names for engine changes.

### 4. "Only more restrictive" is measured against what an admin imposed

A user may set their own policy through `PUT /api/me/redaction`, which is
session-authenticated: an API key cannot set its owner's policy. The invariant is
that their policy may not weaken the admin-effective floor.

One subtlety cost a test to find, and it is the sort that only shows up when the
console makes such rules easy to write: there is **one row per subject**, so an
administrator's `scope=user` rule and that person's own policy are *the same
row*. "Their own" cannot be decided by scope — only by who wrote it. Without
that distinction, an administrator's rule about one person is the one rule that
person can overrule, which is precisely backwards. The floor therefore excludes a
user-scoped rule only when that person authored it.

The invariant is a courtesy, not the enforcement: §2 already makes a weaker rule
inert. It exists so the refusal names the entity type that failed, because
"accepted and silently ignored" reads as the feature not working.

### 5. Block is a fifth mode, not a second axis

Ordered last, so it composes with the fold for free: one dropdown per entity, and
`max` keeps "adding a scope can only tighten" true. `ContentBlockedError` is 403 —
the request is well-formed and the caller authenticated; what is refused is the
*content*. Its message names the entity type and the scope and **never quotes the
matched text**: an error body is logged and pasted into tickets, and echoing a
blocked credential there would leak exactly what the block exists to contain.

A blocked request writes a `usage_records` row with `status=blocked`, zero tokens
and zero cost. Nothing was billed, but "this deployment refused 400 prompts last
month" is a number a data-protection review asks for, and a 403 that leaves no
trace cannot produce it.

### 6. Custom patterns run in the gateway, under RE2

Operator-written regexes are one more entity type: same modes, same placeholders,
same entity count. They are evaluated in the gateway rather than sent to the
detection service — no contract change, works for any engine, and one
deployment's regexes have no business being installed into a service every
deployment shares. They stay outside the detection cache, since a regex is cheap
and deterministic and caching it would fragment a key whose hit rate is worth 10x
on a long conversation.

**RE2 rather than `re`, and the reason is measured.**
`re.search(r"(a+)+$", "a"*26 + "!")` takes **10.8 seconds** on Python's engine,
which has no timeout — one pattern of that shape, written by accident, hangs a
worker. Under RE2 the same call is microseconds, because it cannot backtrack. The
hang is structurally impossible rather than merely unlikely. `google-re2` is
BSD-3 (checked at source, ground rule 1) and ships a `cp314` manylinux wheel, so
it adds no build step. What it costs: no backreferences and no lookaround, and a
pattern using them is refused at save time with RE2's own message, which names
the construct.

### 7. Provenance goes on the request

`usage_records` gains `redaction_scope` and `redaction_rule_id`. The rules table
is **mutable** — an operator edits a rule, they do not append a new one — so the
trail that answers "why was this request redacted" has to live on the request.
A request under several rules records the **narrowest**, which is the one somebody
set deliberately for that subject; the others are visible on the rules screen.
That is a documented limitation rather than a complete trail, and it is stated
here so nobody later reads the column as exhaustive.

## What this does not do

- **Per-scope `language`**, which is the other half of the original Italian
  failure. Still a per-process constant.
- **Rule history.** Rules are mutable, so "who weakened the group's policy in
  March" is answerable only through the requests that ran under it.
- **A second opinion on user-authored patterns.** A user's regex is theirs; it
  can only add detections, but nothing reviews it.
