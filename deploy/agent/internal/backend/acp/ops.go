package acp

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"pystino-agent/internal/backend"
)

// Compile-time check: Backend must satisfy the floor interface. Unlike
// internal/backend/opencode, none of the optional capability interfaces
// (Differ/Childrener/Compactor) apply — ACP has no wire message for any of
// them (PROTOCOL.md §2), so Capabilities() reports them false and this
// package implements none of those interfaces.
var _ backend.Backend = (*Backend)(nil)

func (b *Backend) ListSessions(ctx context.Context, workspaceDir string) ([]backend.Session, error) {
	b.infoMu.Lock()
	canList := b.agentCaps.sessionList
	b.infoMu.Unlock()
	if canList {
		conn, err := b.currentConn()
		if err != nil {
			return nil, err
		}
		callCtx, cancel := context.WithTimeout(ctx, rpcCallTimeout)
		defer cancel()
		raw, err := conn.call(callCtx, "session/list", map[string]any{})
		if err != nil {
			return nil, fmt.Errorf("acp: session/list: %w", err)
		}
		var m map[string]any
		if err := json.Unmarshal(raw, &m); err != nil {
			return nil, fmt.Errorf("acp: session/list: parsing response: %w", err)
		}
		for _, sm := range asMaps(getSlice(m, "sessions")) {
			id := getStr(sm, "sessionId", "id")
			cwd := getStr(sm, "cwd")
			if id == "" || cwd != workspaceDir {
				continue
			}
			st := b.reg.getOrCreate(id, workspaceDir)
			st.mu.Lock()
			if st.title == "" {
				st.title = getStr(sm, "title")
			}
			st.mu.Unlock()
		}
	}
	// Whether or not session/list is advertised, the answer is built from
	// this process's own registry: for an agent without "list", that
	// registry (populated by CreateSession/session/load) is the only
	// source of truth this backend has (PROTOCOL.md/the task: "else keep
	// an in-memory registry per cwd").
	var out []backend.Session
	for _, st := range b.reg.list(workspaceDir) {
		st.mu.Lock()
		out = append(out, st.toSession(b.ID()))
		st.mu.Unlock()
	}
	return out, nil
}

func (b *Backend) GetSession(ctx context.Context, workspaceDir, sessionID string) (backend.Session, error) {
	st, ok := b.reg.get(sessionID)
	if !ok {
		return backend.Session{}, fmt.Errorf("acp: unknown session %q", sessionID)
	}
	st.mu.Lock()
	defer st.mu.Unlock()
	return st.toSession(b.ID()), nil
}

func (b *Backend) CreateSession(ctx context.Context, workspaceDir string, opts backend.CreateSessionOptions) (backend.Session, error) {
	conn, err := b.currentConn()
	if err != nil {
		return backend.Session{}, err
	}
	callCtx, cancel := context.WithTimeout(ctx, rpcCallTimeout)
	defer cancel()
	raw, err := conn.call(callCtx, "session/new", map[string]any{"cwd": workspaceDir, "mcpServers": []any{}})
	if err != nil {
		return backend.Session{}, fmt.Errorf("acp: session/new: %w", err)
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		return backend.Session{}, fmt.Errorf("acp: session/new: parsing response: %w", err)
	}
	id := getStr(m, "sessionId")
	if id == "" {
		return backend.Session{}, fmt.Errorf("acp: session/new: response carried no sessionId")
	}
	modes, models, curMode, curModel := sessionOptions(m)
	b.reg.setCaps(workspaceDir, workspaceCaps{modes: modes, models: models})

	st := b.reg.getOrCreate(id, workspaceDir)
	st.mu.Lock()
	st.title = opts.Title
	st.modeID = curMode
	st.modelID = curModel
	st.mu.Unlock()

	if opts.ModeID != "" && opts.ModeID != curMode {
		if _, err := b.SetMode(ctx, workspaceDir, id, opts.ModeID); err != nil {
			b.cfg.Logf("acp: session/new: setting initial mode %q: %v", opts.ModeID, err)
		}
	}
	if opts.ModelID != "" && opts.ModelID != curModel {
		if _, err := b.SetModel(ctx, workspaceDir, id, opts.ModelID); err != nil {
			b.cfg.Logf("acp: session/new: setting initial model %q: %v", opts.ModelID, err)
		}
	}
	return b.GetSession(ctx, workspaceDir, id)
}

