package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

// credentials is everything serve needs to keep the proxy authenticated,
// and nothing opencode itself needs. The access token is cached here so the
// first proxied request does not pay for a refresh; the refresh token is the
// durable secret. Both live at 0600 and are never logged.
type credentials struct {
	Issuer        string `json:"issuer"`
	TokenEndpoint string `json:"token_endpoint"`
	ClientID      string `json:"client_id"`
	Gateway       string `json:"gateway"`
	Group         string `json:"group"`
	RefreshToken  string `json:"refresh_token"`
	AccessToken   string `json:"access_token"`
	ExpiresIn     int    `json:"expires_in"`
	ObtainedAt    int64  `json:"obtained_at_unix"`
	// ShimPort is the loopback port enroll chose and wrote into
	// opencode.json's baseURL; serve honours it so the two ends agree without
	// the human passing --port. Omitted (0) falls back to defaultShimPort.
	ShimPort int `json:"shim_port,omitempty"`
}

// refreshSkew makes serve refresh a little before expiry, so no proxied
// request rides a token that dies mid-flight.
const refreshSkew = 60 * time.Second

// needsRefresh reports whether the cached access token is within the skew of
// expiry (or already past it) — the one decision serve makes before reusing
// it. Split out so it is unit-testable without a clock or an HTTP round trip.
func (c *credentials) needsRefresh(now time.Time) bool {
	return !c.accessExpiry().After(now.Add(refreshSkew))
}

// accessExpiry converts the token response's relative lifetime into the
// absolute instant serve compares against. A missing lifetime defaults
// conservative (see tokenSet), so this only guards against a zero value.
func (c *credentials) accessExpiry() time.Time {
	lifetime := c.ExpiresIn
	if lifetime <= 0 {
		lifetime = defaultExpiresIn
	}
	return time.Unix(c.ObtainedAt, 0).Add(time.Duration(lifetime) * time.Second)
}

// defaultCredsPath keeps the secret next to opencode's own state: the global
// config dir opencode already uses, so one machine means one enrollment.
func defaultCredsPath() (string, error) {
	dir, err := os.UserConfigDir()
	if err != nil {
		return "", fmt.Errorf("locating config dir: %w", err)
	}
	return filepath.Join(dir, "opencode", "pystino-credentials.json"), nil
}

// saveCredentials writes the file with owner-only permissions from the
// start: creating it 0600 first (not chmod-after) leaves no window where a
// group-readable refresh token sits on disk.
func saveCredentials(path string, creds *credentials) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return fmt.Errorf("creating creds dir: %w", err)
	}
	body, err := json.MarshalIndent(creds, "", "  ")
	if err != nil {
		return err
	}
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o600)
	if err != nil {
		return fmt.Errorf("writing creds: %w", err)
	}
	if _, err := file.Write(append(body, '\n')); err != nil {
		_ = file.Close()
		return fmt.Errorf("writing creds: %w", err)
	}
	return file.Close()
}

// loadCredentials reads the file serve runs from. A corrupt file is a hard
// stop: half a credential would only fail later inside a proxied request.
func loadCredentials(path string) (*credentials, error) {
	body, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("reading creds (run 'enroll enroll' first): %w", err)
	}
	var creds credentials
	if err := json.Unmarshal(body, &creds); err != nil {
		return nil, fmt.Errorf("parsing creds: %w", err)
	}
	if creds.RefreshToken == "" || creds.TokenEndpoint == "" || creds.Gateway == "" {
		return nil, fmt.Errorf("creds file is incomplete: re-run 'enroll enroll'")
	}
	return &creds, nil
}

// opencodeLimit carries the gateway's context/output hints into the shape
// opencode understands, mirroring deploy/opencode/opencode.json.template.
// Both keys are REQUIRED by opencode's config schema for custom-provider
// models — omit either and opencode refuses the whole file ("Missing key
// provider.pystino.models.<id>.limit.output"), which is why the zero
// values below are never emitted (omitempty would lie about that).
type opencodeLimit struct {
	Context int `json:"context"`
	Output  int `json:"output"`
}

// opencodeModel is one entry of the provider models map: the id is the
// gateway's model name.
type opencodeModel struct {
	Name  string         `json:"name"`
	Limit *opencodeLimit `json:"limit,omitempty"`
}

// opencodeProvider is the pystino entry under "provider".
type opencodeProvider struct {
	NPM     string                   `json:"npm"`
	Name    string                   `json:"name"`
	Options map[string]string        `json:"options"`
	Models  map[string]opencodeModel `json:"models"`
}

