package link

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/coder/websocket"
)

type fakeCred struct {
	token string
}

func (f *fakeCred) Token(context.Context) (string, error)        { return f.token, nil }
func (f *fakeCred) ForceRefresh(context.Context) (string, error) { return f.token, nil }
func (f *fakeCred) NextRenewal() time.Time                       { return time.Now().Add(time.Hour) }

type fakeHandler struct {
	calls chan string
}

func (h *fakeHandler) Handle(_ context.Context, op string, args json.RawMessage) (any, *OpError) {
	h.calls <- op
	return map[string]any{"echo": op}, nil
}

// serverConn is the minimal server-side driver a test uses to script one
// connection's frames without pulling in a real Cerea.
type serverConn struct {
	t    *testing.T
	conn *websocket.Conn
}

func (s *serverConn) readType(want string) map[string]any {
	s.t.Helper()
	_, raw, err := s.conn.Read(context.Background())
	if err != nil {
		s.t.Fatalf("reading %s: %v", want, err)
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		s.t.Fatalf("frame not JSON: %v", err)
	}
	if m["type"] != want {
		s.t.Fatalf("frame type = %v, want %s (frame: %s)", m["type"], want, raw)
	}
	return m
}

func (s *serverConn) send(v any) {
	s.t.Helper()
	body, err := json.Marshal(v)
	if err != nil {
		s.t.Fatal(err)
	}
	if err := s.conn.Write(context.Background(), websocket.MessageText, body); err != nil {
		s.t.Fatalf("writing frame: %v", err)
	}
}

func newFakeCereaServer(t *testing.T, drive func(*serverConn, *http.Request)) *httptest.Server {
	t.Helper()
	var srv *httptest.Server
	srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{
			Subprotocols: []string{"pystino-machine.v1"},
		})
		if err != nil {
			t.Errorf("server accept: %v", err)
			return
		}
		sc := &serverConn{t: t, conn: conn}
		drive(sc, r)
	}))
	t.Cleanup(srv.Close)
	return srv
}

// TestHandshakeAndPairingGate pins the wire handshake (hello -> welcome)
// and PROTOCOL.md §4's confirmation gate: a req before "paired" is refused
// forbidden without ever reaching the Handler, and one after is dispatched
// normally.
func TestHandshakeAndPairingGate(t *testing.T) {
	handler := &fakeHandler{calls: make(chan string, 4)}
	var gotAuth, gotMachineID, gotMachineName string
	done := make(chan struct{})

	srv := newFakeCereaServer(t, func(sc *serverConn, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		gotMachineID = r.Header.Get("X-Pystino-Machine-Id")
		gotMachineName = r.Header.Get("X-Pystino-Machine-Name")

		sc.readType("hello")
		sc.send(map[string]any{"type": "welcome", "deviceId": "dev1", "status": "pending"})

		sc.send(map[string]any{"type": "req", "id": "r1", "op": "session.list", "args": map[string]any{}})
		res := sc.readType("res")
		if res["ok"] != false {
			t.Errorf("pre-pairing req: ok = %v, want false", res["ok"])
		}
		errObj, _ := res["error"].(map[string]any)
		if errObj["code"] != "forbidden" {
			t.Errorf("pre-pairing req: error.code = %v, want forbidden", errObj["code"])
		}

		sc.send(map[string]any{"type": "status", "status": "paired"})
		sc.send(map[string]any{"type": "req", "id": "r2", "op": "session.list", "args": map[string]any{}})
		res2 := sc.readType("res")
		if res2["ok"] != true {
			t.Errorf("post-pairing req: ok = %v, want true", res2["ok"])
		}
		result, _ := res2["result"].(map[string]any)
		if result["echo"] != "session.list" {
			t.Errorf("post-pairing req: result = %v", res2["result"])
		}
		close(done)
	})

	l := New(Config{
		CereaOrigin: srv.URL,
		MachineID:   "machine-123",
		MachineName: "test box",
		Cred:        &fakeCred{token: "tok-abc"},
		Hello: func() Hello {
			return Hello{Agent: AgentInfo{Version: "0.1.0", OS: "linux", Arch: "amd64", Hostname: "h"}}
		},
		Handler: handler,
	})

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	runErr := make(chan error, 1)
	go func() { runErr <- l.Run(ctx) }()

	select {
	case <-done:
	case <-time.After(4 * time.Second):
		t.Fatal("server-side script never completed")
	}
	cancel()
	if err := <-runErr; err != nil && err != context.DeadlineExceeded && err != context.Canceled {
		t.Fatalf("Run: %v", err)
	}

	select {
	case op := <-handler.calls:
		if op != "session.list" {
			t.Errorf("handler called with op %q", op)
		}
	default:
		t.Fatal("handler was never called for the post-pairing req")
	}
	if gotAuth != "Bearer tok-abc" {
		t.Errorf("Authorization header = %q", gotAuth)
	}
	if gotMachineID != "machine-123" || gotMachineName != "test box" {
		t.Errorf("machine headers = %q / %q", gotMachineID, gotMachineName)
	}
}

