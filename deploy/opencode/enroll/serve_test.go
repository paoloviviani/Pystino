package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// TestNeedsRefresh pins the one decision the shim makes before reusing a
// token: fresh tokens are kept, tokens inside the skew (or past expiry) are
// refreshed.
func TestNeedsRefresh(t *testing.T) {
	now := time.Now()
	fresh := &credentials{ExpiresIn: 300, ObtainedAt: now.Unix()}
	if fresh.needsRefresh(now) {
		t.Error("a token 300s from expiry should not need refreshing")
	}
	nearExpiry := &credentials{ExpiresIn: 300, ObtainedAt: now.Add(-280 * time.Second).Unix()}
	if !nearExpiry.needsRefresh(now) {
		t.Error("a token 20s from expiry is inside the skew and should refresh")
	}
	expired := &credentials{ExpiresIn: 300, ObtainedAt: now.Add(-600 * time.Second).Unix()}
	if !expired.needsRefresh(now) {
		t.Error("an expired token must refresh")
	}
}

// TestShimRefreshesAndForwards drives the whole handler: an expired token
// forces a refresh, and the gateway must then see the fresh bearer, the
// recorded x-bill-to, and the /v1-rewritten path, with the body streamed back.
func TestShimRefreshesAndForwards(t *testing.T) {
	tokenServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := r.ParseForm(); err != nil {
			t.Errorf("token endpoint: bad form: %v", err)
		}
		if got := r.Form.Get("grant_type"); got != "refresh_token" {
			t.Errorf("token endpoint: grant_type=%q, want refresh_token", got)
		}
		if got := r.Form.Get("refresh_token"); got != "refresh-abc" {
			t.Errorf("token endpoint: refresh_token=%q", got)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"access_token": "fresh-access-token",
			"expires_in":   300,
		})
	}))
	defer tokenServer.Close()

	var sawAuth, sawBillTo, sawPath string
	gatewayServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sawAuth = r.Header.Get("Authorization")
		sawBillTo = r.Header.Get("x-bill-to")
		sawPath = r.URL.Path
		if r.Header.Get("Authorization") != "" && r.Header.Get("Authorization") == "Bearer stale" {
			t.Error("gateway saw the stale token; refresh did not run")
		}
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, "data: hello\n\n")
	}))
	defer gatewayServer.Close()

	creds := &credentials{
		TokenEndpoint: tokenServer.URL,
		ClientID:      "opencode-enrollment",
		Gateway:       gatewayServer.URL + "/v1",
		Group:         "research",
		RefreshToken:  "refresh-abc",
		AccessToken:   "stale",
		ExpiresIn:     300,
		ObtainedAt:    time.Now().Add(-600 * time.Second).Unix(), // expired
	}
	credsPath := filepath.Join(t.TempDir(), "creds.json")
	if err := saveCredentials(credsPath, creds); err != nil {
		t.Fatal(err)
	}

	s := newShim(creds, credsPath)
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"m"}`))
	rec := httptest.NewRecorder()
	s.handler(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
	}
	if sawAuth != "Bearer fresh-access-token" {
		t.Errorf("gateway Authorization = %q, want the refreshed bearer", sawAuth)
	}
	if sawBillTo != "research" {
		t.Errorf("gateway x-bill-to = %q, want research", sawBillTo)
	}
	if sawPath != "/v1/chat/completions" {
		t.Errorf("gateway path = %q, want /v1/chat/completions", sawPath)
	}
	if got := rec.Body.String(); !strings.Contains(got, "data: hello") {
		t.Errorf("body not streamed back: %q", got)
	}
	// The rotated-in token must be persisted for the next start.
	reloaded, err := loadCredentials(credsPath)
	if err != nil {
		t.Fatal(err)
	}
	if reloaded.AccessToken != "fresh-access-token" {
		t.Errorf("refreshed token not persisted: %q", reloaded.AccessToken)
	}
}
