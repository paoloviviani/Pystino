package main

import (
	"context"
	"encoding/json"
	"testing"

	"pystino-agent/internal/backend"
	"pystino-agent/internal/policy"
	"pystino-agent/internal/sessions"
	"pystino-agent/internal/workspaces"
)

// fakeBackend is the minimal backend.Backend these tests need — every
// method beyond ID/Capabilities panics, so an accidental dependency on
// unmocked behaviour fails loudly rather than silently.
type fakeBackend struct {
	compacted []string // sessionIDs Compact was called with
}

func (f *fakeBackend) ID() string                         { return "fake" }
func (f *fakeBackend) Version() string                    { return "0.0.0" }
func (f *fakeBackend) Capabilities() backend.Capabilities { return backend.Capabilities{Compact: true} }
func (f *fakeBackend) ListSessions(context.Context, string) ([]backend.Session, error) {
	panic("not used by these tests")
}
func (f *fakeBackend) GetSession(context.Context, string, string) (backend.Session, error) {
	panic("not used by these tests")
}
func (f *fakeBackend) CreateSession(context.Context, string, backend.CreateSessionOptions) (backend.Session, error) {
	panic("not used by these tests")
}
func (f *fakeBackend) RenameSession(context.Context, string, string, string) (backend.Session, error) {
	panic("not used by these tests")
}
func (f *fakeBackend) DeleteSession(context.Context, string, string) error {
	panic("not used by these tests")
}
func (f *fakeBackend) Prompt(context.Context, string, string, backend.Prompt) error {
	panic("not used by these tests")
}
func (f *fakeBackend) Cancel(context.Context, string, string) error {
	panic("not used by these tests")
}
func (f *fakeBackend) SetMode(context.Context, string, string, string) (backend.Session, error) {
	panic("not used by these tests")
}
func (f *fakeBackend) SetModel(context.Context, string, string, string) (backend.Session, error) {
	panic("not used by these tests")
}
func (f *fakeBackend) ReplyPermission(context.Context, string, string, string, backend.Decision, string) error {
	panic("not used by these tests")
}
func (f *fakeBackend) Modes(context.Context, string) ([]backend.Mode, error) {
	panic("not used by these tests")
}
func (f *fakeBackend) Models(context.Context, string) ([]backend.Model, error) {
	panic("not used by these tests")
}
func (f *fakeBackend) Transcript(context.Context, string, string) (backend.Transcript, error) {
	return backend.Transcript{}, nil
}
func (f *fakeBackend) Subscribe(context.Context) (<-chan backend.BackendEvent, error) {
	panic("not used by these tests")
}
func (f *fakeBackend) Compact(_ context.Context, _ string, sessionID string) error {
	f.compacted = append(f.compacted, sessionID)
	return nil
}

var _ backend.Backend = (*fakeBackend)(nil)
var _ backend.Compactor = (*fakeBackend)(nil)

// fakeBackendNoCompact is the same floor, minus Compact — Go embedding
// cannot "remove" a promoted method (shadowing it would still satisfy
// backend.Compactor), so this is a separate type rather than wrapping
// fakeBackend. session.compact against it must answer `unsupported`
// (PROTOCOL.md §6), never call through.
type fakeBackendNoCompact struct{}

