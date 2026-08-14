# 0003 — Build the gateway rather than adopt LiteLLM

- Status: accepted
- Date: 2026-08-14
- Note: this decision was reversed once during the session and then reinstated. The
  reasoning below is what settled it.

## Context

The expensive decision of this project. An OpenAI-compatible gateway with virtual
keys, budgets and multi-provider routing already exists as open source, and
reimplementing one is weeks of work that could instead be configuration.

## Options considered

### Adopt LiteLLM proxy (MIT core)

Genuinely capable: 100+ providers behind one API, cost tables, an admin UI, virtual
keys, budgets.

The problem is where the line is drawn. **SSO/OIDC, SAML, RBAC and audit logs are
Enterprise-only** (roughly $250/month entry, ~$30k/year premium). OIDC login with a
configurable group claim, per-group model availability and per-group accounting are
not peripheral features of this project — they *are* the project. Adopting LiteLLM
therefore means either paying for the commercial licence, or reimplementing the
gated features around a system that already believes it owns keys and budgets.

Two further findings from the issue tracker weigh directly on accounting
correctness:

- **BerriAI/litellm#25389** — LiteLLM stops reading an upstream stream at
  `finish_reason`, so a trailing usage-only chunk (vLLM and some OpenAI-compatible
  backends emit one) is lost and usage is silently absent. Closed as **not
  planned**. The maintainers' suggested workaround is "put a reverse proxy in front
  of LiteLLM that merges the trailing chunks" — which is unavailable to us, because
  we would be in front of it, not behind.
- **BerriAI/litellm#25350** — in config-only (no database) mode the proxy could
  silently drop every model after 8–14 hours, with `/health` still reporting
  healthy. Closed, but the shape of the failure (a background reload partially
  failing and wiping state without logging) is the shape that matters.

### Adopt the LiteLLM core and build auth around it

Run it as a stateless no-DB router purely for provider normalisation, with our own
gateway in front owning auth, quotas, accounting and redaction. This avoids the
Enterprise gate entirely and was briefly chosen.

### Build our own

Own the whole request path.

## Decision

**Build our own gateway.** One configurable OpenAI-compatible upstream in Phase 1;
multi-provider routing later (`models.provider` already exists for it).

Reasons, in order of weight:

1. **The gated features are the product.** Paying or reimplementing are the only two
   options, and reimplementing *around* another system's key and budget model is
   harder than owning it.
2. **Accounting correctness must be ours.** #25389 is precisely the failure the brief
   warned about — streamed responses silently reporting zero tokens — and it is
   closed as not-planned. Our own SSE path lets us both force
   `stream_options.include_usage` *and* fall back to a labelled local estimate when
   a usage frame never arrives.
3. **Redaction must own the response path, not wrap it.** Rewriting a stream while
   an intermediary is also reassembling it means two SSE parsers with different
   ideas about event boundaries.
4. It is maintainable by one Python developer, which was the stated constraint.

## Consequences

- We own the provider-compatibility treadmill: every provider's deviation from the
  OpenAI schema becomes our bug. Mitigated by being a *permissive proxy* — the
  request model uses `extra="allow"` and forwards unrecognised parameters untouched,
  so a new provider parameter needs no release from us.
- We do not get 100+ providers for free. Phase 1 needs exactly one.
- The mitigations that came out of this analysis are implemented and tested:
  - `usage_source` on every usage row is `upstream_exact`, `estimated` or
    `unavailable`. **Zero is never silently recorded** for a stream that produced
    text ([0007](0007-sse-streaming.md), [0008](0008-accounting-model.md)).
  - `GET /v1/models` is served from **our** database filtered by group, never
    proxied, so an upstream that loses its model list cannot empty our clients' view.
- If LiteLLM later relicenses its SSO features, revisit — but by then the accounting
  and redaction paths are the valuable part and would not move.
