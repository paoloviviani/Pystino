# Desktop app — placeholder

**Nothing is built here yet.** Phase 4 or later.

## What goes here

A Tauri v2 shell that wraps `apps/web` and adds computer use. It is a *shell*, not
a second client: conversations are synced because the server is authoritative, so
there is no separate data model and no reconciliation logic to write.

## Decisions already taken

| Choice | Decision | ADR |
|---|---|---|
| Framework | Tauri v2 (v2.10.1 stable as of 2026-03) | [0017](../../docs/adr/0017-desktop-shell.md) |

**Do not target Tauri 3.** As of August 2026 it exists only as alpha
(`tauri-cef-v3.0.0-alpha.7`, May 2026). v2 continues to receive releases.

## The part that needs real thought

Computer use is the only capability here that does not exist in the web app, and
it is the one that grants the model control of the user's machine. It needs its own
design pass and its own ADR before any of it is written — the sandboxing reasoning
in [0021](../../docs/adr/0021-code-sandbox.md) is about running *generated code*
server-side and does not transfer to driving the user's own desktop.
