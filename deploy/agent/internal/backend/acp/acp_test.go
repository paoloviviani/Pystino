package acp

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"testing"
	"time"

	"pystino-agent/internal/backend"
)

// fakeAgent is an in-process ACP agent: it speaks the same rpcConn wire
// protocol as the real Backend, wired to the opposite ends of two
// io.Pipes, so these tests never spawn a real process (that is what
// acp_it_test.go, gated behind PYSTINO_AGENT_ACP_IT=1, does against real
// `opencode acp`). Using rpcConn on both ends means the fake agent gets
// ACP's bidirectional request/response/notify behavior for free instead of
// reimplementing a second, parallel protocol reader.
type fakeAgent struct {
	t    *testing.T
	conn *rpcConn

	sessionCounter int

	promptReqs chan fakePromptReq
	cancels    chan string
}

type fakePromptReq struct {
	id        json.RawMessage
	sessionID string
}

func newFakeAgent(t *testing.T, w io.Writer) *fakeAgent {
	fa := &fakeAgent{
		t:          t,
		promptReqs: make(chan fakePromptReq, 16),
		cancels:    make(chan string, 16),
	}
	fa.conn = newRPCConn(w, fa.handleRequest, fa.handleNotify, t.Logf)
	return fa
}

func (fa *fakeAgent) handleRequest(id json.RawMessage, method string, params json.RawMessage) {
	switch method {
	case "initialize":
		fa.respond(id, map[string]any{
			"protocolVersion": 1,
			"agentCapabilities": map[string]any{
				"loadSession":         true,
				"promptCapabilities":  map[string]any{"image": true},
				"sessionCapabilities": map[string]any{"list": map[string]any{}, "close": map[string]any{}, "resume": map[string]any{}},
			},
			"agentInfo": map[string]any{"name": "FakeAgent", "version": "0.0.1-test"},
		})
	case "session/new", "session/load":
		fa.sessionCounter++
		sid := fmt.Sprintf("sess-%d", fa.sessionCounter)
		fa.respond(id, map[string]any{"sessionId": sid, "configOptions": []any{}})
	case "session/prompt":
		var p map[string]any
		_ = json.Unmarshal(params, &p)
		fa.promptReqs <- fakePromptReq{id: id, sessionID: getStr(p, "sessionId")}
		// Deliberately no respond here: the test completes it later via
		// fa.respond(pr.id, ...), which is what lets the async-Prompt-
		// contract test observe Prompt() returning well before that.
	case "session/set_mode", "session/set_model", "session/close":
		fa.respond(id, map[string]any{})
	case "session/list":
		fa.respond(id, map[string]any{"sessions": []any{}})
	default:
		fa.respond(id, map[string]any{})
	}
}

func (fa *fakeAgent) handleNotify(method string, params json.RawMessage) {
	if method != "session/cancel" {
		return
	}
	var p map[string]any
	_ = json.Unmarshal(params, &p)
	fa.cancels <- getStr(p, "sessionId")
}

func (fa *fakeAgent) respond(id json.RawMessage, result any) {
	if err := fa.conn.respond(id, result, nil); err != nil {
		fa.t.Logf("fake agent: respond: %v", err)
	}
}

func (fa *fakeAgent) update(sessionID string, update map[string]any) {
	if err := fa.conn.notify("session/update", map[string]any{"sessionId": sessionID, "update": update}); err != nil {
		fa.t.Logf("fake agent: update: %v", err)
	}
}

// requestPermission sends session/request_permission (agent->client) and
// waits for the reply, returning the chosen optionId ("" if cancelled).
func (fa *fakeAgent) requestPermission(ctx context.Context, sessionID, toolCallID string, options []permissionOption) (string, error) {
	raw, err := fa.conn.call(ctx, "session/request_permission", map[string]any{
		"sessionId": sessionID,
		"toolCall":  map[string]any{"toolCallId": toolCallID, "title": "run it", "kind": "execute"},
		"options":   options,
	})
	if err != nil {
		return "", err
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		return "", err
	}
	outcome := getMap(m, "outcome")
	if getStr(outcome, "outcome") == "cancelled" {
		return "", nil
	}
	return getStr(outcome, "optionId"), nil
}

