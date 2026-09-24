package workspaces

import (
	"os"
	"path/filepath"
	"testing"
)

func TestCreateListGetRoundTrip(t *testing.T) {
	dir := t.TempDir()
	projectDir := filepath.Join(dir, "project")
	if err := os.Mkdir(projectDir, 0o755); err != nil {
		t.Fatal(err)
	}
	regPath := filepath.Join(dir, "workspaces.json")

	r, err := Load(regPath)
	if err != nil {
		t.Fatal(err)
	}
	w, err := r.Create("My Project", projectDir, nil)
	if err != nil {
		t.Fatal(err)
	}
	if w.ID == "" || w.Path != projectDir {
		t.Fatalf("unexpected workspace: %+v", w)
	}

	// Reload from disk: a fresh Registry must see the same workspace.
	r2, err := Load(regPath)
	if err != nil {
		t.Fatal(err)
	}
	got, ok := r2.Get(w.ID)
	if !ok {
		t.Fatal("workspace not found after reload")
	}
	if got.Name != "My Project" {
		t.Errorf("Name = %q after reload", got.Name)
	}
	list := r2.List(false)
	if len(list) != 1 {
		t.Fatalf("List = %v, want exactly one workspace", list)
	}
}

func TestCreateRejectsNonDirectory(t *testing.T) {
	dir := t.TempDir()
	file := filepath.Join(dir, "not-a-dir")
	if err := os.WriteFile(file, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	r, err := Load(filepath.Join(dir, "workspaces.json"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := r.Create("bad", file, nil); err == nil {
		t.Fatal("Create must refuse a path that is not a directory")
	}
}

func TestCreateEnforcesWorkspaceRoots(t *testing.T) {
	dir := t.TempDir()
	root := filepath.Join(dir, "allowed")
	inside := filepath.Join(root, "proj")
	outside := filepath.Join(dir, "elsewhere")
	for _, p := range []string{inside, outside} {
		if err := os.MkdirAll(p, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	r, err := Load(filepath.Join(dir, "workspaces.json"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := r.Create("ok", inside, []string{root}); err != nil {
		t.Fatalf("a path under the root must be allowed: %v", err)
	}
	if _, err := r.Create("bad", outside, []string{root}); err == nil {
		t.Fatal("a path outside every root must be refused")
	}
}

func TestArchiveExcludesFromDefaultList(t *testing.T) {
	dir := t.TempDir()
	projectDir := filepath.Join(dir, "project")
	if err := os.Mkdir(projectDir, 0o755); err != nil {
		t.Fatal(err)
	}
	r, err := Load(filepath.Join(dir, "workspaces.json"))
	if err != nil {
		t.Fatal(err)
	}
	w, err := r.Create("proj", projectDir, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.Archive(w.ID); err != nil {
		t.Fatal(err)
	}
	if list := r.List(false); len(list) != 0 {
		t.Errorf("archived workspace still in default List: %v", list)
	}
	if list := r.List(true); len(list) != 1 {
		t.Errorf("archived workspace missing from includeArchived List: %v", list)
	}
	if _, ok := r.Get(w.ID); !ok {
		t.Error("archived workspace must remain reachable by id")
	}
}

func TestRename(t *testing.T) {
	dir := t.TempDir()
	projectDir := filepath.Join(dir, "project")
	if err := os.Mkdir(projectDir, 0o755); err != nil {
		t.Fatal(err)
	}
	r, err := Load(filepath.Join(dir, "workspaces.json"))
	if err != nil {
		t.Fatal(err)
	}
	w, err := r.Create("old", projectDir, nil)
	if err != nil {
		t.Fatal(err)
	}
	renamed, err := r.Rename(w.ID, "new")
	if err != nil {
		t.Fatal(err)
	}
	if renamed.Name != "new" {
		t.Errorf("Name = %q, want new", renamed.Name)
	}
}
