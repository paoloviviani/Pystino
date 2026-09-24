package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// stubDeviceServer answers the two endpoints the device flow touches. The
// token endpoint replays a scripted sequence; the counters let tests assert
// exactly how often each endpoint was hit.
type stubDeviceServer struct {
	authzCalls int
	tokenCalls int
	tokenNext  func(call int) (int, string)
	srv        *httptest.Server
}

func newStubDeviceServer(t *testing.T, tokenNext func(call int) (int, string)) *stubDeviceServer {
	t.Helper()
	stub := &stubDeviceServer{tokenNext: tokenNext}
	mux := http.NewServeMux()
	mux.HandleFunc("/device", func(w http.ResponseWriter, r *http.Request) {
		stub.authzCalls++
		if r.Method != http.MethodPost {
			t.Errorf("device endpoint got %s, want POST", r.Method)
		}
		if err := r.ParseForm(); err != nil {
			t.Errorf("device endpoint form: %v", err)
		}
		if got := r.PostForm.Get("client_id"); got != "opencode-enrollment" {
			t.Errorf("device endpoint client_id = %q", got)
		}
		if got := r.PostForm.Get("scope"); !strings.Contains(got, "groups") {
			t.Errorf("device endpoint scope %q omits groups", got)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"device_code":               "dc-1",
			"user_code":                 "WDJB-MJHT",
			"verification_uri":          "https://idp.example.org/device",
			"verification_uri_complete": "https://idp.example.org/device?user_code=WDJB-MJHT",
			"expires_in":                600,
			"interval":                  1,
		})
	})
	mux.HandleFunc("/token", func(w http.ResponseWriter, r *http.Request) {
		stub.tokenCalls++
		if err := r.ParseForm(); err != nil {
			t.Errorf("token endpoint form: %v", err)
		}
		if got := r.PostForm.Get("grant_type"); got != "urn:ietf:params:oauth:grant-type:device_code" {
			t.Errorf("token endpoint grant_type = %q", got)
		}
		if got := r.PostForm.Get("device_code"); got != "dc-1" {
			t.Errorf("token endpoint device_code = %q", got)
		}
		status, body := stub.tokenNext(stub.tokenCalls)
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	})
	stub.srv = httptest.NewServer(mux)
	t.Cleanup(stub.srv.Close)
	return stub
}

func docFor(stub *stubDeviceServer) *discovery {
	return &discovery{
		AuthorizationEndpoint:       stub.srv.URL + "/auth",
		TokenEndpoint:               stub.srv.URL + "/token",
		DeviceAuthorizationEndpoint: stub.srv.URL + "/device",
	}
}

func noopHooks() deviceFlowHooks {
	return deviceFlowHooks{
		sleep: func(context.Context, time.Duration) error { return nil },
		now:   time.Now,
	}
}

// TestDeviceFlowPendingThenSuccess drives the loop over two pending polls
// into success and asserts every poll slept the advertised interval first.
func TestDeviceFlowPendingThenSuccess(t *testing.T) {
	stub := newStubDeviceServer(t, func(call int) (int, string) {
		if call <= 2 {
			return http.StatusBadRequest, `{"error":"authorization_pending","error_description":"slow down"}`
		}
		return http.StatusOK, `{"access_token":"at","refresh_token":"rt","expires_in":300,"token_type":"Bearer"}`
	})
	var sleeps []time.Duration
	hooks := noopHooks()
	hooks.sleep = func(_ context.Context, d time.Duration) error {
		sleeps = append(sleeps, d)
		return nil
	}
	tokens, err := runDeviceFlowWithHooks(context.Background(), docFor(stub), "opencode-enrollment", hooks)
	if err != nil {
		t.Fatalf("flow errored: %v", err)
	}
	if tokens.AccessToken != "at" || tokens.RefreshToken != "rt" {
		t.Fatalf("wrong tokens: %+v", tokens)
	}
	if stub.tokenCalls != 3 {
		t.Fatalf("token endpoint hit %d times, want 3", stub.tokenCalls)
	}
	if len(sleeps) != 3 || sleeps[0] != time.Second || sleeps[1] != time.Second || sleeps[2] != time.Second {
		t.Fatalf("sleeps before each poll wrong: %v", sleeps)
	}
}

