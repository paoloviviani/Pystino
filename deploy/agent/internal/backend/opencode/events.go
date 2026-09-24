package opencode

import (
	"bufio"
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"time"

	"pystino-agent/internal/backend"
)

const (
	eventStreamMinBackoff = time.Second
	eventStreamMaxBackoff = 30 * time.Second
)

// Subscribe streams every session's events from GET /global/event (SSE,
// all directories). It reconnects on its own with backoff whenever the
// stream ends — including every time opencode itself restarts underneath
// it — for as long as ctx lives; the returned channel only closes when ctx
// is done.
func (b *Backend) Subscribe(ctx context.Context) (<-chan backend.BackendEvent, error) {
	out := make(chan backend.BackendEvent, 256)
	go b.subscribeLoop(ctx, out)
	return out, nil
}

func (b *Backend) subscribeLoop(ctx context.Context, out chan<- backend.BackendEvent) {
	defer close(out)
	delay := eventStreamMinBackoff
	for {
		if ctx.Err() != nil {
			return
		}
		err := b.streamOnce(ctx, out)
		if ctx.Err() != nil {
			return
		}
		b.cfg.Logf("opencode: event stream ended (%v), reconnecting in %s", err, delay)
		select {
		case <-ctx.Done():
			return
		case <-time.After(delay):
		}
		delay *= 2
		if delay > eventStreamMaxBackoff {
			delay = eventStreamMaxBackoff
		}
	}
}

func (b *Backend) streamOnce(ctx context.Context, out chan<- backend.BackendEvent) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, b.baseURL()+"/global/event", nil)
	if err != nil {
		return err
	}
	req.SetBasicAuth("opencode", b.cfg.Password)
	resp, err := b.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 64*1024), 4<<20)
	var dataLines []string
	flush := func() {
		if len(dataLines) == 0 {
			return
		}
		raw := strings.Join(dataLines, "\n")
		dataLines = nil
		b.handleSSEData(raw, out)
	}
	for scanner.Scan() {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		line := scanner.Text()
		switch {
		case line == "":
			flush()
		case strings.HasPrefix(line, "data:"):
			dataLines = append(dataLines, strings.TrimPrefix(strings.TrimPrefix(line, "data:"), " "))
		default:
			// event:/id:/comment lines and anything else are not needed.
		}
	}
	flush()
	return scanner.Err()
}

// sseFrame is GET /global/event's per-message envelope: {directory,
// payload:{id,type,properties}}.
type sseFrame struct {
	Directory string         `json:"directory"`
	Payload   map[string]any `json:"payload"`
}

func (b *Backend) handleSSEData(raw string, out chan<- backend.BackendEvent) {
	var frame sseFrame
	if json.Unmarshal([]byte(raw), &frame) != nil || frame.Payload == nil {
		return
	}
	typ, _ := frame.Payload["type"].(string)
	props, _ := frame.Payload["properties"].(map[string]any)
	if props == nil {
		props = map[string]any{}
	}
	for _, be := range b.translateEvent(frame.Directory, typ, props) {
		out <- be
	}
}

