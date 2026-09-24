package main

import (
	"encoding/json"
	"strings"
	"testing"

	"pystino-agent/internal/policy"
)

// Cerea validates the hello strictly and silently drops one it cannot parse,
// so a default policy (no workspace roots) must still send an array.
func TestHelloSendsEmptyWorkspaceRootsNotNull(t *testing.T) {
	body, err := json.Marshal(buildHello(newE2EFakeBackend(), policy.Default()))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(body), `"workspaceRoots":[]`) {
		t.Fatalf("hello policy must carry workspaceRoots as [], got %s", body)
	}
}
