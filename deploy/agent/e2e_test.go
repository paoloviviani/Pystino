package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
	"time"

	"github.com/coder/websocket"

	"pystino-agent/internal/backend"
	"pystino-agent/internal/link"
	"pystino-agent/internal/policy"
	"pystino-agent/internal/sessions"
	"pystino-agent/internal/workspaces"
)

// e2eFakeBackend is a minimal backend.Backend driven entirely by the test:
// CreateSession/Prompt/Cancel/ReplyPermission record calls and, where
// relevant, push a small scripted event sequence onto the Subscribe
// channel — enough to exercise the whole path from a wire req down through
// internal/sessions and back out as wire event frames, without a real
// opencode process.
type e2eFakeBackend struct {
	events chan backend.BackendEvent

	promptCalls chan struct{ sessionID, text string }
	cancelCalls chan string
	replyCalls  chan struct {
		requestID string
		decision  backend.Decision
	}
}

func newE2EFakeBackend() *e2eFakeBackend {
	return &e2eFakeBackend{
		events:      make(chan backend.BackendEvent, 64),
		promptCalls: make(chan struct{ sessionID, text string }, 8),
		cancelCalls: make(chan string, 8),
		replyCalls: make(chan struct {
			requestID string
			decision  backend.Decision
		}, 8),
	}
}

func (f *e2eFakeBackend) ID() string      { return "fake-e2e" }
func (f *e2eFakeBackend) Version() string { return "0.0.0" }
func (f *e2eFakeBackend) Capabilities() backend.Capabilities {
	return backend.Capabilities{Usage: true}
}

func (f *e2eFakeBackend) ListSessions(context.Context, string) ([]backend.Session, error) {
	return nil, nil
}
func (f *e2eFakeBackend) GetSession(_ context.Context, _ string, sessionID string) (backend.Session, error) {
	return backend.Session{ID: sessionID, Backend: "fake-e2e", Status: backend.StatusIdle}, nil
}
func (f *e2eFakeBackend) CreateSession(_ context.Context, workspaceDir string, opts backend.CreateSessionOptions) (backend.Session, error) {
	return backend.Session{ID: "ses_1", Backend: "fake-e2e", Title: opts.Title, Status: backend.StatusIdle}, nil
}
func (f *e2eFakeBackend) RenameSession(_ context.Context, _ string, sessionID, title string) (backend.Session, error) {
	return backend.Session{ID: sessionID, Title: title}, nil
}
func (f *e2eFakeBackend) DeleteSession(context.Context, string, string) error { return nil }

func (f *e2eFakeBackend) Prompt(_ context.Context, _ string, sessionID string, prompt backend.Prompt) error {
	f.promptCalls <- struct{ sessionID, text string }{sessionID, prompt.Text}
	go func() {
		f.events <- backend.BackendEvent{SessionID: sessionID, Event: backend.Event{Kind: backend.EventStatus, Status: backend.StatusBusy}}
		f.events <- backend.BackendEvent{SessionID: sessionID, Event: backend.Event{
			Kind: backend.EventPart,
			Part: &backend.Part{ID: "prt_1", MessageID: "msg_1", Role: "assistant", Type: backend.PartText, Text: "Hel"},
		}}
		f.events <- backend.BackendEvent{SessionID: sessionID, Event: backend.Event{
			Kind: backend.EventPart,
			Part: &backend.Part{ID: "prt_1", MessageID: "msg_1", Role: "assistant", Type: backend.PartText, Text: "Hello"},
		}}
		f.events <- backend.BackendEvent{SessionID: sessionID, Event: backend.Event{
			Kind:    backend.EventPermissionAsked,
			Request: &backend.PermissionRequest{ID: "perm_1", SessionID: sessionID, Tool: "bash", Title: "run echo"},
		}}
	}()
	return nil
}