// RenameSession is best effort: ACP has no rename/set-title method, so the
// title lives only in this process's overlay (PROTOCOL.md/the task).
func (b *Backend) RenameSession(ctx context.Context, workspaceDir, sessionID, title string) (backend.Session, error) {
	st, ok := b.reg.get(sessionID)
	if !ok {
		return backend.Session{}, fmt.Errorf("acp: unknown session %q", sessionID)
	}
	st.mu.Lock()
	st.title = title
	st.updatedAt = time.Now()
	sess := st.toSession(b.ID())
	st.mu.Unlock()
	return sess, nil
}

// DeleteSession is best effort: session/close if the agent advertised it,
// otherwise this just forgets the session locally (PROTOCOL.md/the task).
func (b *Backend) DeleteSession(ctx context.Context, workspaceDir, sessionID string) error {
	b.infoMu.Lock()
	canClose := b.agentCaps.sessionClose
	b.infoMu.Unlock()
	if canClose {
		if conn, err := b.currentConn(); err == nil {
			callCtx, cancel := context.WithTimeout(ctx, rpcCallTimeout)
			_, callErr := conn.call(callCtx, "session/close", map[string]any{"sessionId": sessionID})
			cancel()
			if callErr != nil {
				b.cfg.Logf("acp: session/close %s: %v", sessionID, callErr)
			}
		}
	}
	b.reg.delete(sessionID)
	return nil
}

// Prompt sends session/prompt and returns as soon as it is on the wire
// (PROTOCOL.md/the task: async, matching opencode's prompt_async) — the
// turn's own completion arrives later, off awaitPromptResult, as a status
// event. It also synthesizes this turn's user Message/Part locally: found
// live, opencode's ACP mode never sends a user_message_chunk during a live
// prompt (only session/load's replay does), so the client's own request is
// the only source of truth for what the user actually said.
func (b *Backend) Prompt(ctx context.Context, workspaceDir, sessionID string, prompt backend.Prompt) error {
	conn, err := b.currentConn()
	if err != nil {
		return err
	}
	st, ok := b.reg.get(sessionID)
	if !ok {
		st = b.reg.getOrCreate(sessionID, workspaceDir)
	}

	st.mu.Lock()
	st.turn++
	turn := st.turn
	userMsgID := fmt.Sprintf("%s:t%d:user", sessionID, turn)
	userPartID := userMsgID + ":text"
	st.assistantMsgID = fmt.Sprintf("%s:t%d:assistant", sessionID, turn)
	st.textPartID = st.assistantMsgID + ":text"
	st.reasoningPartID = st.assistantMsgID + ":reasoning"
	st.textSeen = false
	st.reasoningSeen = false
	st.busy = true
	st.status = backend.StatusBusy
	userMsg := backend.Message{ID: userMsgID, Role: "user", CreatedAt: time.Now(), ClientMessageID: prompt.ClientMessageID}
	userPart := backend.Part{ID: userPartID, MessageID: userMsgID, Role: "user", Type: backend.PartText, Text: prompt.Text}
	st.upsertMessage(userMsg)
	st.upsertPart(userPart)
	st.mu.Unlock()

	b.emit(workspaceDir, sessionID, backend.Event{Kind: backend.EventMessage, Message: &userMsg})
	b.emit(workspaceDir, sessionID, backend.Event{Kind: backend.EventPart, Part: &userPart})
	b.emit(workspaceDir, sessionID, backend.Event{Kind: backend.EventStatus, Status: backend.StatusBusy})

	respCh, err := conn.callAsync("session/prompt", map[string]any{
		"sessionId": sessionID,
		"prompt":    promptContentBlocks(prompt),
	})
	if err != nil {
		st.mu.Lock()
		st.busy = false
		st.status = backend.StatusIdle
		st.mu.Unlock()
		b.emit(workspaceDir, sessionID, backend.Event{Kind: backend.EventStatus, Status: backend.StatusIdle})
		return err
	}
	go b.awaitPromptResult(workspaceDir, sessionID, st, respCh)
	return nil
}

