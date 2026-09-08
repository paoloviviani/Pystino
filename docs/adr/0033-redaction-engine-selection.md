# 0033 — The redaction engine becomes an admin decision, on the record

- Date: 2026-08-24
- Status: **accepted, built**
- Requested as: "list the redaction engines available at config time (now only
  presidio) and let the admin enable and disable them from the UI."
- Related: [0012](0012-redaction-interface.md) (the detection contract),
  [0026](0026-pluggable-detection.md) (the `llmp.redactors` entry point),
  [0032](0032-provider-plugins.md) (the same registry-drives-the-UI pattern, for
  providers), [redaction-scoping-plan.md](../redaction-scoping-plan.md) (step 2,
  still to come).

## Context

Redaction was process configuration read once at startup:
`GATEWAY_REDACTION__ENGINE` plus a handful of parameters. Since ADR 0026 the
engine name resolves through a registry with an entry point, so a deployment can
install one — but nothing in the console said which engines were installed, and
changing engines meant editing the environment and restarting.

## The concern this decision overrides, stated first

[redaction-scoping-plan.md](../redaction-scoping-plan.md) §3 says, about exactly
this feature:

> **Never let the console turn redaction off silently.** Moving from a working
> engine to `noop` is the one change that makes the system quietly stop
> protecting anything […] It needs to be loud, and probably needs to be a
> deployment-level decision rather than a console toggle.

That reasoning still holds and it was raised before building this. The decision
is to build the toggle anyway, and to spend the design effort on the word
**silently** rather than on refusing. Concretely, four things make it loud:

1. ~~**A written reason is required** to move to an engine that redacts
   nothing, and only for that direction.~~ **Dropped on 2026-09-08**, on
   request. A reason is still accepted and still kept when one is given; it is
   no longer demanded. The argument for dropping it beat the argument that put
   it here, and by this ADR's own reasoning: it worried that a prompt with no
   reader "trains people to type 'x'" — and then required exactly such a prompt
   on the one path an operator reaches while handling an incident. What a later
   review can actually rely on is items 2 to 4, which are unchanged: an
   append-only row naming the engine, the admin and the moment; a screen that
   says so in `danger` tone; and a WARNING in the log. The console still stops
   and confirms, it just no longer asks for a sentence to get past the dialog.
2. **The row is append-only.** The history is the feature: "who turned it off,
   when, and why" is a question asked about a window that has already closed, and
   a mutable row answers it only for the most recent change, which is the one
   nobody needs to ask about.
3. **The screen already says so.** `_redaction_warnings` has warned about a
   non-redacting engine since the read-only version, in `danger` tone, and the
   engine list carries a `Redacts nothing` badge on the row itself.
4. **The change is logged at WARNING** with the admin, and the reason when
   one was given.

What would have been genuinely worse than a loud toggle: an operator responding
to an incident by editing an environment variable and restarting the gateway,
with no record anywhere of what was changed or why. The console version is the
one that leaves evidence.

## Decision

### 1. The engine list comes from the registry, described

`register()` gained optional metadata and the registry gained `describe()`,
returning `EngineInfo` — label, description, `needs_endpoint`, `redacts`. Same
shape and same argument as the provider-plugin listing in ADR 0032: installing an
engine makes it selectable without a console release.

The metadata is **optional**, and an engine registered without it still lists,
described by its own name. Thin, and better than refusing to show an installed
engine because its author did not fill in a form. Entry-point engines can declare
the same four as attributes on the factory class, read with `getattr`.

`redacts` is the load-bearing one. `noop` is a *real recorded engine* rather than
an absence — that is deliberate and predates this (see `noop.py`: every usage row
records `redaction_engine='noop'` so a past request cannot be mistaken for a
screened one). Which means "is redaction on" cannot be derived from the engine
name without hardcoding that name in the console, and a third-party engine could
redact nothing under any name at all. So it is declared, and `_engine_redacts`
asks the registry rather than comparing against `"noop"`.

### 2. A database row overrides the environment, and the API says which is in force

`redaction_config`, append-only, newest row wins. **No row means the environment
decides**, so a deployment that never opens the console behaves exactly as it did
before — which is what keeps the upgrade silent.

The migration deliberately does **not** seed a row from the current
`GATEWAY_REDACTION__ENGINE`. That would have looked tidier and would have been
wrong: it silently pins today's environment value into the database, so a later
change to the environment stops taking effect with nothing to explain why.