var standardPermissionOptions = []permissionOption{
	{OptionID: "once", Kind: "allow_once", Name: "Allow once"},
	{OptionID: "always", Kind: "allow_always", Name: "Always allow"},
	{OptionID: "reject", Kind: "reject_once", Name: "Reject"},
}

// newTestBackend wires a Backend to a fakeAgent over two io.Pipes (no real
// subprocess) and waits for the handshake to complete, mirroring what
// Start/runOnce do against a real child (see acp.go's serveConn doc).
func newTestBackend(t *testing.T) (*Backend, *fakeAgent) {
	t.Helper()
	agentReads, backendWrites := io.Pipe() // client(backend) -> agent
	backendReads, agentWrites := io.Pipe() // agent -> client(backend)

	b := New(Config{Command: []string{"fake-agent"}, Logf: t.Logf})
	fa := newFakeAgent(t, agentWrites)
	go fa.conn.readLoop(agentReads)

	ctx, cancel := context.WithCancel(context.Background())
	serveErr := make(chan error, 1)
	go func() { serveErr <- b.serveConn(ctx, backendReads, backendWrites) }()

	select {
	case <-b.ready:
	case err := <-serveErr:
		t.Fatalf("backend connection ended before becoming ready: %v", err)
	case <-time.After(5 * time.Second):
		t.Fatal("backend never became ready")
	}

	t.Cleanup(func() {
		cancel()
		_ = backendWrites.Close()
		_ = agentWrites.Close()
	})
	return b, fa
}

func TestHandshake(t *testing.T) {
	b, _ := newTestBackend(t)
	if got, want := b.ID(), "acp:FakeAgent"; got != want {
		t.Errorf("ID() = %q, want %q", got, want)
	}
	if got, want := b.Version(), "0.0.1-test"; got != want {
		t.Errorf("Version() = %q, want %q", got, want)
	}
	caps := b.Capabilities()
	if !caps.Images {
		t.Error("Capabilities().Images = false, want true (fake agent advertised promptCapabilities.image)")
	}
	if caps.Diff || caps.Children || caps.Usage || caps.Compact || caps.Worktrees {
		t.Errorf("Capabilities() = %+v, want Diff/Children/Usage/Compact/Worktrees all false", caps)
	}
	if !caps.AutoAccept {
		t.Error("Capabilities().AutoAccept = false, want true")
	}
}

// drainEvents collects events for sessionID from ch until stop returns
// true for one of them, or timeout elapses — same shape as
// opencode_it_test.go's drainEvents, reused here so the two backends' tests
// read the same way.
func drainEvents(t *testing.T, ch <-chan backend.BackendEvent, sessionID string, timeout time.Duration, stop func(backend.Event) bool) []backend.Event {
	t.Helper()
	var out []backend.Event
	deadline := time.After(timeout)
	for {
		select {
		case env := <-ch:
			if env.SessionID != sessionID {
				continue
			}
			out = append(out, env.Event)
			if stop(env.Event) {
				return out
			}
		case <-deadline:
			t.Fatalf("timed out after %s; saw %d events: %+v", timeout, len(out), out)
		}
	}
}

func isIdle(ev backend.Event) bool {
	return ev.Kind == backend.EventStatus && ev.Status == backend.StatusIdle
}