func (b *Backend) awaitPromptResult(workspaceDir, sessionID string, st *sessionState, respCh <-chan rpcMessage) {
	msg := <-respCh

	st.mu.Lock()
	st.busy = false
	assistantMsgID := st.assistantMsgID
	st.status = backend.StatusIdle
	st.updatedAt = time.Now()

	if msg.Error != nil {
		st.mu.Unlock()
		b.emit(workspaceDir, sessionID, backend.Event{Kind: backend.EventError, ErrorMessage: msg.Error.Message, ErrorCode: fmt.Sprint(msg.Error.Code)})
		b.emit(workspaceDir, sessionID, backend.Event{Kind: backend.EventStatus, Status: backend.StatusIdle})
		return
	}

	var res struct {
		StopReason string `json:"stopReason"`
	}
	_ = json.Unmarshal(msg.Result, &res)

	now := time.Now()
	errMsg := ""
	if res.StopReason == "refusal" {
		errMsg = "the agent refused this prompt"
	}
	if idx, ok := st.msgIndex[assistantMsgID]; ok {
		st.messages[idx].Message.CompletedAt = &now
		st.messages[idx].Message.Error = errMsg
	}
	st.mu.Unlock()

	if errMsg != "" {
		b.emit(workspaceDir, sessionID, backend.Event{Kind: backend.EventError, ErrorMessage: errMsg})
	}
	// cancelled and end_turn/max_tokens/max_turn_requests all just mean
	// "the turn is over" (PROTOCOL.md/the task: "cancelled → idle, no
	// error"); only refusal (above) and a JSON-RPC error carry an error
	// event.
	b.emit(workspaceDir, sessionID, backend.Event{Kind: backend.EventStatus, Status: backend.StatusIdle})
}

// Cancel sends session/cancel (a notification, not a request — verified
// live) and, per the ACP spec, resolves any permission request still
// pending for this session with a "cancelled" outcome rather than leaving
// the agent's request_permission call hanging forever.
func (b *Backend) Cancel(ctx context.Context, workspaceDir, sessionID string) error {
	conn, err := b.currentConn()
	if err != nil {
		return err
	}
	st, ok := b.reg.get(sessionID)
	if ok {
		st.mu.Lock()
		pending := st.pending
		st.pending = map[string]pendingPermission{}
		st.mu.Unlock()
		for _, p := range pending {
			_ = conn.respond(p.jsonrpcID, map[string]any{"outcome": map[string]any{"outcome": "cancelled"}}, nil)
		}
	}
	return conn.notify("session/cancel", map[string]any{"sessionId": sessionID})
}

func (b *Backend) SetMode(ctx context.Context, workspaceDir, sessionID, modeID string) (backend.Session, error) {
	conn, err := b.currentConn()
	if err != nil {
		return backend.Session{}, err
	}
	callCtx, cancel := context.WithTimeout(ctx, rpcCallTimeout)
	defer cancel()
	if _, err := conn.call(callCtx, "session/set_mode", map[string]any{"sessionId": sessionID, "modeId": modeID}); err != nil {
		return backend.Session{}, fmt.Errorf("acp: session/set_mode: %w", err)
	}
	st, ok := b.reg.get(sessionID)
	if !ok {
		return backend.Session{}, fmt.Errorf("acp: unknown session %q", sessionID)
	}
	st.mu.Lock()
	st.modeID = modeID
	st.updatedAt = time.Now()
	sess := st.toSession(b.ID())
	st.mu.Unlock()
	return sess, nil
}

// SetModel calls ACP's still-unstable session/set_model. An agent that does
// not implement it answers a plain JSON-RPC "method not found", which this
// wraps into a message that says so rather than a bare rpc error
// (PROTOCOL.md/the task: "report unsupported cleanly").
func (b *Backend) SetModel(ctx context.Context, workspaceDir, sessionID, modelID string) (backend.Session, error) {
	conn, err := b.currentConn()
	if err != nil {
		return backend.Session{}, err
	}
	callCtx, cancel := context.WithTimeout(ctx, rpcCallTimeout)
	defer cancel()
	if _, err := conn.call(callCtx, "session/set_model", map[string]any{"sessionId": sessionID, "modelId": modelID}); err != nil {
		return backend.Session{}, fmt.Errorf("acp: session/set_model is not supported by this agent: %w", err)
	}
	st, ok := b.reg.get(sessionID)
	if !ok {
		return backend.Session{}, fmt.Errorf("acp: unknown session %q", sessionID)
	}
	st.mu.Lock()
	st.modelID = modelID
	st.updatedAt = time.Now()
	sess := st.toSession(b.ID())
	st.mu.Unlock()
	return sess, nil
}