Because two sources can now disagree, the status response carries `source`
(`console` or `environment`) and the screen has a **Set by** row. An environment
variable that no longer takes effect looks like a broken one, and that confusion
is precisely what a database override introduces.

### 3. Only the engine name is settable. Not the endpoint, not the key.

The endpoint, the placeholder key, the language, the threshold and the entity
types stay in the environment. Two reasons, and only one of them is "not yet":

- The **placeholder key** is a secret whose rotation re-labels every transcript
  it ever labelled. It has no business in a form.
- An **endpoint that can be typed here is an endpoint that can be pointed at a
  logger** — every prompt, in plaintext, to an address an admin chose in a
  browser. Making that editable needs more thought than this slice has, and the
  plan doc's "editable endpoint plus a test button" is still the intended shape.

### 4. Four refusals, each preventing a *silent* failure

In order, in `PUT /api/admin/redaction/engine`:

| refused | because otherwise |
|---|---|
| an engine that is not installed | the registry's own rule: a gateway that believes redaction is on when it is not is the worst available outcome |
| an engine the environment cannot satisfy | the row would hold a configuration that cannot be built, so every worker logs a construction failure on its next poll and continues with the old engine — which looks like the change not working and reads like a bug |
| a detection engine whose service is not answering | test before save, the same rule providers follow. Enabling it fails every request needing redaction, or with `fail_open` forwards every prompt unredacted |
| ~~switching the layer off with no reason~~ | no longer refused — see item 1 of §"the concern". The record is still the mitigation; the typed sentence was not part of it |

The service check is **skipped when it is already the engine in force**, or an
operator could not switch away from a broken engine and back during an incident.
`noop` is never blocked for the same reason: whatever else is wrong, the way out
stays available.

### 5. Propagation: polling, not a per-request read

The question the old design never had to answer: **how does a worker that did not
handle the request find out?** Three answers, two wrong.

- *Read the row per request.* Correct and immediate, and it puts a `SELECT` on
  the hottest path in the gateway for a value that changes a few times a year. It
  also breaks the round-trip budget `test_query_counts.py` pins, which exists so
  that this kind of cost cannot be added without noticing.
- *Require a restart.* Honest, and what OIDC discovery does — but an operator
  switching the layer off is often responding to an incident, and "now restart
  the gateway" is the wrong sentence to read at that moment.
- *Poll.* One query every 10s per worker, off the request path entirely, so
  `get_redactor` stays a single attribute read.

Polling it is. `RedactionResolver` owns it, and three properties matter:

- **The redactor is rebuilt only when the row changes**, not every poll.
  `HttpDetectionRedactor` owns a connection pool and a per-process detection LRU
  that is most of its value on a long conversation
  ([performance.md](../performance.md)); rebuilding every ten seconds would
  quietly destroy the hit rate.
- **A failed poll changes nothing.** A database blip must not switch redaction
  off, and must not switch it on either — either direction is a policy change
  made by an outage.
- **An engine that cannot be built leaves the current one running**, loudly. The
  row was validated when saved, so reaching that state means the deployment
  changed underneath it — a plugin uninstalled, an endpoint removed. Falling back
  to `noop` there would switch the layer off as a side effect of a packaging
  mistake.

Staleness is bounded by the interval and **reported** as
`propagation_seconds` rather than implied. A change that looks instant and is not
is worse than one that says how long it takes. The worker handling the PUT
refreshes immediately, so the operator's own next request shows the change.

## Consequences

- Switching engines no longer needs a deployment, and now leaves a record that
  editing the environment never did.
- Two configuration sources exist for one value. Mitigated by `source` and the
  **Set by** row, not eliminated. This is the cost of the decision and it is real:
  an operator who changes the environment variable while a console row is in
  force will see no effect, and has to read the screen to find out why.
- `_redaction_service_health` now takes the effective engine rather than reading
  the setting. Asking the service about a configuration that is not running is
  how a screen ends up reassuring about the wrong deployment.
- The 10s poll is one query per worker per interval, forever. Cheap, and not
  free; it is the price of not putting it on the request path.

## Still not done, and deliberately

Per-scope redaction — per model, provider, user or group — is unchanged and still
specified in [redaction-scoping-plan.md](../redaction-scoping-plan.md). This slice
makes the *global* engine an admin decision; it does not introduce a scope, and
the precedence rule recorded there (any applicable scope requiring redaction
wins) is untouched.

Configurable detection parameters and an editable endpoint are also still ahead,
for the reasons in §3.
