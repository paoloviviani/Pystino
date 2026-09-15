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
- [x] Gateway: plugin `base_url_options` (protocol + generic default +
      anthropic/cortecs standalone declarations), jina.py docstring citation +
      options, registry.describe() passthrough, `PluginBaseURLOption` +
      `base_url_options` on `ProviderPluginResponse`
- [x] Gateway tests: provider-plugins listing contract + jina create/switch
      (test_providers.py), plugin-level host pinning (test_provider_plugins.py),
      unified route end-to-end EU host via FakeUpstream.urls (test_unified_search.py
      + conftest + add_backend base_url param)
- [x] ruff clean; mypy clean (93 files); 153 targeted tests pass
- [x] Commit 1: 9c2ec37 (gateway side)
- [x] Console: types.ts `base_url_options`, BackendDialog Endpoint select
      (create pre-fills plugin default; edit preselects stored host; undocumented
      stored URL offered alongside so it is never silently moved), Save guard
      treats a host switch as a change
- [x] Console tests: 201 pass (195 + 6 new dialog tests); typecheck green both
      workspaces

## Next

- [ ] full `uv run pytest -q` green (running, log at
      /tmp/opencode/pystino-jina-pytest.log)
- [ ] `pnpm -r test` full (packages/ui + console)
- [ ] final commit (console side)
- [ ] report