// TestClose4403StopsReconnecting pins PROTOCOL.md §3: a 4403 close means
// revoked, and Run must return without reconnecting.
func TestClose4403StopsReconnecting(t *testing.T) {
	srv := newFakeCereaServer(t, func(sc *serverConn, r *http.Request) {
		sc.readType("hello")
		sc.send(map[string]any{"type": "welcome", "deviceId": "dev1", "status": "pending"})
		_ = sc.conn.Close(websocket.StatusCode(4403), "revoked")
	})

	l := New(Config{
		CereaOrigin: srv.URL,
		Cred:        &fakeCred{token: "tok"},
		Hello:       func() Hello { return Hello{} },
		Handler:     &fakeHandler{calls: make(chan string, 1)},
		MinBackoff:  10 * time.Millisecond,
	})

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	err := l.Run(ctx)
	if err == nil {
		t.Fatal("Run must return an error on 4403")
	}
	if ctx.Err() != nil {
		t.Fatalf("Run took the whole timeout instead of stopping on 4403: %v", ctx.Err())
	}
}

// TestPublishEventSendsFrame pins the outward event push shape.
func TestPublishEventSendsFrame(t *testing.T) {
	done := make(chan struct{})
	var gotEvent map[string]any

	srv := newFakeCereaServer(t, func(sc *serverConn, r *http.Request) {
		sc.readType("hello")
		sc.send(map[string]any{"type": "welcome", "deviceId": "dev1", "status": "paired"})
		frame := sc.readType("event")
		gotEvent = frame
		close(done)
	})

	l := New(Config{
		CereaOrigin: srv.URL,
		Cred:        &fakeCred{token: "tok"},
		Hello:       func() Hello { return Hello{} },
		Handler:     &fakeHandler{calls: make(chan string, 1)},
	})

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	go l.Run(ctx)

	// Wait for pairing before publishing — PublishEvent needs a live conn.
	deadline := time.Now().Add(2 * time.Second)
	for !l.Paired() {
		if time.Now().After(deadline) {
			t.Fatal("never became paired")
		}
		time.Sleep(5 * time.Millisecond)
	}
	if err := l.PublishEvent("s1", "epoch1", 3, json.RawMessage(`{"kind":"status","status":"busy"}`)); err != nil {
		t.Fatalf("PublishEvent: %v", err)
	}

	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("server never saw the event frame")
	}
	if gotEvent["sessionId"] != "s1" || gotEvent["epoch"] != "epoch1" {
		t.Errorf("event frame = %+v", gotEvent)
	}
	if seq, _ := gotEvent["seq"].(float64); seq != 3 {
		t.Errorf("event.seq = %v, want 3", gotEvent["seq"])
	}
}
