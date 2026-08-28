# 0039 — There is no deployment policy, only rules; the catch-all is one of them

- Date: 2026-08-28
- Status: **accepted, built**
- Requested as: "default policy should be no filter. and any scope should be one
  of the scopes in scoped rules. remove the 'redacted entity' card and replace it
  with the content of scoped rules page, adding a catch all scope."
- Supersedes the storage half of [0037](0037-redaction-policy.md); its per-entity
  policy model is unchanged and still the thing being stored.
- Related: [0038](0038-scoped-redaction.md) (the rules table and the fold),
  [0033](0033-redaction-engine-selection.md) (the append-only row this drops a
  column from).

## Context

ADR 0037 put a per-entity policy on `redaction_config` — one deployment-wide
policy — and ADR 0038 added `redaction_rules` for everything narrower. Two
storage locations for one kind of decision, and the console rendered them as two
cards that did not look related: a "Redacted entities" card at the top, and a
list of scoped rules elsewhere. An operator asked how to make a rule for one
group and could not find the answer on the screen that had it.

The two also disagreed about defaults. The deployment policy protected
everything the engine found except four types; a rule protected only what it
named. So the same JSON meant different things depending on which row held it.

## Decision

### 1. The catch-all is a rule with a null `scope_id`

`RedactionScope.ALL` joins `provider`, `model`, `group`, `user` and `api_key`.
Exactly one row may hold it — a `COALESCE` unique index, because SQL's unique
constraints do not consider two NULLs equal and the second catch-all would
otherwise be accepted and silently ignored. A CHECK constraint pins the other
half: `ALL` requires no subject and every other scope requires one.

`redaction_config.policy` is dropped. The append-only row keeps its other job —
choosing the engine — because that is genuinely deployment-wide and not
scopeable.

### 2. The default protects nothing

The shipped default is no rules at all, which redacts nothing, and
`RedactionPolicy.default_mode` is `off` so a policy is a statement about the
types it names and silent about the rest.

This reverses ADR 0037, which defaulted to protecting everything the engine
found except `URL`, `DATE_TIME`, `LOCATION` and `NRP`. The reversal is not a
retreat from that finding; it is where the finding leads. A default that
redacts is a default that is *wrong* for every deployment whose language,
domain or engine the default was not chosen against — and the bug ADR 0037
existed to fix was exactly that: an English model calling an Italian verb a
PERSON at 0.85 and a news site a URL, in a policy nobody had written. Refusing
to guess is the honest version of that lesson. A deployment that wants
protection writes one catch-all rule, and the screen says so.

The cost is stated plainly because it is real: an operator who installs this and
configures nothing gets no redaction. That is a visible absence — an empty rules
list on the Redaction screen — rather than an invisible presence, and a visible
absence is the failure mode that gets fixed.

### 3. The rules list is the screen

The Redaction screen shows the engine, then the rules, then the preview. The
per-rule editor is its own page, reached by clicking a rule. No card duplicates
another, and there is one place a policy can be written.

## Consequences

- One code path for every scope. `RedactionResolver` folds the catch-all first
  and then whatever else applies, and the fold from ADR 0038 is unchanged —
  adding a scope still only tightens.
- `PUT /api/admin/redaction/policy` is gone. Rules CRUD replaces it.
- Migration 0014 creates the catch-all scope and drops `redaction_config.policy`.
  Nothing translates the old deployment policy into a catch-all rule: this is a
  development deployment by the user's decision ("don't care about data and
  migrations"), and a silent translation would reintroduce a policy nobody wrote
  — the exact failure this ADR is about.
- Found while building: a header button still pointed at `/admin/redaction/rules`
  after the list moved onto the Redaction screen, so it navigated to the 404
  page. Moving a screen leaves the ways in behind.