func TestPromptStreamsChunksToNormalizedEvents(t *testing.T) {
	b, fa := newTestBackend(t)
	ctx := context.Background()
	dir := "/work"

	sess, err := b.CreateSession(ctx, dir, backend.CreateSessionOptions{})
	if err != nil {
		t.Fatalf("CreateSession: %v", err)
	}
	events, err := b.Subscribe(ctx)
	if err != nil {
		t.Fatalf("Subscribe: %v", err)
	}

	if err := b.Prompt(ctx, dir, sess.ID, backend.Prompt{Text: "say hello", ClientMessageID: "cm-1"}); err != nil {
		t.Fatalf("Prompt: %v", err)
	}

	var pr fakePromptReq
	select {
	case pr = <-fa.promptReqs:
	case <-time.After(5 * time.Second):
		t.Fatal("fake agent never saw session/prompt")
	}
	if pr.sessionID != sess.ID {
		t.Fatalf("session/prompt sessionId = %q, want %q", pr.sessionID, sess.ID)
	}

	fa.update(pr.sessionID, map[string]any{"sessionUpdate": "agent_message_chunk", "messageId": "m1", "content": map[string]any{"type": "text", "text": "Hel"}})
	fa.update(pr.sessionID, map[string]any{"sessionUpdate": "agent_message_chunk", "messageId": "m1", "content": map[string]any{"type": "text", "text": "lo"}})
	fa.respond(pr.id, map[string]any{"stopReason": "end_turn"})

	evs := drainEvents(t, events, sess.ID, 5*time.Second, isIdle)

	var sawUserMsg, sawUserPart, sawBusy, sawAssistantPart, sawDelta bool
	var assembled string
	for _, ev := range evs {
		switch ev.Kind {
		case backend.EventMessage:
			if ev.Message.Role == "user" {
				sawUserMsg = true
				if ev.Message.ClientMessageID != "cm-1" {
					t.Errorf("user message ClientMessageID = %q, want %q", ev.Message.ClientMessageID, "cm-1")
				}
			}
		case backend.EventPart:
			if ev.Part.Role == "user" && ev.Part.Type == backend.PartText {
				sawUserPart = true
				if ev.Part.Text != "say hello" {
					t.Errorf("user part text = %q, want %q", ev.Part.Text, "say hello")
				}
			}
			if ev.Part.Role == "assistant" && ev.Part.Type == backend.PartText {
				sawAssistantPart = true
				assembled = ev.Part.Text
			}
		case backend.EventDelta:
			sawDelta = true
			assembled += ev.Delta
		case backend.EventStatus:
			if ev.Status == backend.StatusBusy {
				sawBusy = true
			}
		}
	}
	if !sawUserMsg {
		t.Error("never saw the synthesized user message")
	}
	if !sawUserPart {
		t.Error("never saw the synthesized user text part")
	}
	if !sawBusy {
		t.Error("never saw status busy")
	}
	if !sawAssistantPart {
		t.Error("never saw the assistant text part (first chunk)")
	}
	if !sawDelta {
		t.Error("never saw a delta (second chunk)")
	}
	if assembled != "Hello" {
		t.Errorf("assembled assistant text = %q, want %q", assembled, "Hello")
	}
}

func TestToolCallPermissionRoundTrip(t *testing.T) {
	for _, tc := range []struct {
		name        string
		decision    backend.Decision
		wantOptions string
	}{
		{"once", backend.DecisionOnce, "once"},
		{"reject", backend.DecisionReject, "reject"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			b, fa := newTestBackend(t)
			ctx := context.Background()
			dir := "/work"

			sess, err := b.CreateSession(ctx, dir, backend.CreateSessionOptions{})
			if err != nil {
				t.Fatalf("CreateSession: %v", err)
			}
			events, _ := b.Subscribe(ctx)

			if err := b.Prompt(ctx, dir, sess.ID, backend.Prompt{Text: "run the command"}); err != nil {
				t.Fatalf("Prompt: %v", err)
			}
			var pr fakePromptReq
			select {
			case pr = <-fa.promptReqs:
			case <-time.After(5 * time.Second):
				t.Fatal("fake agent never saw session/prompt")
			}

			fa.update(pr.sessionID, map[string]any{
				"sessionUpdate": "tool_call", "toolCallId": "call_1", "title": "bash", "kind": "execute", "status": "pending",
			})

			type result struct {
				optionID string
				err      error
			}
			resCh := make(chan result, 1)
			go func() {
				opt, err := fa.requestPermission(ctx, pr.sessionID, "call_1", standardPermissionOptions)
				resCh <- result{opt, err}
			}()

			asked := drainEvents(t, events, sess.ID, 5*time.Second, func(ev backend.Event) bool {
				return ev.Kind == backend.EventPermissionAsked
			})
			req := asked[len(asked)-1].Request
			if req == nil || req.CallID != "call_1" {
				t.Fatalf("permission.asked request = %+v, want CallID call_1", req)
			}

			if err := b.ReplyPermission(ctx, dir, sess.ID, req.ID, tc.decision, ""); err != nil {
				t.Fatalf("ReplyPermission: %v", err)
			}

			select {
			case res := <-resCh:
				if res.err != nil {
					t.Fatalf("fake agent's request_permission call: %v", res.err)
				}
				if res.optionID != tc.wantOptions {
					t.Errorf("agent received optionId %q, want %q", res.optionID, tc.wantOptions)
				}
			case <-time.After(5 * time.Second):
				t.Fatal("fake agent never got a permission reply")
			}

			fa.update(pr.sessionID, map[string]any{"sessionUpdate": "tool_call_update", "toolCallId": "call_1", "status": "completed"})
			fa.respond(pr.id, map[string]any{"stopReason": "end_turn"})
			drainEvents(t, events, sess.ID, 5*time.Second, isIdle)
		})
	}
}

