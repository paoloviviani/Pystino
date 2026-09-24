package backend

import (
	"encoding/json"
	"time"
)

// SessionStatus is a session's turn-boundary state, per PROTOCOL.md §6.
type SessionStatus string

const (
	StatusIdle  SessionStatus = "idle"
	StatusBusy  SessionStatus = "busy"
	StatusRetry SessionStatus = "retry"
	StatusError SessionStatus = "error"
)

// Session is the normalized session record (PROTOCOL.md §6 Types).
type Session struct {
	ID                 string        `json:"id"`
	WorkspaceID        string        `json:"workspaceId"`
	Backend            string        `json:"backend"`
	Title              string        `json:"title"`
	Status             SessionStatus `json:"status"`
	PendingPermissions int           `json:"pendingPermissions"`
	ModeID             string        `json:"modeId,omitempty"`
	ModelID            string        `json:"modelId,omitempty"`
	AutoAccept         bool          `json:"autoAccept"`
	ParentID           string        `json:"parentId,omitempty"`
	// ParentToolCallID is the parent's tool call that spawned this session
	// (set by session.children), so the panel can anchor it in the transcript.
	ParentToolCallID string    `json:"parentToolCallId,omitempty"`
	CreatedAt        time.Time `json:"createdAt"`
	UpdatedAt        time.Time `json:"updatedAt"`
	Usage            *Usage    `json:"usage,omitempty"`
}

// Message is a normalized chat message (PROTOCOL.md §7).
type Message struct {
	ID          string     `json:"id"`
	Role        string     `json:"role"` // "user" | "assistant"
	ParentID    string     `json:"parentId,omitempty"`
	CreatedAt   time.Time  `json:"createdAt"`
	ModeID      string     `json:"modeId,omitempty"`
	ModelID     string     `json:"modelId,omitempty"`
	CompletedAt *time.Time `json:"completedAt,omitempty"`
	Error       string     `json:"error,omitempty"`
	// ClientMessageID echoes session.prompt's clientMessageId on the user
	// message it created (it keys Cerea's attachment store). Set once, when
	// the backend first reports the user message a prompt produced; never
	// derivable from the backend itself, since opencode has no notion of it
	// — a Backend implementation is responsible for remembering the
	// mapping durably enough to survive a restart (PROTOCOL.md §7).
	ClientMessageID string `json:"clientMessageId,omitempty"`
}

// PartType discriminates Part's per-type fields (PROTOCOL.md §7).
type PartType string

const (
	PartText       PartType = "text"
	PartReason     PartType = "reasoning"
	PartTool       PartType = "tool"
	PartFile       PartType = "file"
	PartSubtask    PartType = "subtask"
	PartCompaction PartType = "compaction"
)

// ToolStatus is a tool part's lifecycle state.
type ToolStatus string

const (
	ToolPending   ToolStatus = "pending"
	ToolRunning   ToolStatus = "running"
	ToolCompleted ToolStatus = "completed"
	ToolFailed    ToolStatus = "error"
)

// Part is one part of a message's content, shaped per Type — a text or
// reasoning span, a tool call, a file attachment, or a subagent spawn
// (PROTOCOL.md §7). Only the fields for its own Type are populated; the
// rest are zero. One struct for every part type (instead of one Go type per
// PartType with an interface) keeps the text contract's "upsert of the full
// part" and delta-application logic in internal/sessions working over a
// single concrete type it can copy and compare by field, and matches the
// wire shape closely enough that encoding it is a small, explicit mapping
// rather than a set of type switches.
type Part struct {
	ID        string   `json:"id"`
	MessageID string   `json:"messageId"`
	Role      string   `json:"role"`
	Type      PartType `json:"type"`

	// text, reasoning
	Text      string `json:"text,omitempty"`
	Synthetic bool   `json:"synthetic,omitempty"`

	// tool
	CallID     string         `json:"callId,omitempty"`
	Tool       string         `json:"tool,omitempty"`
	ToolStatus ToolStatus     `json:"status,omitempty"`
	Title      string         `json:"title,omitempty"`
	Input      map[string]any `json:"input,omitempty"`
	Output     string         `json:"output,omitempty"`
	ToolError  string         `json:"error,omitempty"`

	// file
	Mime     string `json:"mime,omitempty"`
	Filename string `json:"filename,omitempty"`
	URL      string `json:"url,omitempty"`

	// subtask
	SubtaskSessionID string `json:"sessionId,omitempty"`
	Description      string `json:"description,omitempty"`
	Agent            string `json:"agent,omitempty"`

	// compaction (opencode's CompactionPart: a marker part on the assistant
	// message that summarized the session, PROTOCOL.md §7)
	Auto bool `json:"auto,omitempty"`
}

