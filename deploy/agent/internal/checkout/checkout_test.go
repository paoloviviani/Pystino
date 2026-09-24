package checkout

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	"pystino-agent/internal/backend"
)

func run(t *testing.T, dir string, args ...string) {
	t.Helper()
	cmd := exec.Command("git", append([]string{"-C", dir, "-c", "user.email=t@t", "-c", "user.name=t"}, args...)...)
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
}

func TestDiffSeesShellWrittenFilesEditsAndDeletes(t *testing.T) {
	dir := t.TempDir()
	run(t, dir, "init", "-q")
	os.WriteFile(filepath.Join(dir, "keep.txt"), []byte("a\nb\n"), 0o644)
	os.WriteFile(filepath.Join(dir, "gone.txt"), []byte("x\n"), 0o644)
	run(t, dir, "add", ".")
	run(t, dir, "commit", "-q", "-m", "init")

	os.WriteFile(filepath.Join(dir, "keep.txt"), []byte("a\nc\n"), 0o644)
	os.Remove(filepath.Join(dir, "gone.txt"))
	os.WriteFile(filepath.Join(dir, "out.txt"), []byte("parity\n"), 0o644) // as `echo > out.txt` would

	files, err := Diff(context.Background(), dir)
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]backend.FileDiff{}
	for _, f := range files {
		got[f.Path] = f
	}
	if f := got["out.txt"]; f.Status != backend.FileAdded || f.After != "parity\n" || f.Additions != 1 {
		t.Errorf("out.txt = %+v", f)
	}
	if f := got["keep.txt"]; f.Status != backend.FileModified || f.Before != "a\nb\n" || f.After != "a\nc\n" || f.Additions != 1 || f.Deletions != 1 {
		t.Errorf("keep.txt = %+v", f)
	}
	if f := got["gone.txt"]; f.Status != backend.FileDeleted || f.Before != "x\n" {
		t.Errorf("gone.txt = %+v", f)
	}
}

func TestDiffOutsideARepo(t *testing.T) {
	if _, err := Diff(context.Background(), t.TempDir()); err != ErrNotARepo {
		t.Fatalf("err = %v, want ErrNotARepo", err)
	}
}

func TestDiffFreshRepoWithoutCommits(t *testing.T) {
	dir := t.TempDir()
	run(t, dir, "init", "-q")
	os.WriteFile(filepath.Join(dir, "README.md"), []byte("# hi\n"), 0o644)
	files, err := Diff(context.Background(), dir)
	if err != nil || len(files) != 1 || files[0].Status != backend.FileAdded {
		t.Fatalf("files = %+v, err = %v", files, err)
	}
}