func TestCancelReachesIdleWithNoError(t *testing.T) {
	b, fa := newTestBackend(t)
	ctx := context.Background()
	dir := "/work"

	sess, err := b.CreateSession(ctx, dir, backend.CreateSessionOptions{})
	if err != nil {
		t.Fatalf("CreateSession: %v", err)
	}
	events, _ := b.Subscribe(ctx)

	if err := b.Prompt(ctx, dir, sess.ID, backend.Prompt{Text: "go slow"}); err != nil {
		t.Fatalf("Prompt: %v", err)
	}
	var pr fakePromptReq
	select {
	case pr = <-fa.promptReqs:
	case <-time.After(5 * time.Second):
		t.Fatal("fake agent never saw session/prompt")
	}

	if err := b.Cancel(ctx, dir, sess.ID); err != nil {
		t.Fatalf("Cancel: %v", err)
	}
	select {
	case sid := <-fa.cancels:
		if sid != pr.sessionID {
			t.Errorf("session/cancel sessionId = %q, want %q", sid, pr.sessionID)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("fake agent never saw session/cancel")
	}

	fa.respond(pr.id, map[string]any{"stopReason": "cancelled"})
	evs := drainEvents(t, events, sess.ID, 5*time.Second, isIdle)
	for _, ev := range evs {
		if ev.Kind == backend.EventError {
			t.Errorf("saw an error event after cancel: %+v", ev)
		}
	}
}

// TestPromptReturnsBeforeTurnCompletes is the async Prompt contract
// (PROTOCOL.md/the task): Prompt must return once session/prompt is on the
// wire, not once the turn ends. The fake agent here never responds to the
// prompt request at all, so if Prompt were (incorrectly) waiting for a
// reply, this test would time out instead of returning promptly.
func TestPromptReturnsBeforeTurnCompletes(t *testing.T) {
	b, fa := newTestBackend(t)
	ctx := context.Background()
	dir := "/work"

	sess, err := b.CreateSession(ctx, dir, backend.CreateSessionOptions{})
	if err != nil {
		t.Fatalf("CreateSession: %v", err)
	}

	done := make(chan error, 1)
	start := time.Now()
	go func() { done <- b.Prompt(ctx, dir, sess.ID, backend.Prompt{Text: "hi"}) }()

	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("Prompt: %v", err)
		}
		if elapsed := time.Since(start); elapsed > time.Second {
			t.Errorf("Prompt took %s to return; want it to return as soon as the request is sent", elapsed)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Prompt did not return promptly (it must not wait for the turn to finish)")
	}

	select {
	case <-fa.promptReqs:
	case <-time.After(5 * time.Second):
		t.Fatal("fake agent never saw session/prompt")
	}
}
