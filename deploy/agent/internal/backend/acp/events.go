package acp

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"pystino-agent/internal/backend"
)

// Subscribe returns the channel every normalized event this backend
// produces is pushed onto, for as long as ctx lives. Unlike
// internal/backend/opencode (one long-lived SSE stream to reconnect), ACP
// events arrive as session/update notifications on whichever connection is
// currently up; handleNotify (called from the active rpcConn's read loop,
// see acp.go's serveConn) pushes onto this same channel across restarts, so
// from Subscribe's caller's point of view there is exactly one continuous
// stream regardless of how many times the child process has been
// respawned.
// Subscribe never closes the returned channel on its own — a restarted
// child process is not a new epoch (the supervise loop's reconnect+
// re-handshake is meant to be transparent, matching
// internal/backend/opencode's SSE reconnect), so only the whole agent
// process exiting ends the subscription, and nothing needs to observe that
// over this channel (the caller's own ctx ends first).
func (b *Backend) Subscribe(ctx context.Context) (<-chan backend.BackendEvent, error) {
	b.eventsMu.Lock()
	if b.events == nil {
		b.events = make(chan backend.BackendEvent, 256)
	}
	ch := b.events
	b.eventsMu.Unlock()
	return ch, nil
}

func (b *Backend) emit(workspaceDir, sessionID string, ev backend.Event) {
	b.eventsMu.Lock()
	if b.events == nil {
		b.events = make(chan backend.BackendEvent, 256)
	}
	ch := b.events
	b.eventsMu.Unlock()
	ch <- backend.BackendEvent{WorkspaceDir: workspaceDir, SessionID: sessionID, Event: ev}
}

// handleRequest answers an incoming JSON-RPC request from the agent.
// session/request_permission is the only one this client implements; every
// other agent->client request (fs/*, terminal/*) is refused outright — we
// advertised neither capability in initialize; see conn.go's refuse.
func (b *Backend) handleRequest(id json.RawMessage, method string, params json.RawMessage) {
	if method != "session/request_permission" {
		if conn, err := b.currentConn(); err == nil {
			_ = conn.refuse(id, method)
		}
		return
	}
	var p struct {
		SessionID string             `json:"sessionId"`
		ToolCall  map[string]any     `json:"toolCall"`
		Options   []permissionOption `json:"options"`
	}
	if err := json.Unmarshal(params, &p); err != nil {
		return
	}
	st, ok := b.reg.get(p.SessionID)
	if !ok {
		st = b.reg.getOrCreate(p.SessionID, "")
	}

	localID := fmt.Sprintf("%s:perm:%s", p.SessionID, getStr(p.ToolCall, "toolCallId"))
	req := backend.PermissionRequest{
		ID:        localID,
		SessionID: p.SessionID,
		Tool:      getStr(p.ToolCall, "kind", "title"),
		Title:     getStr(p.ToolCall, "title"),
		CallID:    getStr(p.ToolCall, "toolCallId"),
	}

	st.mu.Lock()
	st.pending[localID] = pendingPermission{jsonrpcID: id, options: p.Options, request: req}
	workspaceDir := st.workspaceDir
	st.mu.Unlock()

	b.emit(workspaceDir, p.SessionID, backend.Event{Kind: backend.EventPermissionAsked, Request: &req})
}

// handleNotify processes every agent->client notification: session/update
// is the only one ACP defines that this backend needs (PROTOCOL.md §7's
// normalized events are all derived from it).
func (b *Backend) handleNotify(method string, params json.RawMessage) {
	if method != "session/update" {
		return
	}
	var p struct {
		SessionID string          `json:"sessionId"`
		Update    json.RawMessage `json:"update"`
	}
	if err := json.Unmarshal(params, &p); err != nil {
		return
	}
	var upd map[string]any
	if err := json.Unmarshal(p.Update, &upd); err != nil {
		return
	}
	kind := getStr(upd, "sessionUpdate")

	st, ok := b.reg.get(p.SessionID)
	if !ok {
		st = b.reg.getOrCreate(p.SessionID, "")
	}

	switch kind {
	case "agent_message_chunk":
		b.handleTextChunk(st, "assistant", upd, false)
	case "agent_thought_chunk":
		b.handleTextChunk(st, "assistant", upd, true)
	case "user_message_chunk":
		b.handleTextChunk(st, "user", upd, false)
	case "tool_call":
		b.handleToolCall(st, upd)
	case "tool_call_update":
		b.handleToolCall(st, upd)
	case "plan":
		b.handlePlan(st, upd)
	case "current_mode_update":
		b.handleModeUpdate(st, upd)
	default:
		// available_commands_update, usage_update (no Usage capability),
		// config_option_update: unmapped, dropped per PROTOCOL.md §5's
		// forward-compatibility rule (unknown kinds are ignored).
	}
}

