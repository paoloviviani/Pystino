# NOTES — Jina endpoint choice (standard vs EU host)

Branch: `feat/jina-eu-endpoint` (worktree `/home/ubuntu/workspace/pystino-jina-endpoint`).
Constraint: never edit `/home/ubuntu/workspace/Pystino` itself (live bind-mount).

## Verified at source (ground rule 2)

- docs.jina.ai Search API page, fetched 2026-09-14: endpoint `https://s.jina.ai/`,
  and on the same page: "Use https://eu.s.jina.ai/ to reside all infrastructure
  and data processing operations entirely within EU jurisdiction."
  Both hosts documented by the vendor. EU claim is the vendor's own wording.

## Code facts verified

- `ProviderCreateRequest.base_url` optional → route falls back to
  `plugin.default_base_url`, refuses when the plugin has none (admin.py:488-495).
- `ProviderUpdateRequest` is an all-optional patch; `base_url` is rstripped and
  `providers.forget(provider.id)` rebuilds the client (admin.py:631-653).
- Unified route: `upstream.post_json(plugin.search_path, …)` where the upstream
  is built from `provider.base_url` (providers.py:125, upstream.py:166). No
  route change needed to honour a different host.
- Search backends are created/edited ONLY in `AdminSearch.tsx` `BackendDialog`
  (AdminProviders filters kind!=="search", ADR 0071). That dialog sends no
  base_url today — plugin default applies.
- `ProviderPluginResponse.default_base_url` already exposed (schemas.py:792).

## Design

Plugin declares documented endpoints as (url, label) pairs (`base_url_options`);
`registry.describe()` passes them through; console renders a constrained
"Endpoint" select in the search BackendDialog when a plugin offers more than
one. Free-text base_url editing elsewhere is untouched. API keeps accepting any
base_url (a hand-set URL is the operator's own; the select is a console
ergonomic, not a validation rule).

## Done

- [x] Worktree created; hosts verified at source
- [x] Read CLAUDE.md / AGENTS.md first

## Next

- [ ] plugin + registry + schema changes
- [ ] gateway tests (test_providers, test_unified_search + conftest urls,
      test_provider_plugins)
- [ ] ruff + mypy + pytest green
- [ ] console: types.ts, AdminSearch.tsx BackendDialog select + tests
- [ ] console typecheck + tests green (was 195 passing)
- [ ] commit (one-shot identity; reasoning-style message)
