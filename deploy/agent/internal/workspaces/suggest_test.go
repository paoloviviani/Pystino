package workspaces

import (
	"os"
	"path/filepath"
	"sort"
	"testing"
)

func mkdirs(t *testing.T, base string, names ...string) {
	t.Helper()
	for _, n := range names {
		if err := os.MkdirAll(filepath.Join(base, n), 0o755); err != nil {
			t.Fatal(err)
		}
	}
}

func names(dirs []Directory) []string {
	out := make([]string, len(dirs))
	for i, d := range dirs {
		out[i] = d.Name
	}
	sort.Strings(out)
	return out
}

func TestSuggestPrefixMatch(t *testing.T) {
	dir := t.TempDir()
	mkdirs(t, dir, "workspace-a", "workspace-b", "other")

	got, err := Suggest(filepath.Join(dir, "works"), []string{dir})
	if err != nil {
		t.Fatal(err)
	}
	if want := []string{"workspace-a", "workspace-b"}; !equalStrings(names(got), want) {
		t.Errorf("names = %v, want %v", names(got), want)
	}
}

func TestSuggestTrailingSlashListsEverything(t *testing.T) {
	dir := t.TempDir()
	mkdirs(t, dir, "a", "b", "c")

	got, err := Suggest(dir+"/", []string{dir})
	if err != nil {
		t.Fatal(err)
	}
	if want := []string{"a", "b", "c"}; !equalStrings(names(got), want) {
		t.Errorf("names = %v, want %v", names(got), want)
	}
}

func TestSuggestSkipsHiddenUnlessNamed(t *testing.T) {
	dir := t.TempDir()
	mkdirs(t, dir, ".hidden", "visible")

	got, err := Suggest(dir+"/", []string{dir})
	if err != nil {
		t.Fatal(err)
	}
	if want := []string{"visible"}; !equalStrings(names(got), want) {
		t.Errorf("trailing-slash listing = %v, want %v (hidden excluded)", names(got), want)
	}

	got, err = Suggest(filepath.Join(dir, ".hid"), []string{dir})
	if err != nil {
		t.Fatal(err)
	}
	if want := []string{".hidden"}; !equalStrings(names(got), want) {
		t.Errorf("prefix naming a hidden dir = %v, want %v", names(got), want)
	}
}

func TestSuggestEnforcesWorkspaceRoots(t *testing.T) {
	dir := t.TempDir()
	root := filepath.Join(dir, "allowed")
	mkdirs(t, root, "proj")
	mkdirs(t, dir, "elsewhere/proj-outside")

	got, err := Suggest(filepath.Join(dir, "allowed", "pro"), []string{root})
	if err != nil {
		t.Fatal(err)
	}
	if want := []string{"proj"}; !equalStrings(names(got), want) {
		t.Errorf("inside root = %v, want %v", names(got), want)
	}

	got, err = Suggest(filepath.Join(dir, "elsewhere", "proj"), []string{root})
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 0 {
		t.Errorf("outside every root: got %v, want none", got)
	}
}

func TestSuggestNoRootsDefaultsToHome(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	mkdirs(t, home, "proj")

	outside := t.TempDir()
	mkdirs(t, outside, "proj-outside")

	got, err := Suggest(filepath.Join(home, "pro"), nil)
	if err != nil {
		t.Fatal(err)
	}
	if want := []string{"proj"}; !equalStrings(names(got), want) {
		t.Errorf("under $HOME = %v, want %v", names(got), want)
	}

	got, err = Suggest(filepath.Join(outside, "proj"), nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 0 {
		t.Errorf("outside $HOME with no roots configured: got %v, want none", got)
	}
}

func TestSuggestExpandsTilde(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	mkdirs(t, home, "proj")

	got, err := Suggest("~/pro", nil)
	if err != nil {
		t.Fatal(err)
	}
	if want := []string{"proj"}; !equalStrings(names(got), want) {
		t.Errorf("~ expansion = %v, want %v", names(got), want)
	}
	if len(got) == 1 && got[0].Path != filepath.Join(home, "proj") {
		t.Errorf("Path = %q, want %q", got[0].Path, filepath.Join(home, "proj"))
	}
}

func TestSuggestCapsAtTwenty(t *testing.T) {
	dir := t.TempDir()
	for i := 0; i < 25; i++ {
		mkdirs(t, dir, "d"+string(rune('a'+i)))
	}

	got, err := Suggest(dir+"/", []string{dir})
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != maxSuggestions {
		t.Errorf("len = %d, want %d", len(got), maxSuggestions)
	}
}

func TestSuggestNeverDescendsIntoProcOrSys(t *testing.T) {
	for _, prefix := range []string{"/proc/", "/sys/"} {
		got, err := Suggest(prefix, nil)
		if err != nil {
			t.Fatalf("%s: %v", prefix, err)
		}
		if len(got) != 0 {
			t.Errorf("%s: got %v, want none", prefix, got)
		}
	}
}

func TestSuggestReportsIsGitRepo(t *testing.T) {
	dir := t.TempDir()
	mkdirs(t, dir, "repo/.git", "plain")

	got, err := Suggest(dir+"/", []string{dir})
	if err != nil {
		t.Fatal(err)
	}
	byName := map[string]bool{}
	for _, d := range got {
		byName[d.Name] = d.IsGitRepo
	}
	if !byName["repo"] {
		t.Error("repo/ must report isGitRepo: true")
	}
	if byName["plain"] {
		t.Error("plain/ must report isGitRepo: false")
	}
}

func TestSuggestFollowsSymlinkedDirectories(t *testing.T) {
	dir := t.TempDir()
	root := filepath.Join(dir, "allowed")
	mkdirs(t, root, "real")
	if err := os.Symlink(filepath.Join(root, "real"), filepath.Join(root, "link")); err != nil {
		t.Skipf("symlinks unsupported: %v", err)
	}

	got, err := Suggest(filepath.Join(root, "l"), []string{root})
	if err != nil {
		t.Fatal(err)
	}
	if want := []string{"link"}; !equalStrings(names(got), want) {
		t.Errorf("names = %v, want %v", names(got), want)
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
