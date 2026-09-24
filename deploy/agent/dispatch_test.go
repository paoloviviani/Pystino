package main

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
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

// fakeBackendAsker adds backend.Asker on top of fakeBackend's floor, for the
// question.reply tests: embedding promotes every other method, so this type
// only has to record the two calls it exists to test.
type fakeBackendAsker struct {
	*fakeBackend
	replied  []string
	answers  [][][]string
	rejected []string
}

func (f *fakeBackendAsker) ReplyQuestion(_ context.Context, _, _, requestID string, answers [][]string) error {
	f.replied = append(f.replied, requestID)
	f.answers = append(f.answers, answers)
	return nil
}

func (f *fakeBackendAsker) RejectQuestion(_ context.Context, _, _, requestID string) error {
	f.rejected = append(f.rejected, requestID)
	return nil
}

var _ backend.Asker = (*fakeBackendAsker)(nil)

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

// question.reply's "answer" decision calls ReplyQuestion with the answers in
// order (the user-question tool design).
func TestOpQuestionReplyAnswer(t *testing.T) {
	back := &fakeBackendAsker{fakeBackend: &fakeBackend{}}
	mc := newTestMachine(t, back)
	trackTestSession(t, mc, "s1")

	res, operr := mc.Handle(context.Background(), "question.reply", json.RawMessage(
		`{"sessionId":"s1","requestId":"que_1","decision":"answer","answers":[["A"],["X","Y"]]}`,
	))
	if operr != nil {
		t.Fatalf("unexpected error: %+v", operr)
	}
	if _, ok := res.(map[string]any); !ok {
		t.Fatalf("result = %#v, want an empty object", res)
	}
	if len(back.replied) != 1 || back.replied[0] != "que_1" {
		t.Fatalf("replied = %v, want [que_1]", back.replied)
	}
	if len(back.answers) != 1 || len(back.answers[0]) != 2 || back.answers[0][0][0] != "A" {
		t.Fatalf("answers = %+v", back.answers)
	}
	if len(back.rejected) != 0 {
		t.Fatalf("rejected = %v, want none", back.rejected)
	}
}

// question.reply's "reject" decision calls RejectQuestion, not ReplyQuestion.
func TestOpQuestionReplyReject(t *testing.T) {
	back := &fakeBackendAsker{fakeBackend: &fakeBackend{}}
	mc := newTestMachine(t, back)
	trackTestSession(t, mc, "s1")

	_, operr := mc.Handle(context.Background(), "question.reply", json.RawMessage(
		`{"sessionId":"s1","requestId":"que_2","decision":"reject"}`,
	))
	if operr != nil {
		t.Fatalf("unexpected error: %+v", operr)
	}
	if len(back.rejected) != 1 || back.rejected[0] != "que_2" {
		t.Fatalf("rejected = %v, want [que_2]", back.rejected)
	}
	if len(back.replied) != 0 {
		t.Fatalf("replied = %v, want none", back.replied)
	}
}

// A backend that does not implement backend.Asker must refuse with
// `unsupported` (ACP: PROTOCOL.md/the task's "ACP reports questions: false").
func TestOpQuestionReplyUnsupportedBackend(t *testing.T) {
	back := &fakeBackend{}
	mc := newTestMachine(t, back)
	trackTestSession(t, mc, "s1")

	_, operr := mc.Handle(context.Background(), "question.reply", json.RawMessage(
		`{"sessionId":"s1","requestId":"que_1","decision":"answer","answers":[["A"]]}`,
	))
	if operr == nil || operr.Code != "unsupported" {
		t.Fatalf("operr = %+v, want unsupported", operr)
	}
}

