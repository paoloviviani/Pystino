package sessions

import (
	"context"
	"testing"
	"time"

	"pystino-agent/internal/backend"
	"pystino-agent/internal/policy"
)

// fakeBackend is the minimal backend.Backend a materializer test needs:
// Transcript is settable per session (for the seeding tests), everything
// else is unused by these tests and panics if called, so an accidental
// dependency on unmocked behaviour fails loudly instead of silently.
type fakeBackend struct {
	transcripts map[string]backend.Transcript
	replies     []replyCall
}

type replyCall struct {
	sessionID string
	requestID string
	decision  backend.Decision
}

func newFakeBackend() *fakeBackend {
	return &fakeBackend{transcripts: map[string]backend.Transcript{}}
}

func (f *fakeBackend) ID() string                         { return "fake" }
func (f *fakeBackend) Version() string                    { return "0.0.0" }
func (f *fakeBackend) Capabilities() backend.Capabilities { return backend.Capabilities{} }
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
func (f *fakeBackend) ReplyPermission(_ context.Context, _ string, sessionID, requestID string, decision backend.Decision, _ string) error {
	f.replies = append(f.replies, replyCall{sessionID: sessionID, requestID: requestID, decision: decision})
	return nil
}
func (f *fakeBackend) Modes(context.Context, string) ([]backend.Mode, error) {
	panic("not used by these tests")
}
func (f *fakeBackend) Models(context.Context, string) ([]backend.Model, error) {
	panic("not used by these tests")
}
func (f *fakeBackend) Transcript(_ context.Context, _ string, sessionID string) (backend.Transcript, error) {
	return f.transcripts[sessionID], nil
}
func (f *fakeBackend) Subscribe(context.Context) (<-chan backend.BackendEvent, error) {
	panic("not used by these tests: they drive ApplyBackendEvent directly")
}

func textPart(msgID, partID, text string) backend.Event {
	return backend.Event{
		Kind: backend.EventPart,
		Part: &backend.Part{ID: partID, MessageID: msgID, Role: "assistant", Type: backend.PartText, Text: text},
	}
}

// TestTextContract pins PROTOCOL.md §7: the first sighting of a part is a
// full upsert; later growth becomes a suffix delta; a stale (shorter)
// resend is dropped; a genuinely different resend re-baselines as a fresh
// upsert — and concat(first text, deltas...) always equals the latest text.
func TestTextContract(t *testing.T) {
	m := New(newFakeBackend(), policy.Default())
	m.Track("/ws", backend.Session{ID: "s1"})
	ctx := context.Background()
	apply := func(ev backend.Event) {
		m.ApplyBackendEvent(ctx, backend.BackendEvent{WorkspaceDir: "/ws", SessionID: "s1", Event: ev})
	}

	apply(textPart("m1", "p1", "Hello"))
	apply(textPart("m1", "p1", "Hello world"))
	apply(textPart("m1", "p1", "Hello wor")) // stale, shorter resend: must be dropped
	apply(textPart("m1", "p1", "Hello world!"))
	apply(textPart("m1", "p1", "Hello world!")) // exact resend of current text: no new info

	res, err := m.Sync(ctx, "s1", m.Epoch(), 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Events) == 0 {
		t.Fatal("expected buffered events")
	}

	var kinds []backend.EventKind
	assembled := ""
	for i, env := range res.Events {
		if env.Seq != int64(i+1) {
			t.Fatalf("seq[%d] = %d, want %d (strictly increasing, no gaps)", i, env.Seq, i+1)
		}
		kinds = append(kinds, env.Event.Kind)
		switch env.Event.Kind {
		case backend.EventPart:
			assembled = env.Event.Part.Text
		case backend.EventDelta:
			assembled += env.Event.Delta
		}
	}
	if assembled != "Hello world!" {
		t.Fatalf("assembled text = %q, want %q", assembled, "Hello world!")
	}
	// Exactly 3 emissions expected: the first upsert, one delta for the
	// " world" growth, one delta for the "!" growth. The stale shorter
	// resend and the exact-duplicate resend must produce nothing.
	wantKinds := []backend.EventKind{backend.EventPart, backend.EventDelta, backend.EventDelta}
	if len(kinds) != len(wantKinds) {
		t.Fatalf("emitted %d events %v, want %v", len(kinds), kinds, wantKinds)
	}
	for i := range wantKinds {
		if kinds[i] != wantKinds[i] {
			t.Errorf("event[%d].Kind = %q, want %q", i, kinds[i], wantKinds[i])
		}
	}
}

