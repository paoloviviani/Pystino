// Package backend defines the interface internal/sessions drives to talk to
// a coding-agent backend, and the normalized types that cross it — Session,
// Message, Part, Event, PermissionRequest, Mode, Model, Usage, FileDiff.
//
// The interface is shaped like ACP (Agent Client Protocol): the same nouns
// — session, prompt, an update stream, a permission request with
// once/always/reject, cancel, mode, model — because that shape already
// covers what every coding agent we expect to add looks like from the
// outside. It is not ACP itself. PROTOCOL.md §2 records why: opencode's own
// HTTP+SSE server (`opencode serve`) is richer than its ACP adapter for
// things ACP hasn't standardized or hasn't stabilized — session listing,
// usage and context accounting, per-session diffs, subagent trees, and
// multiple attached clients — and it persists sessions across restarts,
// where the ACP adapter is owned by one client process. Building the
// primary backend, opencode, against `opencode serve` needed an interface
// that could express all of that; building it against ACP first would have
// meant designing to the narrower surface and bolting the rest on later.
//
// The trade a bespoke interface makes: a second backend (a generic ACP
// adapter, covering Gemini CLI, Claude Code and Codex via their own ACP
// adapters, and Pi via pi-acp) is straightforward to add — it implements
// Backend and simply reports fewer optional capabilities — but it is not
// "any ACP-speaking client for free" the way adopting ACP as the interface
// verbatim would have been. Capabilities are advertised per backend
// (Backend.Capabilities, mirrored in the link's hello frame) precisely so
// Cerea can hide affordances a given backend lacks instead of the backend
// having to fake them.
package backend
