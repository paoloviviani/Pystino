# 0017 — Desktop shell: Tauri v2

- Status: accepted (decision recorded; **not implemented** — Phase 4)
- Date: 2026-08-14

## Context

A desktop app wrapping the same web frontend and adding computer use. The assumption
was Tauri v2.

## Findings (verified 2026-08-14)

- **Tauri v2.10.1** is the latest stable, released 4 March 2026. v2.0 has been stable
  since October 2024 and continues to receive releases.
- **Tauri 3 exists only as alpha** — `tauri-cef-v3.0.0-alpha.7`, last published 23 May
  2026, with a 3.0 milestone still open. There is no stable v3.
- Licence: MIT / Apache-2.0.

## Decision

**Tauri v2.** The assumption was correct. **Do not target v3** until it is stable and has
had a release cycle in the wild.

The app is a *shell*. Conversations sync because the server is authoritative, so there is
no second data model and no reconciliation logic — which is the single biggest reason this
is cheap to build and the reason not to let the frontend start owning conversation state
([0015](0015-frontend-stack.md)).

## Consequences

- Electron was not seriously considered: Tauri's smaller footprint and Rust core suit a
  self-hosted research tool, and the web app is the product either way.
- **Computer use needs its own ADR before any of it is written.** It is the only
  capability here that does not exist in the web app, and it is the one that hands a model
  control of the user's machine. Note explicitly that the sandboxing analysis in
  [0021](0021-code-sandbox.md) does **not** transfer: that is about running generated code
  server-side, where we control the boundary. Driving a user's own desktop has a different
  threat model, a different consent model, and no sandbox at all unless one is designed.
- Auto-update, code signing and notarisation are unaddressed and are usually where
  desktop timelines actually go.
