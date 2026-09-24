package main

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// Re-enrolling must give the machine a new identity: the old id may have been
// revoked, and Cerea refuses a revoked id for good.
func TestRotateMachineIDMintsANewIDAndClearsTheRevokedMarker(t *testing.T) {
	dir := t.TempDir()
	first, err := loadOrMintMachineID(filepath.Join(dir, machineIDFileName))
	if err != nil {
		t.Fatal(err)
	}
	if err := writeRevokedMarker(dir, first); err != nil {
		t.Fatal(err)
	}
	if !revokedMarkerMatches(dir, first) {
		t.Fatal("marker for the current id must match")
	}
	second, err := rotateMachineID(dir)
	if err != nil {
		t.Fatal(err)
	}
	if second == first || second == "" {
		t.Fatalf("rotated id %q must differ from %q", second, first)
	}
	if revokedMarkerMatches(dir, second) {
		t.Fatal("a fresh id must not read as revoked")
	}
	if _, err := os.Stat(filepath.Join(dir, revokedMarkerFileName)); !os.IsNotExist(err) {
		t.Fatalf("the revoked marker must be gone after rotation, stat err = %v", err)
	}
	again, _ := loadOrMintMachineID(filepath.Join(dir, machineIDFileName))
	if again != second {
		t.Fatalf("run must keep the rotated id: %q != %q", again, second)
	}
}

func TestRevokedMarkerIsPerMachineID(t *testing.T) {
	dir := t.TempDir()
	if err := writeRevokedMarker(dir, "old-id"); err != nil {
		t.Fatal(err)
	}
	if revokedMarkerMatches(dir, "new-id") {
		t.Fatal("a marker for another id must not stop this one")
	}
}

// The live hang: after a revoke the link returns on its own while the
// process-wide context is still live. The forwarder must stop when run cancels
// its own context, or run waits in <-eventsDone forever instead of exiting 78.
func TestForwardingStopsWhenRunCancelsItNotOnlyOnSignal(t *testing.T) {
	processCtx, cancelProcess := context.WithCancel(context.Background())
	defer cancelProcess() // never cancelled before the assertion: no signal arrived
	forwardCtx, stopForwarding := context.WithCancel(processCtx)
	done := startForwarding(forwardCtx, func(c context.Context) { <-c.Done() })

	stopForwarding() // what run does on its way out after the link returned ErrRevoked
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("the forwarder kept running after run cancelled it; run would hang instead of exiting")
	}
}