// TestSyncAfterGap pins that a client resuming from an afterSeq the ring
// still holds gets exactly the missing tail, with seq strictly increasing
// and no gaps across the whole run.
func TestSyncAfterGap(t *testing.T) {
	m := New(newFakeBackend(), policy.Default())
	m.Track("/ws", backend.Session{ID: "s1"})
	ctx := context.Background()

	for i := 0; i < 5; i++ {
		m.ApplyBackendEvent(ctx, backend.BackendEvent{
			WorkspaceDir: "/ws", SessionID: "s1",
			Event: backend.Event{Kind: backend.EventStatus, Status: backend.StatusBusy},
		})
	}

	res, err := m.Sync(ctx, "s1", m.Epoch(), 2)
	if err != nil {
		t.Fatal(err)
	}
	if res.Snapshot != nil {
		t.Fatal("a gap the ring still holds must not fall back to a snapshot")
	}
	if len(res.Events) != 3 {
		t.Fatalf("got %d events after afterSeq=2 of 5, want 3", len(res.Events))
	}
	for i, env := range res.Events {
		wantSeq := int64(3 + i)
		if env.Seq != wantSeq {
			t.Errorf("event[%d].Seq = %d, want %d", i, env.Seq, wantSeq)
		}
	}
	if res.Seq != 5 {
		t.Errorf("res.Seq = %d, want 5", res.Seq)
	}
}

// TestSeqStrictlyIncreasingAcrossSessions pins that seq is per-session
// (starts at 1, increases by exactly 1 per emission) and independent
// between sessions — one session's traffic never perturbs another's
// numbering.
func TestSeqStrictlyIncreasingAcrossSessions(t *testing.T) {
	m := New(newFakeBackend(), policy.Default())
	m.Track("/ws", backend.Session{ID: "s1"})
	m.Track("/ws", backend.Session{ID: "s2"})
	ctx := context.Background()

	emit := func(sessionID string) {
		m.ApplyBackendEvent(ctx, backend.BackendEvent{
			WorkspaceDir: "/ws", SessionID: sessionID,
			Event: backend.Event{Kind: backend.EventStatus, Status: backend.StatusIdle},
		})
	}
	emit("s1")
	emit("s2")
	emit("s1")
	emit("s1")
	emit("s2")

	res1, err := m.Sync(ctx, "s1", m.Epoch(), 0)
	if err != nil {
		t.Fatal(err)
	}
	if res1.Seq != 3 {
		t.Fatalf("s1 seq = %d, want 3", res1.Seq)
	}
	for i, env := range res1.Events {
		if env.Seq != int64(i+1) {
			t.Errorf("s1 event[%d].Seq = %d, want %d", i, env.Seq, i+1)
		}
	}

	res2, err := m.Sync(ctx, "s2", m.Epoch(), 0)
	if err != nil {
		t.Fatal(err)
	}
	if res2.Seq != 2 {
		t.Fatalf("s2 seq = %d, want 2", res2.Seq)
	}
}

// TestSyncEpochMismatchFallsBackToSnapshot pins that a client presenting a
// stale epoch (a prior process run) gets a full snapshot, seeded from the
// backend's persisted transcript, never a diff against buffered events that
// belong to a different epoch.
func TestSyncEpochMismatchFallsBackToSnapshot(t *testing.T) {
	fb := newFakeBackend()
	completedAt := time.Now()
	fb.transcripts["s1"] = backend.Transcript{
		Messages: []backend.TranscriptEntry{{
			Message: backend.Message{ID: "m1", Role: "assistant", CompletedAt: &completedAt},
			Parts:   []backend.Part{{ID: "p1", MessageID: "m1", Type: backend.PartText, Text: "from before restart"}},
		}},
		Status: backend.StatusIdle,
	}
	m := New(fb, policy.Default())
	m.Track("/ws", backend.Session{ID: "s1"})
	ctx := context.Background()

	// Some live activity happens in this (new) epoch too.
	m.ApplyBackendEvent(ctx, backend.BackendEvent{
		WorkspaceDir: "/ws", SessionID: "s1",
		Event: textPart("m2", "p2", "live text"),
	})

	res, err := m.Sync(ctx, "s1", "some-other-process's-epoch", 5)
	if err != nil {
		t.Fatal(err)
	}
	if res.Events != nil {
		t.Fatal("an epoch mismatch must never return an events tail")
	}
	if res.Epoch != m.Epoch() {
		t.Errorf("returned epoch = %q, want the current epoch %q", res.Epoch, m.Epoch())
	}
	if res.Snapshot == nil {
		t.Fatal("an epoch mismatch must return a snapshot")
	}
	if len(res.Snapshot.Messages) != 2 {
		t.Fatalf("snapshot has %d messages, want 2 (seeded + live)", len(res.Snapshot.Messages))
	}
	if res.Snapshot.Messages[0].Message.ID != "m1" {
		t.Errorf("snapshot message order lost the seeded (older) message first: %+v", res.Snapshot.Messages)
	}
	if res.Snapshot.Messages[1].Parts[0].Text != "live text" {
		t.Errorf("snapshot lost live activity: %+v", res.Snapshot.Messages[1])
	}
}

