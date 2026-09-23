package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

// encodeOffer mirrors what the SDK emits: the offer JSON base64url-encoded,
// unpadded (Node's Buffer.toString("base64url")), inside the #offer= fragment.
func encodeOffer(t *testing.T, offer map[string]any) string {
	t.Helper()
	body, err := json.Marshal(offer)
	if err != nil {
		t.Fatal(err)
	}
	return base64.RawURLEncoding.EncodeToString(body)
}

func TestExtractOfferFromPairingURL(t *testing.T) {
	url := "https://app.paseo.sh/#offer=" + encodeOffer(t, map[string]any{
		"v":                  2,
		"serverId":           "srv_abc123",
		"daemonPublicKeyB64": "cHVibGljLWtleQ==",
		"relay":              map[string]any{"endpoint": "cerea.example.org:443", "useTls": true},
	})

	offer, err := extractOffer(url)
	if err != nil {
		t.Fatalf("extractOffer errored: %v", err)
	}
	if offer.ServerID != "srv_abc123" || offer.DaemonPublicKeyB64 != "cHVibGljLWtleQ==" {
		t.Fatalf("misparsed offer: %+v", offer)
	}

	// A URL with noise after the fragment: the fragment ends at ? or &.
	noisy := "https://app.paseo.sh/#offer=" + encodeOffer(t, map[string]any{
		"serverId":           "srv_x",
		"daemonPublicKeyB64": "k",
	}) + "?utm=noise"
	if _, err := extractOffer(noisy); err != nil {
		t.Fatalf("trailing query broke the fragment: %v", err)
	}

	// A padded fragment is tolerated the way the chat's parser is.
	padded := base64.URLEncoding.EncodeToString([]byte(`{"serverId":"srv_p","daemonPublicKeyB64":"k"}`))
	if _, err := extractOffer("https://app.paseo.sh/#offer=" + padded); err != nil {
		t.Fatalf("padded fragment refused: %v", err)
	}
}

func TestExtractOfferRejectsBrokenLinks(t *testing.T) {
	cases := []struct {
		name string
		url  func(t *testing.T) string
		want string
	}{
		{"no fragment", func(*testing.T) string { return "https://app.paseo.sh/" }, "no #offer= fragment"},
		{"not base64", func(*testing.T) string { return "https://app.paseo.sh/#offer=!!!" }, "not base64url"},
		{
			"not json",
			func(*testing.T) string {
				return "https://app.paseo.sh/#offer=" + base64.RawURLEncoding.EncodeToString([]byte("hello"))
			},
			"not readable JSON",
		},
		{
			"missing identity",
			func(t *testing.T) string {
				return "https://app.paseo.sh/#offer=" + encodeOffer(t, map[string]any{"v": 2})
			},
			"missing the daemon's relay identity",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := extractOffer(tc.url(t))
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("want error containing %q, got: %v", tc.want, err)
			}
		})
	}
}

func TestResolveChatDerivesFromGateway(t *testing.T) {
	// The stored gateway is the deployment origin's /v1 base; the chat lives
	// beside it at <origin>/chat.
	chat, err := resolveChat("", "https://cerea.pviviani.eu/v1")
	if err != nil {
		t.Fatalf("derive errored: %v", err)
	}
	if chat != "https://cerea.pviviani.eu/chat" {
		t.Fatalf("derived %q", chat)
	}

	// An explicit --chat wins, and a trailing slash is trimmed.
	chat, err = resolveChat("https://chat.example.org/chat/", "https://ignored.example.org/v1")
	if err != nil {
		t.Fatalf("explicit errored: %v", err)
	}
	if chat != "https://chat.example.org/chat" {
		t.Fatalf("explicit produced %q", chat)
	}

	// A credential file from before the gateway shape existed names the
	// remedy instead of deriving a URL from garbage.
	if _, err := resolveChat("", "not-a-url"); err == nil || !strings.Contains(err.Error(), "--chat") {
		t.Fatalf("want a --chat remedy for a bad gateway, got: %v", err)
	}

	// --chat must itself be absolute.
	if _, err := resolveChat("chat.example.org/chat", "https://cerea.pviviani.eu/v1"); err == nil {
		t.Fatal("a relative --chat was accepted")
	}
}

func TestPairRequestBodyShape(t *testing.T) {
	var gotPath, gotAuth, gotAccept, gotName, gotOffer, gotMethod string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotMethod = r.Method
		gotPath = r.URL.Path
		gotAuth = r.Header.Get("Authorization")
		gotAccept = r.Header.Get("Accept")
		var body pairRequestBody
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Errorf("body did not decode: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		gotName = body.Name
		gotOffer = body.Offer
		// The endpoint answers superjson: the payload sits under "json".
		_, _ = w.Write([]byte(`{"json":{"device":{"id":"dev1","name":"box","status":"paired"}},"meta":{}}`))
	}))
	defer srv.Close()

	device, err := postPairing(context.Background(), srv.URL, "access-token-1", "my box", "https://app.paseo.sh/#offer=abc")
	if err != nil {
		t.Fatalf("postPairing errored: %v", err)
	}
	if gotMethod != http.MethodPost {
		t.Errorf("method = %q", gotMethod)
	}
	if gotPath != "/api/v2/code/enroll/machine" {
		t.Errorf("path = %q, want the machine endpoint", gotPath)
	}
	if gotAuth != "Bearer access-token-1" {
		t.Errorf("Authorization = %q", gotAuth)
	}
	if !strings.Contains(gotAccept, "application/json") {
		t.Errorf("Accept = %q, want JSON negotiation so errors come back as JSON", gotAccept)
	}
	if gotName != "my box" || gotOffer != "https://app.paseo.sh/#offer=abc" {
		t.Errorf("body was {name: %q, offer: %q}", gotName, gotOffer)
	}
	if !strings.Contains(device, "dev1") || !strings.Contains(device, "paired") {
		t.Errorf("device description %q lost the view", device)
	}
}

