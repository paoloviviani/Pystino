# 0027 — Inference providers as configurable records

- Date: 2026-08-15
- Status: accepted
- Supersedes the single-upstream assumption in
  [0013](0013-upstream-http-client.md), whose transport decisions still hold.

## Context

Phase 1 shipped one upstream, configured by `GATEWAY_UPSTREAM__BASE_URL` and
`GATEWAY_UPSTREAM__API_KEY`, and `models.provider` was a free-text label nothing
read. That was honest for a vertical slice and is now the constraint: a
foundation runs a commercial API alongside a self-hosted vLLM and somebody's
laptop Ollama, and adding one should not be a deploy.

Two things follow, and both are more than a screen:

- **Routing per model.** A request has to reach the provider that serves the
  model it names, with that provider's credentials and connection pool.
- **Credentials in the database.** A provider created through the console has an
  API key, and it has to live somewhere the gateway can read and a reader cannot.

Model access is also group-only today. "Give this one researcher the expensive
model" currently means inventing a group for one person.

## Decision

### 1. Providers are rows; every model points at one

A `providers` table (name, base URL, encrypted key, extra headers, active flag)
and `models.provider_id`, **not nullable**. No fallback to the environment at
request time: a model resolves to exactly one provider, or the configuration is
wrong and says so. Ambiguity about which credentials a request used is the last
thing wanted when reconciling a bill.

The migration creates a `default` provider from the existing environment
variables and points every existing model at it, so an upgrade changes nothing
observable. `GATEWAY_UPSTREAM__*` remains as the **bootstrap** for that row and
as the source of shared transport tuning (timeouts, pool sizes) — those are
properties of the gateway's HTTP client, not of a provider.

### 2. Keys are encrypted at rest and write-only over the API

Fernet (`cryptography`, Apache-2.0 OR BSD-3-Clause, already a transitive
dependency via joserfc, now declared directly), with the key from
`GATEWAY_SECRET_KEY` — deliberately **not** `GATEWAY_SESSION_SECRET`, because
rotating session signing should not destroy every provider credential.

- The API never returns a key. It returns a hint (`sk-…4f2a`) sufficient to tell
  two keys apart and useless to anyone who steals it.
- `GATEWAY_SECRET_KEY` accepts a comma-separated list: the first encrypts, any
  decrypts. That makes rotation a rolling restart rather than re-entering every
  provider's credentials by hand.
- A decrypt failure is loud and names the cause. A provider that silently sends
  no credentials would look like a provider outage and waste an afternoon.

The trade this accepts, stated plainly: a database dump now contains provider
credentials in encrypted form, and losing every value of `GATEWAY_SECRET_KEY`
means re-entering the keys. The alternative — storing only the *name* of an
environment variable — keeps secrets out of the database entirely but makes
adding a provider a deploy, which defeats the point of the screen.

### 3. Access is the union of group grants and user grants

`user_model_access` alongside `group_model_access`. A caller may use a model if
**their group has it or they do personally**. Absence of any grant still means no
access, and there is still no global allow-all.

Deliberately **no denials**. An explicit deny that overrides a group grant makes
"why can this person not use that model" a question requiring a search rather
than a look, and nobody asked for it.

### 4. A provider can be tested before anything depends on it

`POST /api/admin/providers/{id}/test` calls the provider's `/models` and reports
what came back. A wrong base URL or a stale key should be discovered when it is
entered, not by a user's request failing an hour later.

## Consequences

- The gateway holds one HTTP client **per provider**, built lazily and discarded
  when the provider changes. Connection pools are per-provider by nature — they
  are pools to a specific host — so this is the correct shape rather than an
  optimisation.
- Deactivating a provider makes every model behind it unusable. That is the
  intent, and the console says how many models are affected before the click.
- Deleting a provider is refused while models reference it. Cascading would turn
  historical spend into rows pointing at a model that can no longer be explained.
- `models.provider` (free text) is dropped. It was written by the importer and
  read by nothing; keeping it beside `provider_id` would give a display name two
  sources of truth.
- Discovery and import become per-provider: "what does *this* provider offer".
- Still absent: per-provider rate limits, weighted failover between providers
  serving the same model, and per-provider quotas. Each is plausible; none is
  needed to configure a provider, and failover in particular needs a policy
  decision about double-billing a retried request.