// translateEvent turns one opencode SSE payload into zero or more
// normalized BackendEvents. Unknown types are dropped (PROTOCOL.md §5:
// unknown event kinds are ignored by both sides).
func (b *Backend) translateEvent(directory, typ string, props map[string]any) []backend.BackendEvent {
	sessionID := getStr(props, "sessionID", "sessionId", "id")

	wrap := func(sessionID string, ev backend.Event) backend.BackendEvent {
		return backend.BackendEvent{WorkspaceDir: directory, SessionID: sessionID, Event: ev}
	}

	switch typ {
	case "session.created", "session.updated":
		info := getMap(props, "info")
		if info == nil {
			info = props
		}
		sess := sessionFromMap(info)
		if sess.ID == "" {
			return nil
		}
		return []backend.BackendEvent{wrap(sess.ID, backend.Event{Kind: backend.EventSession, Session: &sess})}

	case "session.status":
		status := getMap(props, "status")
		state := getStr(status, "type")
		var s backend.SessionStatus
		switch state {
		case "busy":
			s = backend.StatusBusy
		case "retry":
			s = backend.StatusRetry
		default:
			s = backend.StatusIdle
		}
		return []backend.BackendEvent{wrap(sessionID, backend.Event{Kind: backend.EventStatus, Status: s})}

	case "session.idle":
		return []backend.BackendEvent{wrap(sessionID, backend.Event{Kind: backend.EventStatus, Status: backend.StatusIdle})}

	case "session.error":
		return []backend.BackendEvent{wrap(sessionID, backend.Event{
			Kind:         backend.EventError,
			ErrorMessage: getStr(props, "message"),
			ErrorCode:    getStr(props, "code"),
		})}

	case "session.diff":
		// Surfaced through the Differ capability's own poll (session.diff
		// op), not the event stream — opencode tells us a diff changed, but
		// the normalized Event set has no diff-content event; dropped here
		// deliberately rather than approximated.
		return nil

	case "message.updated":
		info := getMap(props, "info")
		if info == nil {
			return nil
		}
		msg := messageFromMap(info)
		if msg.ID == "" {
			return nil
		}
		sid := getStr(info, "sessionID", "sessionId")
		if sid == "" {
			sid = sessionID
		}
		events := []backend.BackendEvent{wrap(sid, backend.Event{Kind: backend.EventMessage, Message: &msg})}
		if msg.Role == "assistant" {
			if u := usageFromMessageMap(info); u != nil {
				b.fillContextMax(info, u)
				events = append(events, wrap(sid, backend.Event{Kind: backend.EventUsage, Usage: u}))
			}
		}
		return events

	case "message.part.updated":
		partMap := getMap(props, "part")
		if partMap == nil {
			return nil
		}
		part := partFromMap(partMap)
		sid := getStr(partMap, "sessionID", "sessionId")
		if sid == "" {
			sid = sessionID
		}
		return []backend.BackendEvent{wrap(sid, backend.Event{Kind: backend.EventPart, Part: &part})}

	case "message.part.delta":
		return []backend.BackendEvent{wrap(sessionID, backend.Event{
			Kind:      backend.EventDelta,
			MessageID: getStr(props, "messageID", "messageId"),
			PartID:    getStr(props, "partID", "partId"),
			Field:     getStr(props, "field"),
			Delta:     getStr(props, "delta"),
		})}

	case "message.part.removed":
		return []backend.BackendEvent{wrap(sessionID, backend.Event{
			Kind:      backend.EventPartRemoved,
			MessageID: getStr(props, "messageID", "messageId"),
			PartID:    getStr(props, "partID", "partId"),
		})}

	case "permission.asked":
		req := permissionFromMap(props)
		if req.ID == "" {
			return nil
		}
		return []backend.BackendEvent{wrap(req.SessionID, backend.Event{Kind: backend.EventPermissionAsked, Request: &req})}

	case "permission.replied":
		return []backend.BackendEvent{wrap(sessionID, backend.Event{
			Kind:      backend.EventPermissionReplied,
			RequestID: getStr(props, "requestID", "requestId"),
			Decision:  backend.Decision(getStr(props, "reply")),
			By:        "user",
		})}

	case "todo.updated":
		var todos []backend.Todo
		for _, tm := range asMaps(getSlice(props, "todos")) {
			todos = append(todos, todoFromMap(tm))
		}
		return []backend.BackendEvent{wrap(sessionID, backend.Event{Kind: backend.EventTodo, Todos: todos})}

	case "server.heartbeat":
		return nil

	default:
		return nil
	}
}

// fillContextMax looks up the model's published context window (cached
// from the last Models() call) and sets u.ContextMax from it, when the
// message names a model this process has seen.
func (b *Backend) fillContextMax(info map[string]any, u *backend.Usage) {
	providerID := getStr(info, "providerID", "providerId")
	modelID := getStr(info, "modelID", "modelId")
	if providerID == "" || modelID == "" {
		return
	}
	b.modelsMu.Lock()
	limit, ok := b.modelLimit[providerID+"/"+modelID]
	b.modelsMu.Unlock()
	if ok {
		max := limit
		u.ContextMax = &max
	}
}
