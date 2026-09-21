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
type opencodeLimit struct {
	Context int `json:"context,omitempty"`
	Output  int `json:"output,omitempty"`
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

// opencodeConfig is the whole opencode.json this tool writes. Deliberately
// no apiKey: opencode would send it as-is and it would expire (ADR 0040).
// The baseURL points at the local shim, which owns the bearer instead.
type opencodeConfig struct {
	Schema   string                      `json:"$schema"`
	Provider map[string]opencodeProvider `json:"provider"`
}

// buildOpencodeConfig renders the config around the shim address. Models
// come from discovery; an empty discovery yields the same placeholder the
// pasted-key installer writes, replaced once the gateway is reachable.
func buildOpencodeConfig(shimAddr string, models []gatewayModel) *opencodeConfig {
	entries := make(map[string]opencodeModel, len(models))
	for _, m := range models {
		if m.ID == "" {
			continue
		}
		entry := opencodeModel{Name: m.DisplayName}
		if entry.Name == "" {
			entry.Name = m.ID
		}
		if m.ContextWindow > 0 || m.MaxOutputTokens > 0 {
			entry.Limit = &opencodeLimit{Context: m.ContextWindow, Output: m.MaxOutputTokens}
		}
		entries[m.ID] = entry
	}
	if len(entries) == 0 {
		entries["REPLACE-WITH-MODEL-ID"] = opencodeModel{
			Name: "Replace with a model id from GET /v1/models",
		}
	}
	return &opencodeConfig{
		Schema: "https://opencode.ai/config.json",
		Provider: map[string]opencodeProvider{
			"pystino": {
				NPM:  "@ai-sdk/openai-compatible",
				Name: "Pystino Gateway",
				Options: map[string]string{
					"baseURL": "http://" + shimAddr + "/v1",
				},
				Models: entries,
			},
		},
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
