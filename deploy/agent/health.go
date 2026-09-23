package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

// credState is the shim's verdict on the credential, coarse enough for
// something outside the process (an operator, Cerea reading the file
// through the paseo daemon) to act on without re-deriving it.
type credState string

const (
	// stateOK: the cached access token is usable, refreshed or not.
	stateOK credState = "ok"
	// stateExpired: the token endpoint gave its final word (invalid_grant)
	// — dead until a human re-enrolls and restarts serve.
	stateExpired credState = "expired"
	// stateUnreachable: the refresh attempt failed for a reason that might
	// pass — a network error, a 5xx from the IdP. Worth retrying.
	stateUnreachable credState = "unreachable"
)

// healthStatus is written to statusFileName next to the credentials on
// every state change, and served verbatim from GET /pystino/health, so
// both the file and the endpoint always agree.
type healthStatus struct {
	State     credState `json:"state"`
	CheckedAt time.Time `json:"checkedAt"`
	Message   string    `json:"message"`
}

const statusFileName = "pystino-status.json"

// statusPathFor places the status file beside the credential file: whatever
// --creds pointed at (default or overridden), the status lives in the same
// directory, so a reader that already knows where the credential is knows
// where the status is too.
func statusPathFor(credsPath string) string {
	return filepath.Join(filepath.Dir(credsPath), statusFileName)
}

// writeStatusFile persists status atomically: a reader (the health
// endpoint's file-based twin, or Cerea through the paseo daemon) must never
// observe a half-written file, so the write lands in a temp file in the same
// directory and is renamed into place, which POSIX guarantees is atomic
// within one filesystem.
func writeStatusFile(path string, status healthStatus) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return fmt.Errorf("creating status dir: %w", err)
	}
	body, err := json.MarshalIndent(status, "", "  ")
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".pystino-status-*.tmp")
	if err != nil {
		return fmt.Errorf("writing status: %w", err)
	}
	tmpPath := tmp.Name()
	if _, err := tmp.Write(append(body, '\n')); err != nil {
		_ = tmp.Close()
		_ = os.Remove(tmpPath)
		return fmt.Errorf("writing status: %w", err)
	}
	if err := tmp.Chmod(0o600); err != nil {
		_ = tmp.Close()
		_ = os.Remove(tmpPath)
		return fmt.Errorf("writing status: %w", err)
	}
	if err := tmp.Close(); err != nil {
		_ = os.Remove(tmpPath)
		return fmt.Errorf("writing status: %w", err)
	}
	if err := os.Rename(tmpPath, path); err != nil {
		_ = os.Remove(tmpPath)
		return fmt.Errorf("writing status: %w", err)
	}
	return nil
}

// readStatusFile reads back what writeStatusFile wrote — used by tests, and
// available to anything else that would rather read the file than the
// endpoint (the status file's whole reason to exist: Cerea reading it
// through the paseo daemon, with no HTTP round trip to the shim required).
func readStatusFile(path string) (healthStatus, error) {
	var status healthStatus
	body, err := os.ReadFile(path)
	if err != nil {
		return status, fmt.Errorf("reading status: %w", err)
	}
	if err := json.Unmarshal(body, &status); err != nil {
		return status, fmt.Errorf("parsing status: %w", err)
	}
	return status, nil
}