// TestDeviceFlowSlowDownBacksOff checks the §3.5 penalty: exactly five
// seconds added to the advertised interval, once per slow_down.
func TestDeviceFlowSlowDownBacksOff(t *testing.T) {
	stub := newStubDeviceServer(t, func(call int) (int, string) {
		if call == 1 {
			return http.StatusBadRequest, `{"error":"slow_down","error_description":"do not poll so fast"}`
		}
		return http.StatusOK, `{"access_token":"at","refresh_token":"rt","expires_in":300}`
	})
	var sleeps []time.Duration
	hooks := noopHooks()
	hooks.sleep = func(_ context.Context, d time.Duration) error {
		sleeps = append(sleeps, d)
		return nil
	}
	if _, err := runDeviceFlowWithHooks(context.Background(), docFor(stub), "opencode-enrollment", hooks); err != nil {
		t.Fatalf("flow errored: %v", err)
	}
	if len(sleeps) != 2 || sleeps[0] != time.Second || sleeps[1] != 6*time.Second {
		t.Fatalf("slow_down did not back off by 5s: %v", sleeps)
	}
}

// TestDeviceFlowAccessDenied stops the loop on a refusal: polling forever
// after an explicit denial would never finish.
func TestDeviceFlowAccessDenied(t *testing.T) {
	stub := newStubDeviceServer(t, func(int) (int, string) {
		return http.StatusBadRequest, `{"error":"access_denied","error_description":"no"}`
	})
	_, err := runDeviceFlowWithHooks(context.Background(), docFor(stub), "opencode-enrollment", noopHooks())
	if err == nil || !strings.Contains(err.Error(), "denied") {
		t.Fatalf("expected a denial error, got: %v", err)
	}
}

// TestDeviceFlowExpiredByClock uses a moved clock to reach the deadline and
// asserts the loop stopped polling (one hit) instead of asking forever.
func TestDeviceFlowExpiredByClock(t *testing.T) {
	stub := newStubDeviceServer(t, func(int) (int, string) {
		return http.StatusBadRequest, `{"error":"authorization_pending"}`
	})
	start := time.Now()
	tick := 0
	hooks := noopHooks()
	hooks.now = func() time.Time {
		tick++
		// Call 1 builds the deadline; call 2 is the first loop check. From
		// call 3 on, the fake clock reads past the deadline.
		if tick <= 2 {
			return start
		}
		return start.Add(700 * time.Second)
	}
	_, err := runDeviceFlowWithHooks(context.Background(), docFor(stub), "opencode-enrollment", hooks)
	if err == nil || !strings.Contains(err.Error(), "expired") {
		t.Fatalf("expected an expiry error, got: %v", err)
	}
	if stub.tokenCalls != 1 {
		t.Fatalf("token endpoint hit %d times after expiry, want 1", stub.tokenCalls)
	}
}

// TestRequestDeviceCodePinsDefaults pins the field checks and the §3.2/§3.5
// defaults for a sparse response.
func TestRequestDeviceCodePinsDefaults(t *testing.T) {
	var gotPath, gotScope string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		_ = r.ParseForm()
		gotScope = r.PostForm.Get("scope")
		_, _ = w.Write([]byte(`{"device_code":"dc","user_code":"ABCD-EFGH","verification_uri":"https://idp.example.org/device"}`))
	}))
	defer srv.Close()

	authz, err := requestDeviceCode(context.Background(), srv.URL+"/device", "opencode-enrollment")
	if err != nil {
		t.Fatal(err)
	}
	if gotPath != "/device" {
		t.Fatalf("posted to %q", gotPath)
	}
	if !strings.Contains(gotScope, "groups") {
		t.Fatalf("scope %q omits groups", gotScope)
	}
	if authz.DeviceCode != "dc" || authz.UserCode != "ABCD-EFGH" {
		t.Fatalf("misparsed response: %+v", authz)
	}
	if authz.Interval != 5 || authz.ExpiresIn != 600 {
		t.Fatalf("defaults not applied: interval=%d expires=%d", authz.Interval, authz.ExpiresIn)
	}
}

// TestRequestDeviceCodeRefusal surfaces the endpoint's own error words.
func TestRequestDeviceCodeRefusal(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"error":"invalid_client","error_description":"unknown client"}`))
	}))
	defer srv.Close()

	_, err := requestDeviceCode(context.Background(), srv.URL+"/device", "wrong")
	if err == nil || !strings.Contains(err.Error(), "invalid_client") {
		t.Fatalf("expected invalid_client refusal, got: %v", err)
	}
}