func TestPairErrorMapping(t *testing.T) {
	cases := []struct {
		status int
		body   string
		want   string
	}{
		{
			http.StatusUnauthorized,
			`{"message":"That access token is not valid or has expired; run the enrollment again."}`,
			"the sign-in expired; run 'enroll enroll' again",
		},
		{
			// The login wall's shape carries "error", not "message".
			http.StatusUnauthorized,
			`{"error":"You must be logged in"}`,
			"the sign-in expired",
		},
		{
			http.StatusNotFound,
			`{"message":"Log into the chat once before pairing a machine."}`,
			"log into the chat once",
		},
		{
			// A chat without the endpoint at all (old deployment) must not
			// read as "log into the chat": the server's own message rides
			// along so the two 404s can be told apart.
			http.StatusNotFound,
			`{"message":"Not Found"}`,
			"a chat without the machine endpoint",
		},
		{
			// Same for the deployment-without-relay case: the endpoint's own
			// 404 carries its reason, and the CLI must not rebrand it.
			http.StatusNotFound,
			`{"message":"No coding-agent relay is configured in this deployment."}`,
			"no relay configured",
		},
		{
			http.StatusBadGateway,
			`{"message":"The daemon could not be reached through the relay."}`,
			"paseo daemon status",
		},
		{http.StatusForbidden, `{"message":"no"}`, "HTTP 403"},
	}
	for _, tc := range cases {
		err := pairHTTPError(tc.status, []byte(tc.body))
		if err == nil || !strings.Contains(err.Error(), tc.want) {
			t.Errorf("status %d: want error containing %q, got: %v", tc.status, tc.want, err)
		}
	}
}

// fakePaseo installs an executable named `paseo` on a PATH of its own, so
// fetchPairingOffer's exec reaches a scripted daemon instead of a real one.
// toErr sends the payload to stderr instead of stdout — the CLI has been
// observed doing either with its structured error.
func fakePaseo(t *testing.T, payload string, exit int, toErr bool) {
	t.Helper()
	dir := t.TempDir()
	script := filepath.Join(dir, "paseo")
	redirect := ""
	if toErr {
		redirect = " >&2"
	}
	sh := "#!/bin/sh\nprintf '%s' " + shellQuote(payload) + redirect + "\nexit " + strconv.Itoa(exit) + "\n"
	if err := os.WriteFile(script, []byte(sh), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir)
}

func shellQuote(s string) string {
	return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'"
}

func TestFetchPairingOfferRelayDisabled(t *testing.T) {
	// The daemon's own refusal when relay pairing is off: structured JSON on
	// stdout, exit 1. The remedy must be the relay wiring (what setup-agent.sh
	// writes), not "run with --relay" — which a launch-override daemon answers
	// with a second error.
	relayOff := `{"code":"RELAY_DISABLED","message":"Relay pairing is disabled for this daemon.","action":"Run paseo daemon pair --relay --json to enable it explicitly."}`
	for _, toErr := range []bool{true, false} {
		fakePaseo(t, relayOff, 1, toErr)
		_, err := fetchPairingOffer(context.Background())
		if err == nil || !strings.Contains(err.Error(), "relay is not enabled") {
			t.Fatalf("toErr=%v: want the relay-wiring remedy, got: %v", toErr, err)
		}
	}
}

func TestFetchPairingOfferSuccess(t *testing.T) {
	url := "https://app.paseo.sh/#offer=" + encodeOffer(t, map[string]any{
		"v":                  2,
		"serverId":           "srv_cli_test",
		"daemonPublicKeyB64": "k",
	})
	fakePaseo(t, `{"relayEnabled":true,"url":`+mustJSON(t, url)+`,"qr":"data:image/png;base64,xx"}`, 0, false)
	got, err := fetchPairingOffer(context.Background())
	if err != nil {
		t.Fatalf("fetchPairingOffer errored: %v", err)
	}
	if got != url {
		t.Fatalf("offer URL mutated: %q", got)
	}
}

func TestFetchPairingOfferNoOffer(t *testing.T) {
	// relayEnabled true with a null URL is the SDK's own "no offer" shape
	// (generateLocalPairingOffer) — refuse it with the wiring remedy.
	fakePaseo(t, `{"relayEnabled":true,"url":null,"qr":null}`, 0, false)
	_, err := fetchPairingOffer(context.Background())
	if err == nil || !strings.Contains(err.Error(), "no relay pairing offer") {
		t.Fatalf("want the no-offer remedy, got: %v", err)
	}
}

func mustJSON(t *testing.T, v any) string {
	t.Helper()
	body, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(body)
}
