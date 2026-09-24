package acp

import (
	"encoding/json"
	"sync"
	"time"

	"pystino-agent/internal/backend"
)

// pendingPermission is one outstanding session/request_permission the agent
// asked us, kept just long enough for ReplyPermission (or Cancel) to answer
// it. jsonrpcID is the agent's own request id (echoed back verbatim in the
// response); options is the agent's own option list, which ReplyPermission
// matches a Decision against by kind (PROTOCOL.md/the task: allow_once
// /allow_always/reject_once, with fallbacks).
type pendingPermission struct {
	jsonrpcID json.RawMessage
	options   []permissionOption
	request   backend.PermissionRequest
}

type permissionOption struct {
	OptionID string `json:"optionId"`
	Kind     string `json:"kind"`
	Name     string `json:"name"`
}

func findOptionByKind(opts []permissionOption, kinds ...string) string {
	for _, want := range kinds {
		for _, o := range opts {
			if o.Kind == want {
				return o.OptionID
			}
		}
	}
	return ""
}

// sessionState is everything this backend remembers about one ACP session
// between events: it is both the Session metadata (title/mode/model
// overlay — ACP gives session/set_mode|set_model no confirming session
// object back, found live, so the backend is the only place that
// remembers the choice) and the accumulated Transcript, built up as
// session/update notifications arrive (live, or replayed by session/load —
// see events.go's turn-vs-replay split).
type sessionState struct {
	mu sync.Mutex

	id           string
	workspaceDir string
	title        string
	modeID       string
	modelID      string
	status       backend.SessionStatus
	createdAt    time.Time
	updatedAt    time.Time

	messages  []backend.TranscriptEntry
	msgIndex  map[string]int // message id -> index into messages
	partIndex map[string]int // part id -> index into messages[..].Parts, keyed cross-message via partOwner
	partOwner map[string]int // part id -> which messages[] entry owns it
	todos     []backend.Todo

	pending map[string]pendingPermission // our local request id -> pending

	// turn tracking: set by Prompt for the duration of one session/prompt
	// call, so session/update chunks arriving while busy are folded into
	// this turn's single user/assistant message pair (PROTOCOL.md/the
	// task: "one message per turn per role; part id derived from the
	// turn"), rather than trusting ACP's own per-chunk message ids (an
	// agent is free to mint a fresh one per chunk).
	busy            bool
	turn            int
	assistantMsgID  string
	textPartID      string
	textSeen        bool
	reasoningPartID string
	reasoningSeen   bool
}

func newSessionState(id, workspaceDir string) *sessionState {
	now := time.Now()
	return &sessionState{
		id: id, workspaceDir: workspaceDir,
		status: backend.StatusIdle, createdAt: now, updatedAt: now,
		msgIndex: map[string]int{}, partIndex: map[string]int{}, partOwner: map[string]int{},
		pending: map[string]pendingPermission{},
	}
}

// upsertMessage inserts msg if unseen, else leaves the existing entry's
// Parts alone and just refreshes the fields msg carries (used both for the
// initial insert and for the "completed" pass that fills CompletedAt/Error
// once a turn ends).
func (s *sessionState) upsertMessage(msg backend.Message) {
	if idx, ok := s.msgIndex[msg.ID]; ok {
		s.messages[idx].Message = msg
		return
	}
	s.msgIndex[msg.ID] = len(s.messages)
	s.messages = append(s.messages, backend.TranscriptEntry{Message: msg})
}

// upsertPart inserts part if unseen (recording which message owns it for
// future updates), else replaces it in place — the shape session/update's
// tool_call_update and the text contract's "part" events both need.
func (s *sessionState) upsertPart(part backend.Part) {
	if idx, ok := s.partIndex[part.ID]; ok {
		msgIdx := s.partOwner[part.ID]
		s.messages[msgIdx].Parts[idx] = part
		return
	}
	msgIdx, ok := s.msgIndex[part.MessageID]
	if !ok {
		s.upsertMessage(backend.Message{ID: part.MessageID, Role: part.Role, CreatedAt: time.Now()})
		msgIdx = s.msgIndex[part.MessageID]
	}
	s.partIndex[part.ID] = len(s.messages[msgIdx].Parts)
	s.partOwner[part.ID] = msgIdx
	s.messages[msgIdx].Parts = append(s.messages[msgIdx].Parts, part)
}

func (s *sessionState) appendDelta(partID, text string) {
	if idx, ok := s.partIndex[partID]; ok {
		msgIdx := s.partOwner[partID]
		s.messages[msgIdx].Parts[idx].Text += text
	}
}

func (s *sessionState) toSession(backendID string) backend.Session {
	return backend.Session{
		ID: s.id, Backend: backendID, Title: s.title, Status: s.status,
		PendingPermissions: len(s.pending), ModeID: s.modeID, ModelID: s.modelID,
		AutoAccept: true, CreatedAt: s.createdAt, UpdatedAt: s.updatedAt,
	}
}

func (s *sessionState) toTranscript() backend.Transcript {
	tr := backend.Transcript{Status: s.status, Todos: append([]backend.Todo{}, s.todos...)}
	tr.Messages = append(tr.Messages, s.messages...)
	for _, p := range s.pending {
		tr.Permissions = append(tr.Permissions, p.request)
	}
	return tr
}

// registry is the process's session tables: sessionMu/sessions keyed by
// session id (every session this process has created, loaded or heard an
// event about), and capsMu/caps caching each workspace's modes/models —
// ACP has no session-independent "list the models" call, only session/new
// /session/load answer with them (found live), so the first session
// created or loaded for a workspace is what populates its entry.
type registry struct {
	sessionMu sync.Mutex
	sessions  map[string]*sessionState

	capsMu sync.Mutex
	caps   map[string]workspaceCaps
}

type workspaceCaps struct {
	modes  []backend.Mode
	models []backend.Model
}

func newRegistry() *registry {
	return &registry{sessions: map[string]*sessionState{}, caps: map[string]workspaceCaps{}}
}

func (r *registry) get(id string) (*sessionState, bool) {
	r.sessionMu.Lock()
	defer r.sessionMu.Unlock()
	st, ok := r.sessions[id]
	return st, ok
}

func (r *registry) getOrCreate(id, workspaceDir string) *sessionState {
	r.sessionMu.Lock()
	defer r.sessionMu.Unlock()
	if st, ok := r.sessions[id]; ok {
		return st
	}
	st := newSessionState(id, workspaceDir)
	r.sessions[id] = st
	return st
}

func (r *registry) put(st *sessionState) {
	r.sessionMu.Lock()
	r.sessions[st.id] = st
	r.sessionMu.Unlock()
}

func (r *registry) delete(id string) {
	r.sessionMu.Lock()
	delete(r.sessions, id)
	r.sessionMu.Unlock()
}

func (r *registry) list(workspaceDir string) []*sessionState {
	r.sessionMu.Lock()
	defer r.sessionMu.Unlock()
	var out []*sessionState
	for _, st := range r.sessions {
		if st.workspaceDir == workspaceDir {
			out = append(out, st)
		}
	}
	return out
}

func (r *registry) setCaps(workspaceDir string, c workspaceCaps) {
	if len(c.modes) == 0 && len(c.models) == 0 {
		return
	}
	r.capsMu.Lock()
	r.caps[workspaceDir] = c
	r.capsMu.Unlock()
}

func (r *registry) getCaps(workspaceDir string) workspaceCaps {
	r.capsMu.Lock()
	defer r.capsMu.Unlock()
	return r.caps[workspaceDir]
}
