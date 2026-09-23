package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// TestStatusPathForSitsBesideCreds pins the placement contract: the status
// file lives in the same directory as whatever --creds pointed at, default
// or overridden.
func TestStatusPathForSitsBesideCreds(t *testing.T) {
	got := statusPathFor("/home/agent/.config/opencode/pystino-credentials.json")
	want := "/home/agent/.config/opencode/pystino-status.json"
	if got != want {
		t.Errorf("statusPathFor = %q, want %q", got, want)
	}
}

// TestWriteStatusFileAtomic checks the write round-trips correctly, leaves
// no temp file behind, and lands at 0600 — the same contract as
// saveCredentials, applied to a file that is not itself a secret but sits
// next to one.
func TestWriteStatusFileAtomic(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "pystino-status.json")
	now := time.Now().Truncate(time.Second).UTC()
	status := healthStatus{State: stateExpired, CheckedAt: now, Message: "dead credential"}

	if err := writeStatusFile(path, status); err != nil {
		t.Fatal(err)
	}

	got, err := readStatusFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if got.State != status.State || got.Message != status.Message || !got.CheckedAt.Equal(status.CheckedAt) {
		t.Errorf("read back %+v, want %+v", got, status)
	}

	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Errorf("status file mode = %o, want 0600", perm)
	}

	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 {
		t.Errorf("directory holds %d entries after write, want 1 (no leftover temp file): %v", len(entries), entries)
	}

	// A second write (the common case — a later state transition) must
	// replace the file cleanly, not leave two files or fail because the
	// target already exists.
	status2 := healthStatus{State: stateOK, CheckedAt: now.Add(time.Minute), Message: "token refreshed"}
	if err := writeStatusFile(path, status2); err != nil {
		t.Fatal(err)
	}
	got2, err := readStatusFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if got2.State != stateOK {
		t.Errorf("after second write, state = %q, want %q", got2.State, stateOK)
	}
	entries, err = os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 {
		t.Errorf("directory holds %d entries after second write, want 1", len(entries))
	}
}