// Decision is a human's (or auto-accept's) answer to a PermissionRequest.
type Decision string

const (
	DecisionOnce   Decision = "once"
	DecisionAlways Decision = "always"
	DecisionReject Decision = "reject"
)

// PermissionRequest is one pending ask (PROTOCOL.md §6/§7).
type PermissionRequest struct {
	ID        string         `json:"id"`
	SessionID string         `json:"sessionId"`
	Tool      string         `json:"tool"`
	Title     string         `json:"title"`
	Patterns  []string       `json:"patterns,omitempty"`
	Metadata  map[string]any `json:"metadata,omitempty"`
	CallID    string         `json:"callId,omitempty"`
	MessageID string         `json:"messageId,omitempty"`
	Always    []string       `json:"always,omitempty"`
}

// Mode is a backend's named operating mode (opencode: agents with
// mode "primary" — compaction/summary/title are hidden by the backend
// implementation, never surfaced here).
type Mode struct {
	ID          string `json:"id"`
	Label       string `json:"label"`
	Description string `json:"description,omitempty"`
}

// Model is one selectable model, id "<providerId>/<model>".
type Model struct {
	ID            string `json:"id"`
	Label         string `json:"label"`
	ProviderID    string `json:"providerId"`
	IsDefault     bool   `json:"isDefault,omitempty"`
	ContextWindow int    `json:"contextWindow,omitempty"`
	Images        bool   `json:"images,omitempty"`
	Reasoning     bool   `json:"reasoning,omitempty"`
}

// Usage is per-session token and cost accounting (PROTOCOL.md §6).
// ContextMax is a pointer because "unknown" (the model published no limit)
// and "zero" are different answers to give the UI.
type Usage struct {
	Input       int     `json:"input"`
	Output      int     `json:"output"`
	Reasoning   int     `json:"reasoning"`
	CacheRead   int     `json:"cacheRead"`
	CacheWrite  int     `json:"cacheWrite"`
	Cost        float64 `json:"cost"`
	ContextUsed int     `json:"contextUsed"`
	ContextMax  *int    `json:"contextMax,omitempty"`
}

// FileDiffStatus is one changed file's kind of change.
type FileDiffStatus string

const (
	FileAdded    FileDiffStatus = "added"
	FileModified FileDiffStatus = "modified"
	FileDeleted  FileDiffStatus = "deleted"
)

// FileDiff is one file's change within a session (capability Differ).
type FileDiff struct {
	Path      string         `json:"path"`
	Status    FileDiffStatus `json:"status"`
	Before    string         `json:"before"`
	After     string         `json:"after"`
	Additions int            `json:"additions"`
	Deletions int            `json:"deletions"`
}

// TodoStatus is a plan item's lifecycle state.
type TodoStatus string

const (
	TodoPending    TodoStatus = "pending"
	TodoInProgress TodoStatus = "in_progress"
	TodoCompleted  TodoStatus = "completed"
	TodoCancelled  TodoStatus = "cancelled"
)