func (f *fakeBackendNoCompact) ID() string                         { return "fake-no-compact" }
func (f *fakeBackendNoCompact) Version() string                    { return "0.0.0" }
func (f *fakeBackendNoCompact) Capabilities() backend.Capabilities { return backend.Capabilities{} }
func (f *fakeBackendNoCompact) ListSessions(context.Context, string) ([]backend.Session, error) {
	panic("not used by these tests")
}
func (f *fakeBackendNoCompact) GetSession(context.Context, string, string) (backend.Session, error) {
	panic("not used by these tests")
}
func (f *fakeBackendNoCompact) CreateSession(context.Context, string, backend.CreateSessionOptions) (backend.Session, error) {
	panic("not used by these tests")
}
func (f *fakeBackendNoCompact) RenameSession(context.Context, string, string, string) (backend.Session, error) {
	panic("not used by these tests")
}
func (f *fakeBackendNoCompact) DeleteSession(context.Context, string, string) error {
	panic("not used by these tests")
}
func (f *fakeBackendNoCompact) Prompt(context.Context, string, string, backend.Prompt) error {
	panic("not used by these tests")
}
func (f *fakeBackendNoCompact) Cancel(context.Context, string, string) error {
	panic("not used by these tests")
}
func (f *fakeBackendNoCompact) SetMode(context.Context, string, string, string) (backend.Session, error) {
	panic("not used by these tests")
}
func (f *fakeBackendNoCompact) SetModel(context.Context, string, string, string) (backend.Session, error) {
	panic("not used by these tests")
}
func (f *fakeBackendNoCompact) ReplyPermission(context.Context, string, string, string, backend.Decision, string) error {
	panic("not used by these tests")
}
func (f *fakeBackendNoCompact) Modes(context.Context, string) ([]backend.Mode, error) {
	panic("not used by these tests")
}
func (f *fakeBackendNoCompact) Models(context.Context, string) ([]backend.Model, error) {
	panic("not used by these tests")
}
func (f *fakeBackendNoCompact) Transcript(context.Context, string, string) (backend.Transcript, error) {
	return backend.Transcript{}, nil
}
func (f *fakeBackendNoCompact) Subscribe(context.Context) (<-chan backend.BackendEvent, error) {
	panic("not used by these tests")
}

var _ backend.Backend = (*fakeBackendNoCompact)(nil)

func newTestMachine(t *testing.T, back backend.Backend) *machine {
	t.Helper()
	dir := t.TempDir()
	reg, err := workspaces.Load(dir + "/workspaces.json")
	if err != nil {
		t.Fatal(err)
	}
	pol := policy.Default()
	mat := sessions.New(back, pol)
	return newMachine(reg, back, mat, pol)
}

// trackTestSession registers a session against a real workspace directory
// (Create requires one to exist), the same bookkeeping session.create does.
func trackTestSession(t *testing.T, mc *machine, sessionID string) {
	t.Helper()
	dir := t.TempDir()
	w, err := mc.workspaces.Create("ws", dir, nil)
	if err != nil {
		t.Fatal(err)
	}
	mc.trackSession(w, backend.Session{ID: sessionID})
}

func TestOpSessionCompact(t *testing.T) {
	back := &fakeBackend{}
	mc := newTestMachine(t, back)
	trackTestSession(t, mc, "s1")

	res, operr := mc.Handle(context.Background(), "session.compact", json.RawMessage(`{"sessionId":"s1"}`))
	if operr != nil {
		t.Fatalf("unexpected error: %+v", operr)
	}
	if _, ok := res.(map[string]any); !ok {
		t.Fatalf("result = %#v, want an empty object", res)
	}
	if len(back.compacted) != 1 || back.compacted[0] != "s1" {
		t.Fatalf("Compact called with %v, want [s1]", back.compacted)
	}
}

// A backend that does not implement backend.Compactor must refuse with
// `unsupported`, matching session.diff/session.children's own capability
// checks — never a panic, never a silent no-op.
func TestOpSessionCompactUnsupportedBackend(t *testing.T) {
	back := &fakeBackendNoCompact{}
	mc := newTestMachine(t, back)
	trackTestSession(t, mc, "s1")

	_, operr := mc.Handle(context.Background(), "session.compact", json.RawMessage(`{"sessionId":"s1"}`))
	if operr == nil {
		t.Fatal("expected an OpError")
	}
	if operr.Code != "unsupported" {
		t.Fatalf("code = %q, want unsupported", operr.Code)
	}
}

func TestOpSessionCompactUnknownSession(t *testing.T) {
	back := &fakeBackend{}
	mc := newTestMachine(t, back)

	_, operr := mc.Handle(context.Background(), "session.compact", json.RawMessage(`{"sessionId":"nope"}`))
	if operr == nil || operr.Code != "not_found" {
		t.Fatalf("operr = %+v, want not_found", operr)
	}
}
