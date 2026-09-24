package policy

import (
	"os"
	"path/filepath"
	"testing"
)

func TestDefaultIsClosed(t *testing.T) {
	p := Default()
	if p.AutoAcceptAllowed() {
		t.Error("default policy must deny auto-accept")
	}
	if p.AllowFreeModels {
		t.Error("default policy must not allow free models")
	}
}

func TestLoadMissingFileIsDefault(t *testing.T) {
	p, err := Load(filepath.Join(t.TempDir(), "no-such-policy.json"))
	if err != nil {
		t.Fatalf("Load of a missing file must not error: %v", err)
	}
	if p.AutoAccept != AutoAcceptDenied {
		t.Errorf("AutoAccept = %q, want denied", p.AutoAccept)
	}
}

func TestSaveLoadRoundTrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "policy.json")
	want := Policy{AutoAccept: AutoAcceptAllowed, WorkspaceRoots: []string{"/srv/code"}, AllowFreeModels: true}
	if err := Save(path, want); err != nil {
		t.Fatal(err)
	}
	got, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if got.AutoAccept != want.AutoAccept || got.AllowFreeModels != want.AllowFreeModels ||
		!equalStrings(got.WorkspaceRoots, want.WorkspaceRoots) {
		t.Errorf("round trip mismatch: got %+v, want %+v", got, want)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Errorf("policy.json mode = %v, want 0600", info.Mode().Perm())
	}
}

func TestAllowedEmptyRootsIsUnrestricted(t *testing.T) {
	ok, err := Allowed(nil, t.TempDir())
	if err != nil || !ok {
		t.Fatalf("empty roots must allow anything: ok=%v err=%v", ok, err)
	}
}

func TestAllowedPrefixAndSymlink(t *testing.T) {
	root := t.TempDir()
	sub := filepath.Join(root, "project")
	if err := os.Mkdir(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	outside := t.TempDir()

	ok, err := Allowed([]string{root}, sub)
	if err != nil || !ok {
		t.Fatalf("a subdirectory of an allowed root must pass: ok=%v err=%v", ok, err)
	}
	ok, err = Allowed([]string{root}, outside)
	if err != nil || ok {
		t.Fatalf("a directory outside every root must fail: ok=%v err=%v", ok, err)
	}

	// A symlink inside the root that points outside it must resolve to its
	// real target and be refused — a bare string prefix check on the
	// unresolved path would wrongly allow it.
	link := filepath.Join(root, "escape")
	if err := os.Symlink(outside, link); err != nil {
		t.Fatal(err)
	}
	ok, err = Allowed([]string{root}, link)
	if err != nil || ok {
		t.Fatalf("a symlink escaping the root must be refused: ok=%v err=%v", ok, err)
	}
}

func TestFilterModelIDs(t *testing.T) {
	ids := []string{"pystino/coder-large", "anthropic/claude", "pystino/chat-small"}

	closed := Policy{AllowFreeModels: false}
	got := closed.FilterModelIDs(ids)
	want := []string{"pystino/coder-large", "pystino/chat-small"}
	if !equalStrings(got, want) {
		t.Errorf("closed policy: got %v, want %v", got, want)
	}

	open := Policy{AllowFreeModels: true}
	got = open.FilterModelIDs(ids)
	if !equalStrings(got, ids) {
		t.Errorf("open policy must pass every id through: got %v", got)
	}
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