// handleTextChunk folds one agent_message_chunk/agent_thought_chunk
// /user_message_chunk into the session's transcript, applying the text
// contract (PROTOCOL.md §7: the first "part" event for a part id carries
// the text so far, later growth is "delta" only).
//
// Two id strategies, chosen by whether a Prompt call is in flight for this
// session (st.busy): while busy, ids are derived from our own turn counter
// so a chatty agent that mints a fresh message id per chunk still produces
// "one message per turn per role" (PROTOCOL.md/the task); otherwise (a
// session/load replay, which happens before any Prompt on this process —
// see ops.go's Transcript) there is no turn to derive from, so the chunk's
// own ACP message id is used directly.
func (b *Backend) handleTextChunk(st *sessionState, role string, upd map[string]any, reasoning bool) {
	content := getMap(upd, "content")
	text := getStr(content, "text")
	if content == nil || getStr(content, "type") != "text" {
		return
	}
	acpMsgID := getStr(upd, "messageId", "messageID")

	st.mu.Lock()
	var msgID, partID string
	var firstChunk bool
	if role == "assistant" && st.busy {
		msgID = st.assistantMsgID
		if reasoning {
			partID = st.reasoningPartID
			firstChunk = !st.reasoningSeen
			st.reasoningSeen = true
		} else {
			partID = st.textPartID
			firstChunk = !st.textSeen
			st.textSeen = true
		}
	} else {
		msgID = acpMsgID
		if reasoning {
			partID = "reason:" + acpMsgID
		} else {
			partID = "part:" + acpMsgID
		}
		_, firstChunk = st.partIndex[partID]
		firstChunk = !firstChunk
		if firstChunk {
			st.upsertMessage(backend.Message{ID: msgID, Role: role, CreatedAt: time.Now()})
		}
	}
	if msgID == "" {
		st.mu.Unlock()
		return
	}
	partType := backend.PartText
	if reasoning {
		partType = backend.PartReason
	}
	var part backend.Part
	if firstChunk {
		part = backend.Part{ID: partID, MessageID: msgID, Role: role, Type: partType, Text: text}
		st.upsertPart(part)
	} else {
		st.appendDelta(partID, text)
	}
	workspaceDir := st.workspaceDir
	st.mu.Unlock()

	if firstChunk {
		b.emit(workspaceDir, st.id, backend.Event{Kind: backend.EventPart, Part: &part})
	} else {
		b.emit(workspaceDir, st.id, backend.Event{Kind: backend.EventDelta, MessageID: msgID, PartID: partID, Role: role, Field: "text", Delta: text})
	}
}

// handleToolCall folds a tool_call or tool_call_update into the session's
// transcript as a tool Part, keyed by ACP's own (stable) toolCallId.
func (b *Backend) handleToolCall(st *sessionState, upd map[string]any) {
	callID := getStr(upd, "toolCallId")
	if callID == "" {
		return
	}
	partID := "tool:" + callID

	st.mu.Lock()
	msgID := ""
	if _, ok := st.partIndex[partID]; ok {
		msgID = st.messages[st.partOwner[partID]].Message.ID
	} else if st.busy {
		msgID = st.assistantMsgID
	} else {
		msgID = "tool_msg:" + callID
		st.upsertMessage(backend.Message{ID: msgID, Role: "assistant", CreatedAt: time.Now()})
	}

	part := backend.Part{
		ID: partID, MessageID: msgID, Role: "assistant", Type: backend.PartTool,
		CallID:     callID,
		Tool:       getStr(upd, "kind", "title"),
		ToolStatus: mapToolStatus(getStr(upd, "status")),
		Title:      getStr(upd, "title"),
	}
	if in := getMap(upd, "rawInput"); in != nil {
		part.Input = in
	}
	if part.ToolStatus == backend.ToolCompleted {
		part.Output = toolCallText(upd)
	} else if part.ToolStatus == backend.ToolFailed {
		part.ToolError = toolCallText(upd)
	}
	st.upsertPart(part)
	workspaceDir := st.workspaceDir
	st.mu.Unlock()

	b.emit(workspaceDir, st.id, backend.Event{Kind: backend.EventPart, Part: &part})
}

func (b *Backend) handlePlan(st *sessionState, upd map[string]any) {
	var todos []backend.Todo
	for _, e := range asMaps(getSlice(upd, "entries")) {
		todos = append(todos, backend.Todo{
			ID:       getStr(e, "id"),
			Content:  getStr(e, "content"),
			Status:   backend.TodoStatus(getStr(e, "status")),
			Priority: getStr(e, "priority"),
		})
	}
	st.mu.Lock()
	st.todos = todos
	workspaceDir := st.workspaceDir
	st.mu.Unlock()
	b.emit(workspaceDir, st.id, backend.Event{Kind: backend.EventTodo, Todos: todos})
}

func (b *Backend) handleModeUpdate(st *sessionState, upd map[string]any) {
	modeID := getStr(upd, "currentModeId", "modeId")
	if modeID == "" {
		return
	}
	st.mu.Lock()
	st.modeID = modeID
	st.updatedAt = time.Now()
	sess := st.toSession(b.ID())
	workspaceDir := st.workspaceDir
	st.mu.Unlock()
	b.emit(workspaceDir, st.id, backend.Event{Kind: backend.EventSession, Session: &sess})
}
