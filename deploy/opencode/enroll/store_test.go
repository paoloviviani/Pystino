package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// TestBuildOpencodeConfigShape pins the emitted opencode.json to the template
// shape (deploy/opencode/opencode.json.template): same provider entry, same
// npm package, baseURL under options, no apiKey (the shim owns the bearer,
// ADR 0040), and gateway limit hints carried into limit blocks.
func TestBuildOpencodeConfigShape(t *testing.T) {
	models := []gatewayModel{
		{ID: "coder-large", DisplayName: "Coder Large", ContextWindow: 200000, MaxOutputTokens: 32000},
		{ID: "chat-small"},
	}
	cfg := buildOpencodeConfig("127.0.0.1:41871", models)
	if cfg.Schema != "https://opencode.ai/config.json" {
		t.Fatalf("wrong schema: %s", cfg.Schema)
	}
	provider, ok := cfg.Provider["pystino"]
	if !ok {
		t.Fatal("missing pystino provider")
	}
	if provider.NPM != "@ai-sdk/openai-compatible" {
		t.Fatalf("wrong npm package: %s", provider.NPM)
	}
	if provider.Options["baseURL"] != "http://127.0.0.1:41871/v1" {
		t.Fatalf("wrong baseURL: %s", provider.Options["baseURL"])
	}
	if _, hasKey := provider.Options["apiKey"]; hasKey {
		t.Fatal("config must not carry an apiKey: the token expires, the shim refreshes it")
	}
	large := provider.Models["coder-large"]
	if large.Name != "Coder Large" || large.Limit == nil ||
		large.Limit.Context != 200000 || large.Limit.Output != 32000 {
		t.Fatalf("limit hints lost: %+v", large)
	}
	small := provider.Models["chat-small"]
	if small.Name != "chat-small" || small.Limit != nil {
		t.Fatalf("nameless model misrendered: %+v", small)
	}

	// The file itself must parse back to the same document.
	path := filepath.Join(t.TempDir(), "opencode.json")
	if err := writeOpencodeConfig(path, cfg); err != nil {
		t.Fatal(err)
	}
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var roundTrip opencodeConfig
	if err := json.Unmarshal(body, &roundTrip); err != nil {
		t.Fatalf("written config does not parse: %v", err)
	}
	if len(roundTrip.Provider["pystino"].Models) != 2 {
		t.Fatal("models map did not survive the round trip")
	}
}

// TestBuildOpencodeConfigPlaceholder pins the unreachable-gateway fallback:
// same placeholder id the pasted-key installer writes, so both paths tell
// the user the same thing.
func TestBuildOpencodeConfigPlaceholder(t *testing.T) {
	cfg := buildOpencodeConfig("127.0.0.1:41871", nil)
	entry, ok := cfg.Provider["pystino"].Models["REPLACE-WITH-MODEL-ID"]
	if !ok {
		t.Fatal("missing placeholder models entry")
	}
	if entry.Name == "" {
		t.Fatal("placeholder needs a human-readable name")
	}
}
