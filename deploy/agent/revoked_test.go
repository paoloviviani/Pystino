package main

import (
	"os"
	"path/filepath"
	"testing"
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
