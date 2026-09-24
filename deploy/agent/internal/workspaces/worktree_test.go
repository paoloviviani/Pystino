package workspaces

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

// newTestRepo makes an initialized git repo with one commit at dir/name,
// so `git worktree add` has a HEAD to branch from.
func newTestRepo(t *testing.T, dir, name string) string {
	t.Helper()
	repo := filepath.Join(dir, name)
	if err := os.MkdirAll(repo, 0o755); err != nil {
		t.Fatal(err)
	}
	run := func(args ...string) {
		cmd := exec.Command("git", append([]string{"-C", repo}, args...)...)
		cmd.Env = append(os.Environ(),
			"GIT_AUTHOR_NAME=test", "GIT_AUTHOR_EMAIL=test@example.com",
			"GIT_COMMITTER_NAME=test", "GIT_COMMITTER_EMAIL=test@example.com",
		)
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git %v: %v\n%s", args, err, out)
		}
	}
	run("init", "-q", "-b", "main")
	if err := os.WriteFile(filepath.Join(repo, "README"), []byte("hi\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	run("add", "README")
	run("commit", "-q", "-m", "initial")
	return repo
}

func newTestRegistry(t *testing.T) *Registry {
	t.Helper()
	r, err := Load(filepath.Join(t.TempDir(), "workspaces.json"))
	if err != nil {
		t.Fatal(err)
	}
	return r
}

func TestCreateWorktreeRoundTrip(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not installed")
	}
	dir := t.TempDir()
	repo := newTestRepo(t, dir, "proj")

	r := newTestRegistry(t)
	from, err := r.Create("proj", repo, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !from.IsGitRepo {
		t.Error("a repo's workspace must report isGitRepo: true")
	}

	w, err := r.CreateWorktree(context.Background(), from, "feature/x", "", nil)
	if err != nil {
		t.Fatal(err)
	}
	if w.WorktreeOf != from.ID {
		t.Errorf("WorktreeOf = %q, want %q", w.WorktreeOf, from.ID)
	}
	if w.Branch != "feature/x" {
		t.Errorf("Branch = %q, want feature/x", w.Branch)
	}
	wantPath := repo + ".worktrees/feature/x"
	if w.Path != wantPath {
		t.Errorf("Path = %q, want %q", w.Path, wantPath)
	}
	if info, err := os.Stat(w.Path); err != nil || !info.IsDir() {
		t.Fatalf("worktree directory missing at %q: %v", w.Path, err)
	}
	if !w.IsGitRepo {
		t.Error("a worktree's own workspace must report isGitRepo: true")
	}

	// The registry survives a reload with the worktree metadata intact.
	r2, err := Load(r.path)
	if err != nil {
		t.Fatal(err)
	}
	got, ok := r2.Get(w.ID)
	if !ok || got.WorktreeOf != from.ID || got.Branch != "feature/x" {
		t.Errorf("after reload: %+v", got)
	}
}

func TestCreateWorktreeRejectsDotDotBranch(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not installed")
	}
	dir := t.TempDir()
	repo := newTestRepo(t, dir, "proj")
	r := newTestRegistry(t)
	from, err := r.Create("proj", repo, nil)
	if err != nil {
		t.Fatal(err)
	}

	if _, err := r.CreateWorktree(context.Background(), from, "../escape", "", nil); err == nil {
		t.Fatal("a branch name containing .. must be refused")
	}
	if _, err := r.CreateWorktree(context.Background(), from, "a/../../b", "", nil); err == nil {
		t.Fatal("a branch name containing .. anywhere must be refused")
	}
}

func TestCreateWorktreeRejectsNonRepo(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not installed")
	}
	dir := t.TempDir()
	plain := filepath.Join(dir, "plain")
	if err := os.MkdirAll(plain, 0o755); err != nil {
		t.Fatal(err)
	}
	r := newTestRegistry(t)
	from, err := r.Create("plain", plain, nil)
	if err != nil {
		t.Fatal(err)
	}
	if from.IsGitRepo {
		t.Error("a non-repo workspace must report isGitRepo: false")
	}
	if _, err := r.CreateWorktree(context.Background(), from, "feature", "", nil); err == nil {
		t.Fatal("CreateWorktree against a non-repo workspace must fail")
	}
}

func TestCreateWorktreeEnforcesWorkspaceRoots(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not installed")
	}
	dir := t.TempDir()
	repo := newTestRepo(t, dir, "proj")
	r := newTestRegistry(t)
	from, err := r.Create("proj", repo, nil)
	if err != nil {
		t.Fatal(err)
	}

	// The worktree lands beside the repo, at <repo>.worktrees/<branch> —
	// outside a root that only covers the repo itself.
	if _, err := r.CreateWorktree(context.Background(), from, "feature", "", []string{repo}); err == nil {
		t.Fatal("a worktree path outside every configured workspaceRoot must be refused")
	}
	// And it must not leave an orphaned git worktree behind on refusal.
	out, err := exec.Command("git", "-C", repo, "worktree", "list").CombinedOutput()
	if err != nil {
		t.Fatal(err)
	}
	if got := string(out); len(got) == 0 {
		t.Fatal("expected at least the main worktree in the list")
	}
	if r2, err := Load(r.path); err != nil || len(r2.List(true)) != 1 {
		t.Errorf("registry must still hold only the original workspace after a refused worktree: %v, err=%v", r2, err)
	}
}

func TestRemoveWorktree(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not installed")
	}
	dir := t.TempDir()
	repo := newTestRepo(t, dir, "proj")
	r := newTestRegistry(t)
	from, err := r.Create("proj", repo, nil)
	if err != nil {
		t.Fatal(err)
	}
	w, err := r.CreateWorktree(context.Background(), from, "feature", "", nil)
	if err != nil {
		t.Fatal(err)
	}

	if err := RemoveWorktree(context.Background(), from.Path, w.Path, false); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(w.Path); !os.IsNotExist(err) {
		t.Errorf("worktree directory must be gone, stat err = %v", err)
	}
}

func TestRemoveWorktreeRefusesDirtyWithoutForce(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not installed")
	}
	dir := t.TempDir()
	repo := newTestRepo(t, dir, "proj")
	r := newTestRegistry(t)
	from, err := r.Create("proj", repo, nil)
	if err != nil {
		t.Fatal(err)
	}
	w, err := r.CreateWorktree(context.Background(), from, "feature", "", nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(w.Path, "untracked.txt"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}

	if err := RemoveWorktree(context.Background(), from.Path, w.Path, false); err == nil {
		t.Fatal("a dirty worktree must be refused without force")
	}
	if err := RemoveWorktree(context.Background(), from.Path, w.Path, true); err != nil {
		t.Fatalf("force must remove a dirty worktree: %v", err)
	}
}