// Todo is one plan item (PROTOCOL.md §7 "todo" event).
type Todo struct {
	ID       string     `json:"id"`
	Content  string     `json:"content"`
	Status   TodoStatus `json:"status"`
	Priority string     `json:"priority,omitempty"`
}

// Attachment is a prompt's non-text input (PROTOCOL.md §6). URL is a data:
// URL for P0.
type Attachment struct {
	Type     string `json:"type"` // "file"
	Mime     string `json:"mime"`
	Filename string `json:"filename"`
	URL      string `json:"url"`
}

// TranscriptEntry pairs a message with its parts, as GET session.sync's
// snapshot and a backend's own persisted transcript both shape it.
type TranscriptEntry struct {
	Message Message `json:"message"`
	Parts   []Part  `json:"parts"`
}

// Transcript is a session's full recorded state: what internal/sessions
// seeds a session from before applying live events on top (PROTOCOL.md §7:
// "the snapshot equals the persisted transcript plus everything applied
// since, with no gap").
type Transcript struct {
	Messages    []TranscriptEntry   `json:"messages"`
	Permissions []PermissionRequest `json:"permissions"`
	Status      SessionStatus       `json:"status"`
	Usage       *Usage              `json:"usage,omitempty"`
	Todos       []Todo              `json:"todos"`
}

// EventKind discriminates Event's per-kind fields (PROTOCOL.md §7).
type EventKind string

const (
	EventMessage           EventKind = "message"
	EventPart              EventKind = "part"
	EventDelta             EventKind = "delta"
	EventPartRemoved       EventKind = "part.removed"
	EventStatus            EventKind = "status"
	EventPermissionAsked   EventKind = "permission.asked"
	EventPermissionReplied EventKind = "permission.replied"
	EventUsage             EventKind = "usage"
	EventSession           EventKind = "session"
	EventError             EventKind = "error"
	EventTodo              EventKind = "todo"
)

// Event is one normalized stream event (PROTOCOL.md §7). Its fields are
// grouped by Kind, populated only for that kind — the same shape the wire
// envelope needs, but this type intentionally carries no json tags of its
// own: Kind and Message both wanting the JSON key "message" for two
// different kinds is exactly the ambiguity Go's encoding/json resolves by
// silently dropping both fields, so wire encoding is done explicitly by
// internal/sessions (which already owns the envelope, epoch and seq this
// event is wrapped in) rather than trusted to struct tags here.
type Event struct {
	Kind EventKind

	// message: upsert
	Message *Message
	// part: upsert of the full part (see the text contract, PROTOCOL.md §7)
	Part *Part

	// delta: field is always "text" today, carried anyway for forward
	// compatibility with PROTOCOL.md's own wire shape.
	MessageID string
	PartID    string
	Role      string
	Field     string
	Delta     string

	// status
	Status SessionStatus
	Detail string

	// permission.asked
	Request *PermissionRequest

	// permission.replied
	RequestID string
	Decision  Decision
	By        string // "user" | "auto"

	// usage
	Usage *Usage

	// session: metadata changed
	Session *Session

	// error: a turn-level failure
	ErrorMessage string
	ErrorCode    string

	// todo: full list
	Todos []Todo
}

// MarshalJSON always emits arrays for the transcript's lists: a fresh session
// has no messages, permissions or todos, and PROTOCOL.md types all three as
// arrays that Cerea iterates without a null check.
func (t Transcript) MarshalJSON() ([]byte, error) {
	type plain Transcript
	out := plain(t)
	if out.Messages == nil {
		out.Messages = []TranscriptEntry{}
	}
	for i := range out.Messages {
		if out.Messages[i].Parts == nil {
			out.Messages[i].Parts = []Part{}
		}
	}
	if out.Permissions == nil {
		out.Permissions = []PermissionRequest{}
	}
	if out.Todos == nil {
		out.Todos = []Todo{}
	}
	return json.Marshal(out)
}
