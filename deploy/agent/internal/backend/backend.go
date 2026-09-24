package backend

import "context"

// Capabilities is what a backend can do beyond the floor every backend
// implements, advertised in the link's hello frame (PROTOCOL.md §5) so
// Cerea can hide affordances a given backend lacks instead of the backend
// faking them.
type Capabilities struct {
	Diff       bool `json:"diff"`
	Children   bool `json:"children"`
	Usage      bool `json:"usage"`
	Compact    bool `json:"compact"`
	Images     bool `json:"images"`
	Files      bool `json:"files"`
	Worktrees  bool `json:"worktrees"`
	AutoAccept bool `json:"autoAccept"`
	// Questions is the user-question tool design's own capability: whether
	// this backend has a native multiple-choice question mechanism
	// (opencode: the built-in "question" tool, GET/POST /question). ACP
	// reports false — ACP has no wire message for it.
	Questions bool `json:"questions"`
}

// CreateSessionOptions are session.create's optional fields (PROTOCOL.md
// §6). Empty ModeID/ModelID mean "let the backend choose its default".
type CreateSessionOptions struct {
	Title   string
	ModeID  string
	ModelID string
}

// Prompt is one session.prompt call's payload (PROTOCOL.md §6).
type Prompt struct {
	Text            string
	ClientMessageID string
	Attachments     []Attachment
}

// Backend is what internal/sessions drives per coding-agent backend: create
// and manage sessions, send prompts, stream normalized events, answer
// permission requests, and report modes/models. See the package doc for why
// this is shaped like ACP without being ACP.
//
// workspaceDir is always an absolute, already-validated directory (internal
// /workspaces and internal/policy have done their checks before a Backend
// method is ever called); a Backend implementation trusts it.
type Backend interface {
	ID() string
	Version() string
	Capabilities() Capabilities

	ListSessions(ctx context.Context, workspaceDir string) ([]Session, error)
	GetSession(ctx context.Context, workspaceDir, sessionID string) (Session, error)
	CreateSession(ctx context.Context, workspaceDir string, opts CreateSessionOptions) (Session, error)
	RenameSession(ctx context.Context, workspaceDir, sessionID, title string) (Session, error)
	DeleteSession(ctx context.Context, workspaceDir, sessionID string) error

	Prompt(ctx context.Context, workspaceDir, sessionID string, prompt Prompt) error
	Cancel(ctx context.Context, workspaceDir, sessionID string) error

	SetMode(ctx context.Context, workspaceDir, sessionID, modeID string) (Session, error)
	SetModel(ctx context.Context, workspaceDir, sessionID, modelID string) (Session, error)

	ReplyPermission(ctx context.Context, workspaceDir, sessionID, requestID string, decision Decision, message string) error

	Modes(ctx context.Context, workspaceDir string) ([]Mode, error)
	Models(ctx context.Context, workspaceDir string) ([]Model, error)

	// Transcript is the backend's own persisted record of a session. It is
	// what internal/sessions seeds a session's state from lazily, the first
	// time anything asks about a session it hasn't seen an event for yet
	// since this process started (PROTOCOL.md §7).
	Transcript(ctx context.Context, workspaceDir, sessionID string) (Transcript, error)

	// Subscribe streams every event of every session this backend knows
	// about, from now on. internal/sessions subscribes once, at process
	// start, and keeps the channel for the process's lifetime; a backend
	// implementation is responsible for reconnecting its own underlying
	// transport (opencode's SSE stream) internally and must not close the
	// channel just because one connection attempt failed. The channel
	// closing at all means the subscription is over for good — the caller
	// treats that as fatal (PROTOCOL.md §7: a new epoch, because this
	// process's view of events cannot be trusted to resume).
	Subscribe(ctx context.Context) (<-chan BackendEvent, error)
}

// BackendEvent pairs a backend's own session identity with the normalized
// Event, so a subscriber fanning out events (internal/sessions) can route
// each one without the backend having to know about workspace or session
// registries that live above it.
type BackendEvent struct {
	WorkspaceDir string
	SessionID    string
	Event        Event
}

// Differ is the optional "diff" capability (PROTOCOL.md §6 session.diff).
type Differ interface {
	Diff(ctx context.Context, workspaceDir, sessionID string) ([]FileDiff, error)
}

// Childrener is the optional "children" capability (session.children):
// subagent sessions spawned within a parent.
type Childrener interface {
	Children(ctx context.Context, workspaceDir, sessionID string) ([]Session, error)
}

// Compactor is the optional "compact" capability: manual context
// compaction (opencode: POST /session/:id/summarize).
type Compactor interface {
	Compact(ctx context.Context, workspaceDir, sessionID string) error
}

// Asker is the optional "questions" capability (the user-question tool
// design): answering or dismissing a pending multi-question ask (opencode:
// POST /question/:id/reply|reject). answers is one slice per question, each
// the labels chosen for that question, in order — the same shape opencode's
// own reply body takes ("each answer is an array of selected labels").
type Asker interface {
	ReplyQuestion(ctx context.Context, workspaceDir, sessionID, requestID string, answers [][]string) error
	RejectQuestion(ctx context.Context, workspaceDir, sessionID, requestID string) error
}