func (f *e2eFakeBackend) Cancel(_ context.Context, _ string, sessionID string) error {
	f.cancelCalls <- sessionID
	return nil
}
func (f *e2eFakeBackend) SetMode(_ context.Context, _ string, sessionID, modeID string) (backend.Session, error) {
	return backend.Session{ID: sessionID, ModeID: modeID}, nil
}
func (f *e2eFakeBackend) SetModel(_ context.Context, _ string, sessionID, modelID string) (backend.Session, error) {
	return backend.Session{ID: sessionID, ModelID: modelID}, nil
}
func (f *e2eFakeBackend) ReplyPermission(_ context.Context, _ string, sessionID string, requestID string, decision backend.Decision, _ string) error {
	f.replyCalls <- struct {
		requestID string
		decision  backend.Decision
	}{requestID, decision}
	go func() {
		f.events <- backend.BackendEvent{SessionID: sessionID, Event: backend.Event{
			Kind: backend.EventPart,
			Part: &backend.Part{ID: "prt_tool", MessageID: "msg_1", Role: "assistant", Type: backend.PartTool, Tool: "bash", ToolStatus: backend.ToolCompleted, Output: "hi\n"},
		}}
		f.events <- backend.BackendEvent{SessionID: sessionID, Event: backend.Event{Kind: backend.EventStatus, Status: backend.StatusIdle}}
	}()
	return nil
}
func (f *e2eFakeBackend) Modes(context.Context, string) ([]backend.Mode, error)   { return nil, nil }
func (f *e2eFakeBackend) Models(context.Context, string) ([]backend.Model, error) { return nil, nil }
func (f *e2eFakeBackend) Transcript(context.Context, string, string) (backend.Transcript, error) {
	return backend.Transcript{}, nil
}
func (f *e2eFakeBackend) Subscribe(ctx context.Context) (<-chan backend.BackendEvent, error) {
	return f.events, nil
}

// e2eServerConn scripts one connection's frames from the fake-Cerea side.
type e2eServerConn struct {
	t    *testing.T
	conn *websocket.Conn
}

func (s *e2eServerConn) readJSON() map[string]any {
	s.t.Helper()
	_, raw, err := s.conn.Read(context.Background())
	if err != nil {
		s.t.Fatalf("reading frame: %v", err)
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		s.t.Fatalf("frame not JSON: %v (%s)", err, raw)
	}
	return m
}

// readUntilType drains frames until one of the given types is seen,
// returning it. Used to skip past live event frames when waiting for a
// specific res.
func (s *e2eServerConn) readUntilType(types ...string) map[string]any {
	s.t.Helper()
	for i := 0; i < 200; i++ {
		m := s.readJSON()
		typ, _ := m["type"].(string)
		for _, want := range types {
			if typ == want {
				return m
			}
		}
	}
	s.t.Fatalf("never saw a frame of type %v", types)
	return nil
}

func (s *e2eServerConn) send(v any) {
	s.t.Helper()
	body, err := json.Marshal(v)
	if err != nil {
		s.t.Fatal(err)
	}
	if err := s.conn.Write(context.Background(), websocket.MessageText, body); err != nil {
		s.t.Fatalf("writing frame: %v", err)
	}
}

func (s *e2eServerConn) req(id, op string, args any) map[string]any {
	s.send(map[string]any{"type": "req", "id": id, "op": op, "args": args})
	return s.readUntilType("res")
}

