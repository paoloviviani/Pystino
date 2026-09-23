package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestBuildOpencodeConfigShape pins the emitted opencode.json to the template
// shape (deploy/opencode/opencode.json.template): same provider entry, same
// npm package, baseURL under options, no apiKey (the shim owns the bearer,
// ADR 0040), and gateway limit hints carried into limit blocks. Default
// (opt-out) behaviour also pins enabled_providers to [pystino] — opencode's
// own allowlist, so the gateway's models are the only ones the picker offers.
func TestBuildOpencodeConfigShape(t *testing.T) {
	models := []gatewayModel{
		{ID: "coder-large", DisplayName: "Coder Large", ContextWindow: 200000, MaxOutputTokens: 32000},
		{ID: "chat-small"},
		// Non-chat kinds never make the picker: opencode is a coding
		// agent, and an embedding tier is one accidental keypress away
		// from a 400. An unknown kind stays listed (older gateways).
		{ID: "qwen3-embedding-8b", Kind: "embedding", ContextWindow: 32768},
		{ID: "legacy-unknown-kind", Kind: ""},
	}
	cfg := buildOpencodeConfig("127.0.0.1:41871", models, false)
	if cfg.Schema != "https://opencode.ai/config.json" {
		t.Fatalf("wrong schema: %s", cfg.Schema)
	}
	if len(cfg.EnabledProviders) != 1 || cfg.EnabledProviders[0] != "pystino" {
		t.Fatalf("built-in providers must be opted out via enabled_providers: %v", cfg.EnabledProviders)
	}
	provider, ok := cfg.Provider["pystino"]
	if ok != true {
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
	// A model with no hints gets defaults, never an omitted limit: opencode
	// rejects a custom-provider model entry without both keys (found live).
	small := provider.Models["chat-small"]
	if small.Limit == nil || small.Limit.Context != defaultContextWindow ||
		small.Limit.Output != defaultMaxOutputTokens {
		t.Fatalf("hintless model must carry default limits: %+v", small)
	}
	if _, listed := provider.Models["qwen3-embedding-8b"]; listed {
		t.Fatal("embedding kinds must not reach opencode's model picker")
	}
	if _, listed := provider.Models["legacy-unknown-kind"]; !listed {
		t.Fatal("an unknown kind stays listed (back-compat with hintless gateways)")
	}

	// The file itself must parse back to the same document, with every
	// model carrying both limit keys on the wire, and the allowlist named
	// at the top level (where opencode's schema puts it).
	path := filepath.Join(t.TempDir(), "opencode.json")
	if err := writeOpencodeConfig(path, cfg); err != nil {
		t.Fatal(err)
	}
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var raw struct {
		EnabledProviders []string `json:"enabled_providers"`
		Provider         struct {
			Pystino struct {
				Models map[string]struct {
					Limit *struct {
						Context *int `json:"context"`
						Output  *int `json:"output"`
					} `json:"limit"`
				} `json:"models"`
			} `json:"pystino"`
		} `json:"provider"`
	}
	if err := json.Unmarshal(body, &raw); err != nil {
		t.Fatalf("written config does not parse: %v", err)
	}
	if len(raw.EnabledProviders) != 1 || raw.EnabledProviders[0] != "pystino" {
		t.Fatalf("enabled_providers lost on the wire: %v", raw.EnabledProviders)
	}
	for id, m := range raw.Provider.Pystino.Models {
		if m.Limit == nil || m.Limit.Context == nil || m.Limit.Output == nil {
			t.Fatalf("model %s lacks a limit block on the wire", id)
		}
	}
}

// TestBuildOpencodeConfigPlaceholder pins the unreachable-gateway fallback:
// same placeholder id the pasted-key installer writes, so both paths tell
// the user the same thing.
func TestBuildOpencodeConfigPlaceholder(t *testing.T) {
	cfg := buildOpencodeConfig("127.0.0.1:41871", nil, false)
	entry, ok := cfg.Provider["pystino"].Models["REPLACE-WITH-MODEL-ID"]
	if !ok {
		t.Fatal("missing placeholder models entry")
	}
	if entry.Name == "" {
		t.Fatal("placeholder needs a human-readable name")
	}
	if entry.Limit == nil || entry.Limit.Context != defaultContextWindow || entry.Limit.Output != defaultMaxOutputTokens {
		t.Fatal("placeholder carries defaults too: opencode rejects a model without both limit keys")
	}
}

// TestBuildOpencodeConfigAllowOpencodeProvider pins the opt-in: with
// --allow-opencode-provider the allowlist is OMITTED, not emptied — a null
// or [] would be a claim about opencode's default behaviour, and omitting
// the key is the only shape that leaves opencode untouched.
func TestBuildOpencodeConfigAllowOpencodeProvider(t *testing.T) {
	models := []gatewayModel{{ID: "coder-large", DisplayName: "Coder Large"}}
	cfg := buildOpencodeConfig("127.0.0.1:41871", models, true)
	if cfg.EnabledProviders != nil {
		t.Fatalf("opting in must omit enabled_providers, got %v", cfg.EnabledProviders)
	}
	if _, ok := cfg.Provider["pystino"]; !ok {
		t.Fatal("the gateway's provider block is unconditional")
	}
	path := filepath.Join(t.TempDir(), "opencode.json")
	if err := writeOpencodeConfig(path, cfg); err != nil {
		t.Fatal(err)
	}
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(body), "enabled_providers") {
		t.Fatalf("opted-in config must not name the key at all: %s", body)
	}
}