// TestSyncNoPriorEpochIsSnapshot pins that a client's first-ever sync (no
// epoch to present at all) always gets a snapshot, not an attempt to diff
// against an epoch it never had.
func TestSyncNoPriorEpochIsSnapshot(t *testing.T) {
	m := New(newFakeBackend(), policy.Default())
	m.Track("/ws", backend.Session{ID: "s1"})
	ctx := context.Background()
	m.ApplyBackendEvent(ctx, backend.BackendEvent{WorkspaceDir: "/ws", SessionID: "s1", Event: textPart("m1", "p1", "hi")})

	res, err := m.Sync(ctx, "s1", "", 0)
	if err != nil {
		t.Fatal(err)
	}
	if res.Snapshot == nil || res.Events != nil {
		t.Fatalf("first sync with no epoch must be a snapshot, got %+v", res)
	}
}

// TestAutoAcceptRepliesOnceAndMarksAuto pins auto-accept (PROTOCOL.md §7):
// when enabled and permitted by policy, a permission.asked never reaches
// the client as an ask — the backend is told "once" and a
// permission.replied by:"auto" is emitted instead.
func TestAutoAcceptRepliesOnceAndMarksAuto(t *testing.T) {
	fb := newFakeBackend()
	m := New(fb, policy.Policy{AutoAccept: policy.AutoAcceptAllowed})
	m.Track("/ws", backend.Session{ID: "s1"})
	ctx := context.Background()
	if err := m.SetAutoAccept("s1", true); err != nil {
		t.Fatal(err)
	}

	m.ApplyBackendEvent(ctx, backend.BackendEvent{
		WorkspaceDir: "/ws", SessionID: "s1",
		Event: backend.Event{Kind: backend.EventPermissionAsked, Request: &backend.PermissionRequest{ID: "perm1", SessionID: "s1", Tool: "bash"}},
	})

	if len(fb.replies) != 1 || fb.replies[0].requestID != "perm1" || fb.replies[0].decision != backend.DecisionOnce {
		t.Fatalf("backend replies = %+v, want one 'once' reply to perm1", fb.replies)
	}
	res, err := m.Sync(ctx, "s1", m.Epoch(), 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Events) != 1 || res.Events[0].Event.Kind != backend.EventPermissionReplied || res.Events[0].Event.By != "auto" {
		t.Fatalf("events = %+v, want exactly one permission.replied by=auto (no permission.asked)", res.Events)
	}
	if m.PendingPermissions("s1") != 0 {
		t.Errorf("pending permissions = %d, want 0 after auto-accept", m.PendingPermissions("s1"))
	}
}

// TestSetAutoAcceptForbiddenByPolicy pins the machine's veto: denied policy
// refuses session.setAutoAccept outright, regardless of what's asked.
func TestSetAutoAcceptForbiddenByPolicy(t *testing.T) {
	m := New(newFakeBackend(), policy.Default())
	m.Track("/ws", backend.Session{ID: "s1"})
	if err := m.SetAutoAccept("s1", true); err != ErrAutoAcceptForbidden {
		t.Fatalf("err = %v, want ErrAutoAcceptForbidden", err)
	}
	if m.AutoAccept("s1") {
		t.Error("auto-accept must not have been enabled")
	}
}

// TestPermissionAskedWithoutAutoAcceptIsForwardedAndCounted pins the
// non-auto-accept path: the ask reaches the client as-is and counts toward
// PendingPermissions until replied.
func TestPermissionAskedWithoutAutoAcceptIsForwardedAndCounted(t *testing.T) {
	m := New(newFakeBackend(), policy.Default())
	m.Track("/ws", backend.Session{ID: "s1"})
	ctx := context.Background()
	m.ApplyBackendEvent(ctx, backend.BackendEvent{
		WorkspaceDir: "/ws", SessionID: "s1",
		Event: backend.Event{Kind: backend.EventPermissionAsked, Request: &backend.PermissionRequest{ID: "perm1", SessionID: "s1"}},
	})
	if m.PendingPermissions("s1") != 1 {
		t.Fatalf("pending permissions = %d, want 1", m.PendingPermissions("s1"))
	}
	res, err := m.Sync(ctx, "s1", m.Epoch(), 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Events) != 1 || res.Events[0].Event.Kind != backend.EventPermissionAsked {
		t.Fatalf("events = %+v, want the ask forwarded", res.Events)
	}

	m.ApplyBackendEvent(ctx, backend.BackendEvent{
		WorkspaceDir: "/ws", SessionID: "s1",
		Event: backend.Event{Kind: backend.EventPermissionReplied, RequestID: "perm1", Decision: backend.DecisionReject, By: "user"},
	})
	if m.PendingPermissions("s1") != 0 {
		t.Errorf("pending permissions = %d after reply, want 0", m.PendingPermissions("s1"))
	}
}

func TestSyncUnknownSession(t *testing.T) {
	m := New(newFakeBackend(), policy.Default())
	if _, err := m.Sync(context.Background(), "nope", "", 0); err != ErrUnknownSession {
		t.Fatalf("err = %v, want ErrUnknownSession", err)
	}
}