// TestE2EFakeCereaAgainstFakeBackend drives the whole machine (link ->
// dispatch -> sessions materializer -> a fake backend) from a scripted fake
// Cerea: hello -> welcome(pending) -> status(paired), then
// session.create/prompt/sync/permission.reply/cancel, asserting on both the
// res frames and the live event frames the prompt and the permission reply
// produce.
func TestE2EFakeCereaAgainstFakeBackend(t *testing.T) {
	fb := newE2EFakeBackend()
	pol := policy.Default()
	mat := sessions.New(fb, pol)
	if err := mat.Start(context.Background()); err != nil {
		t.Fatal(err)
	}

	reg, err := workspaces.Load(filepath.Join(t.TempDir(), "workspaces.json"))
	if err != nil {
		t.Fatal(err)
	}
	ws, err := reg.Create("test workspace", t.TempDir(), nil)
	if err != nil {
		t.Fatal(err)
	}

	mc := newMachine(reg, fb, mat, pol)

	scriptDone := make(chan struct{})
	var sessionID string

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{Subprotocols: []string{"pystino-machine.v1"}})
		if err != nil {
			t.Errorf("accept: %v", err)
			return
		}
		sc := &e2eServerConn{t: t, conn: conn}
		sc.readUntilType("hello")
		sc.send(map[string]any{"type": "welcome", "deviceId": "dev1", "status": "pending"})
		sc.send(map[string]any{"type": "status", "status": "paired"})

		createRes := sc.req("r1", "session.create", map[string]any{"workspaceId": ws.ID, "title": "t"})
		if createRes["ok"] != true {
			t.Fatalf("session.create failed: %+v", createRes)
		}
		sessionObj, _ := createRes["result"].(map[string]any)["session"].(map[string]any)
		sessionID, _ = sessionObj["id"].(string)
		if sessionID == "" {
			t.Fatalf("session.create result carried no session id: %+v", createRes)
		}

		promptRes := sc.req("r2", "session.prompt", map[string]any{"sessionId": sessionID, "text": "hello"})
		if promptRes["ok"] != true {
			t.Fatalf("session.prompt failed: %+v", promptRes)
		}

		// Drain live events until the permission.asked shows up (busy status
		// and the growing text part arrive first, per the text contract).
		var sawDelta, sawPart bool
		var permRequestID string
		for permRequestID == "" {
			frame := sc.readUntilType("event")
			ev, _ := frame["event"].(map[string]any)
			switch ev["kind"] {
			case "part":
				sawPart = true
			case "delta":
				sawDelta = true
			case "permission.asked":
				req, _ := ev["request"].(map[string]any)
				permRequestID, _ = req["id"].(string)
			}
		}
		if !sawPart {
			t.Error("never saw the initial part upsert")
		}
		if !sawDelta {
			t.Error("never saw the growth delta (text contract)")
		}
		if permRequestID != "perm_1" {
			t.Errorf("permission request id = %q, want perm_1", permRequestID)
		}

		replyRes := sc.req("r3", "permission.reply", map[string]any{
			"sessionId": sessionID, "requestId": permRequestID, "decision": "once",
		})
		if replyRes["ok"] != true {
			t.Fatalf("permission.reply failed: %+v", replyRes)
		}

		// Drain until the turn finishes (status idle).
		for {
			frame := sc.readUntilType("event")
			ev, _ := frame["event"].(map[string]any)
			if ev["kind"] == "status" && ev["status"] == "idle" {
				break
			}
		}

		syncRes := sc.req("r4", "session.sync", map[string]any{"sessionId": sessionID, "epoch": mat.Epoch(), "afterSeq": 0})
		if syncRes["ok"] != true {
			t.Fatalf("session.sync failed: %+v", syncRes)
		}
		syncResult, _ := syncRes["result"].(map[string]any)
		events, _ := syncResult["events"].([]any)
		if len(events) == 0 {
			t.Error("session.sync returned no events for a session with activity")
		}

		cancelRes := sc.req("r5", "session.cancel", map[string]any{"sessionId": sessionID})
		if cancelRes["ok"] != true {
			t.Fatalf("session.cancel failed: %+v", cancelRes)
		}

		close(scriptDone)
	}))
	defer srv.Close()

	lnk := link.New(link.Config{
		CereaOrigin: srv.URL,
		MachineID:   "m1",
		MachineName: "test",
		Cred:        &fakeCredForE2E{},
		Hello:       func() link.Hello { return buildHello(fb, pol) },
		Handler:     mc,
		Logf:        t.Logf,
	})

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	go lnk.Run(ctx)
	go forwardEvents(ctx, mat, lnk)

	select {
	case <-scriptDone:
	case <-time.After(9 * time.Second):
		t.Fatal("server-side script never completed")
	}

	select {
	case call := <-fb.promptCalls:
		if call.sessionID != sessionID || call.text != "hello" {
			t.Errorf("unexpected prompt call: %+v", call)
		}
	default:
		t.Error("backend Prompt was never called")
	}
	select {
	case call := <-fb.replyCalls:
		if call.requestID != "perm_1" || call.decision != backend.DecisionOnce {
			t.Errorf("unexpected reply call: %+v", call)
		}
	default:
		t.Error("backend ReplyPermission was never called")
	}
	select {
	case sid := <-fb.cancelCalls:
		if sid != sessionID {
			t.Errorf("cancel called for %q, want %q", sid, sessionID)
		}
	default:
		t.Error("backend Cancel was never called")
	}
}

// fakeCredForE2E is a link.Credential that needs no real OIDC machinery.
type fakeCredForE2E struct{}

func (fakeCredForE2E) Token(context.Context) (string, error)        { return "tok", nil }
func (fakeCredForE2E) ForceRefresh(context.Context) (string, error) { return "tok", nil }
func (fakeCredForE2E) NextRenewal() time.Time                       { return time.Now().Add(time.Hour) }