// ReplyPermission answers a pending session/request_permission with the
// optionId matching decision's kind (PROTOCOL.md/the task:
// once->allow_once, always->allow_always (falling back to allow_once if
// the agent offered none), reject->reject_once (falling back to
// reject_always)).
func (b *Backend) ReplyPermission(ctx context.Context, workspaceDir, sessionID, requestID string, decision backend.Decision, message string) error {
	conn, err := b.currentConn()
	if err != nil {
		return err
	}
	st, ok := b.reg.get(sessionID)
	if !ok {
		return fmt.Errorf("acp: unknown session %q", sessionID)
	}
	st.mu.Lock()
	pending, ok := st.pending[requestID]
	if ok {
		delete(st.pending, requestID)
	}
	st.mu.Unlock()
	if !ok {
		return fmt.Errorf("acp: no pending permission %q", requestID)
	}

	var optionID string
	switch decision {
	case backend.DecisionOnce:
		optionID = findOptionByKind(pending.options, "allow_once")
	case backend.DecisionAlways:
		optionID = findOptionByKind(pending.options, "allow_always", "allow_once")
	case backend.DecisionReject:
		optionID = findOptionByKind(pending.options, "reject_once", "reject_always")
	}
	if optionID == "" {
		return fmt.Errorf("acp: no permission option matches decision %q (options: %+v)", decision, pending.options)
	}
	if err := conn.respond(pending.jsonrpcID, map[string]any{"outcome": map[string]any{"outcome": "selected", "optionId": optionID}}, nil); err != nil {
		return err
	}
	b.emit(workspaceDir, sessionID, backend.Event{Kind: backend.EventPermissionReplied, RequestID: requestID, Decision: decision, By: "user"})
	return nil
}

func (b *Backend) Modes(ctx context.Context, workspaceDir string) ([]backend.Mode, error) {
	return b.reg.getCaps(workspaceDir).modes, nil
}

func (b *Backend) Models(ctx context.Context, workspaceDir string) ([]backend.Model, error) {
	return b.reg.getCaps(workspaceDir).models, nil
}

// Transcript returns the session's accumulated state if this process has
// touched it (live prompts, or an earlier Transcript call), otherwise
// tries session/load (PROTOCOL.md §7's no-gap guarantee) — its replayed
// session/update notifications populate the registry through the very
// same handleNotify path a live turn uses (see events.go), so by the time
// the session/load call itself returns, the registry already holds
// everything it replayed.
func (b *Backend) Transcript(ctx context.Context, workspaceDir, sessionID string) (backend.Transcript, error) {
	st, ok := b.reg.get(sessionID)
	if !ok {
		b.infoMu.Lock()
		// Both loadSession (the top-level agentCapabilities flag) and
		// sessionCapabilities.resume gate session/load — verified live,
		// opencode 1.18.31 advertises both together, but the ACP schema
		// treats them as two separate signals, so a stricter agent that
		// sets one without the other is trusted over guessing.
		canLoad := b.agentCaps.loadSession && b.agentCaps.sessionResume
		b.infoMu.Unlock()
		if !canLoad {
			return backend.Transcript{Status: backend.StatusIdle}, nil
		}
		conn, err := b.currentConn()
		if err != nil {
			return backend.Transcript{}, err
		}
		callCtx, cancel := context.WithTimeout(ctx, rpcCallTimeout)
		raw, err := conn.call(callCtx, "session/load", map[string]any{"sessionId": sessionID, "cwd": workspaceDir, "mcpServers": []any{}})
		cancel()
		if err != nil {
			return backend.Transcript{}, fmt.Errorf("acp: session/load: %w", err)
		}
		var m map[string]any
		if json.Unmarshal(raw, &m) == nil {
			modes, models, curMode, curModel := sessionOptions(m)
			b.reg.setCaps(workspaceDir, workspaceCaps{modes: modes, models: models})
			st = b.reg.getOrCreate(sessionID, workspaceDir)
			st.mu.Lock()
			if st.modeID == "" {
				st.modeID = curMode
			}
			if st.modelID == "" {
				st.modelID = curModel
			}
			st.mu.Unlock()
		}
		st, ok = b.reg.get(sessionID)
		if !ok {
			st = b.reg.getOrCreate(sessionID, workspaceDir)
		}
	}
	st.mu.Lock()
	defer st.mu.Unlock()
	return st.toTranscript(), nil
}