func TestOpQuestionReplyInvalidDecision(t *testing.T) {
	back := &fakeBackendAsker{fakeBackend: &fakeBackend{}}
	mc := newTestMachine(t, back)
	trackTestSession(t, mc, "s1")

	_, operr := mc.Handle(context.Background(), "question.reply", json.RawMessage(
		`{"sessionId":"s1","requestId":"que_1","decision":"maybe"}`,
	))
	if operr == nil || operr.Code != "invalid" {
		t.Fatalf("operr = %+v, want invalid", operr)
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

func newTestMachineWithRoots(t *testing.T, back backend.Backend, roots []string) *machine {
	t.Helper()
	dir := t.TempDir()
	reg, err := workspaces.Load(dir + "/workspaces.json")
	if err != nil {
		t.Fatal(err)
	}
	pol := policy.Policy{WorkspaceRoots: roots}
	mat := sessions.New(back, pol)
	return newMachine(reg, back, mat, pol)
}

func TestOpWorkspaceSuggest(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "workspace-a"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(dir, "other"), 0o755); err != nil {
		t.Fatal(err)
	}
	mc := newTestMachineWithRoots(t, &fakeBackend{}, []string{dir})

	args, err := json.Marshal(map[string]string{"prefix": filepath.Join(dir, "works")})
	if err != nil {
		t.Fatal(err)
	}
	res, operr := mc.Handle(context.Background(), "workspace.suggest", args)
	if operr != nil {
		t.Fatalf("unexpected error: %+v", operr)
	}
	dirs, ok := res.(map[string]any)["directories"].([]workspaces.Directory)
	if !ok || len(dirs) != 1 || dirs[0].Name != "workspace-a" {
		t.Fatalf("directories = %#v", res.(map[string]any)["directories"])
	}
}

// initGitRepo makes an initialized git repo at dir with one commit, so
// `git worktree add` has a HEAD to branch from.
func initGitRepo(t *testing.T, dir string) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	run := func(args ...string) {
		cmd := exec.Command("git", append([]string{"-C", dir}, args...)...)
		cmd.Env = append(os.Environ(),
			"GIT_AUTHOR_NAME=test", "GIT_AUTHOR_EMAIL=test@example.com",
			"GIT_COMMITTER_NAME=test", "GIT_COMMITTER_EMAIL=test@example.com",
		)
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git %v: %v\n%s", args, err, out)
		}
	}
	run("init", "-q", "-b", "main")
	if err := os.WriteFile(filepath.Join(dir, "README"), []byte("hi\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	run("add", "README")
	run("commit", "-q", "-m", "initial")
}

func TestOpWorkspaceCreateWorktree(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not installed")
	}
	dir := t.TempDir()
	repo := filepath.Join(dir, "proj")
	initGitRepo(t, repo)
	mc := newTestMachineWithRoots(t, &fakeBackend{}, nil)

	createArgs, _ := json.Marshal(map[string]string{"path": repo})
	res, operr := mc.Handle(context.Background(), "workspace.create", createArgs)
	if operr != nil {
		t.Fatalf("unexpected error: %+v", operr)
	}
	from := res.(map[string]any)["workspace"].(workspaces.Workspace)
	if !from.IsGitRepo {
		t.Fatal("repo workspace must report isGitRepo: true")
	}

	wtArgs, _ := json.Marshal(map[string]any{
		"worktree": map[string]string{"from": from.ID, "branch": "feature/x"},
	})
	res2, operr2 := mc.Handle(context.Background(), "workspace.create", wtArgs)
	if operr2 != nil {
		t.Fatalf("unexpected error: %+v", operr2)
	}
	w := res2.(map[string]any)["workspace"].(workspaces.Workspace)
	if w.WorktreeOf != from.ID || w.Branch != "feature/x" {
		t.Fatalf("worktree workspace = %+v", w)
	}
	if info, err := os.Stat(w.Path); err != nil || !info.IsDir() {
		t.Fatalf("worktree directory missing at %q: %v", w.Path, err)
	}
}

func TestOpWorkspaceArchiveRemoveWorktree(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not installed")
	}
	dir := t.TempDir()
	repo := filepath.Join(dir, "proj")
	initGitRepo(t, repo)
	mc := newTestMachineWithRoots(t, &fakeBackend{}, nil)

	createArgs, _ := json.Marshal(map[string]string{"path": repo})
	res, operr := mc.Handle(context.Background(), "workspace.create", createArgs)
	if operr != nil {
		t.Fatalf("unexpected error: %+v", operr)
	}
	from := res.(map[string]any)["workspace"].(workspaces.Workspace)

	wtArgs, _ := json.Marshal(map[string]any{
		"worktree": map[string]string{"from": from.ID, "branch": "feature"},
	})
	res2, operr2 := mc.Handle(context.Background(), "workspace.create", wtArgs)
	if operr2 != nil {
		t.Fatalf("unexpected error: %+v", operr2)
	}
	w := res2.(map[string]any)["workspace"].(workspaces.Workspace)

	archiveArgs, _ := json.Marshal(map[string]any{"workspaceId": w.ID, "removeWorktree": true})
	if _, operr := mc.Handle(context.Background(), "workspace.archive", archiveArgs); operr != nil {
		t.Fatalf("unexpected error: %+v", operr)
	}
	if _, err := os.Stat(w.Path); !os.IsNotExist(err) {
		t.Errorf("worktree directory must be gone, stat err = %v", err)
	}

	// removeWorktree against a workspace that isn't a worktree must refuse
	// rather than silently doing nothing to the source repo.
	archiveArgs2, _ := json.Marshal(map[string]any{"workspaceId": from.ID, "removeWorktree": true})
	_, operr3 := mc.Handle(context.Background(), "workspace.archive", archiveArgs2)
	if operr3 == nil || operr3.Code != "invalid" {
		t.Fatalf("operr = %+v, want invalid", operr3)
	}
}