// The defaults cover a gateway that publishes no hints (both are nullable
// on the wire): a flash-class output budget and a mid-size context window
// are safer than an omitted key, which opencode treats as a broken file.
const (
	defaultContextWindow   = 131072
	defaultMaxOutputTokens = 16384
)

// opencodeConfig is the whole opencode.json this tool writes. Deliberately
// no apiKey: opencode would send it as-is and it would expire (ADR 0040).
// The baseURL points at the local shim, which owns the bearer instead.
type opencodeConfig struct {
	Schema string `json:"$schema"`
	// EnabledProviders is opencode's own provider allowlist. Verified
	// against opencode's config schema (https://opencode.ai/config.json,
	// "enabled_providers": "When set, ONLY these providers will be enabled.
	// All other providers will be ignored") and against the pinned
	// version's source (packages/opencode/src/provider/provider.ts at
	// v1.18.31: the final provider list keeps only ids the allowlist
	// names, applied to built-in and config-defined providers alike).
	// Naming just pystino is what makes the gateway's models the only ones
	// the picker offers — and unlike enumerating disabled_providers, it
	// stays correct when an opencode release ships a new built-in.
	// Omitted entirely when the operator opts out with
	// --allow-opencode-provider, so nothing about opencode's default
	// behaviour is claimed either way.
	EnabledProviders []string                    `json:"enabled_providers,omitempty"`
	Provider         map[string]opencodeProvider `json:"provider"`
}

// buildOpencodeConfig renders the config around the shim address. Models
// come from discovery; an empty discovery yields the same placeholder the
// pasted-key installer writes, replaced once the gateway is reachable.
// Only chat models make the list: opencode is a coding agent, and an
// embedding tier in its model picker is one accidental keypress from a
// 400. Unknown limits become defaults, never omissions — opencode's
// schema rejects a model entry without both limit keys (found live).
//
// allowOpencodeProviders=false names pystino in enabled_providers, so the
// machine's opencode sees the gateway's models and nothing else: a
// built-in provider with ambient credentials (an ANTHROPIC_API_KEY in the
// environment, a logged-in Copilot) would otherwise offer models that
// bypass the gateway — its spend would never land in the caller's
// account, which the whole point of enrolling is. The flag opts out for
// operators who want both.
func buildOpencodeConfig(
	shimAddr string,
	models []gatewayModel,
	allowOpencodeProviders bool,
) *opencodeConfig {
	entries := make(map[string]opencodeModel, len(models))
	for _, m := range models {
		if m.ID == "" {
			continue
		}
		if m.Kind != "" && m.Kind != "chat" {
			continue
		}
		entry := opencodeModel{Name: m.DisplayName}
		if entry.Name == "" {
			entry.Name = m.ID
		}
		context, output := m.ContextWindow, m.MaxOutputTokens
		if context <= 0 {
			context = defaultContextWindow
		}
		if output <= 0 {
			output = defaultMaxOutputTokens
		}
		entry.Limit = &opencodeLimit{Context: context, Output: output}
		entries[m.ID] = entry
	}
	if len(entries) == 0 {
		entries["REPLACE-WITH-MODEL-ID"] = opencodeModel{
			Name:  "Replace with a model id from GET /v1/models",
			Limit: &opencodeLimit{Context: defaultContextWindow, Output: defaultMaxOutputTokens},
		}
	}
	provider := map[string]opencodeProvider{
		"pystino": {
			NPM:  "@ai-sdk/openai-compatible",
			Name: "Pystino Gateway",
			Options: map[string]string{
				"baseURL": "http://" + shimAddr + "/v1",
			},
			Models: entries,
		},
	}
	if allowOpencodeProviders {
		return &opencodeConfig{Schema: "https://opencode.ai/config.json", Provider: provider}
	}
	return &opencodeConfig{
		Schema:           "https://opencode.ai/config.json",
		EnabledProviders: []string{"pystino"},
		Provider:         provider,
	}
}

// writeOpencodeConfig persists the file. 0600 is harmless for a file with no
// secret in it, and keeps the permissions story to one rule: everything this
// tool writes is owner-only.
func writeOpencodeConfig(path string, cfg *opencodeConfig) error {
	body, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return err
	}
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o600)
	if err != nil {
		return fmt.Errorf("writing opencode.json: %w", err)
	}
	if _, err := file.Write(append(body, '\n')); err != nil {
		_ = file.Close()
		return fmt.Errorf("writing opencode.json: %w", err)
	}
	return file.Close()
}
