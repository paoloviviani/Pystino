package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"
)

const serveUsage = `enroll serve — run the local refreshing proxy shim.

Usage:
  enroll serve [--creds PATH] [--port PORT]

  --creds  Credential file written by 'enroll enroll' (default
           <config-dir>/opencode/pystino-credentials.json).
  --port   Override the loopback port recorded at enroll time.

opencode points its baseURL at http://127.0.0.1:<port>/v1; the shim
injects a fresh access token and the recorded x-bill-to on every request.

GET /pystino/health reports the credential's state ("ok", "expired", or
"unreachable"), mirrored to <creds-dir>/pystino-status.json on every change.
`

// shim is the running proxy: opencode speaks OpenAI to it over loopback, and
// it speaks to the gateway as the enrolled human. It owns the one thing
// opencode cannot — a token that expires (ADR 0040) — refreshing under a lock
// so a burst of requests triggers one refresh rather than one each.
type shim struct {
	mu         sync.Mutex
	creds      *credentials
	credsPath  string
	statusPath string
	status     healthStatus
	// deadErr is the permanent refusal that produced status.State ==
	// stateExpired, cached so every request after the first gets the exact
	// same answer with no further IdP round trip.
	deadErr error
	// No Timeout: a chat completion streams for as long as the model talks,
	// so the request's own context (opencode's connection) is what bounds a
	// call — a client hang-up cancels the upstream and nothing else does.
	client *http.Client
}

func newShim(creds *credentials, credsPath, statusPath string) *shim {
	return &shim{creds: creds, credsPath: credsPath, statusPath: statusPath, client: &http.Client{}}
}

// refreshInterval is how often the shim proactively refreshes ahead of any
// request, well inside even the tightest access-token lifespan (the
// generator's agent_machine lifespan sets it to 1h; ADR references live in
// deploy/idp). Refreshing on this timer — not only when a request arrives —
// is what lets serve answer "known dead" immediately instead of finding out
// mid-request.
const refreshInterval = 15 * time.Minute

// transitionLocked records a state check, writing the status file and
// logging only when the state actually changed — a steady "ok" every 15
// minutes is noise, but "ok" -> "expired" is the one line an operator needs
// to see. Caller holds s.mu.
func (s *shim) transitionLocked(state credState, checkedAt time.Time, message string) {
	changed := s.status.State != state
	s.status = healthStatus{State: state, CheckedAt: checkedAt, Message: message}
	if !changed {
		return
	}
	switch state {
	case stateExpired:
		fmt.Fprintf(os.Stderr, "\n!!! pystino shim: credential EXPIRED — every request will fail until this machine is re-enrolled !!!\n%s\n\n", message)
	case stateUnreachable:
		fmt.Fprintf(os.Stderr, "pystino shim: refresh failed, will retry — %s\n", message)
	case stateOK:
		fmt.Fprintf(os.Stderr, "pystino shim: credential OK — %s\n", message)
	}
	if err := writeStatusFile(s.statusPath, s.status); err != nil {
		fmt.Fprintf(os.Stderr, "warning: could not write status file: %v\n", err)
	}
}

// refreshLocked attempts one refresh and updates state from the outcome.
// Caller holds s.mu.
func (s *shim) refreshLocked() error {
	now := time.Now()
	err := refreshCredential(s.credsPath, s.creds)
	if err == nil {
		s.deadErr = nil
		s.transitionLocked(stateOK, now, "token refreshed")
		return nil
	}
	var perr *permanentRefreshError
	if errors.As(err, &perr) {
		s.deadErr = err
		s.transitionLocked(stateExpired, now, err.Error())
		return err
	}
	s.transitionLocked(stateUnreachable, now, err.Error())
	return err
}

// refresh is refreshLocked with its own lock, for callers outside the
// request path (startup, the background timer).
func (s *shim) refresh() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.refreshLocked()
}

// refreshLoop keeps the credential proactively current so state is known
// before any request needs it. It stops once the credential is confirmed
// dead: nothing left to check until a human re-enrolls and restarts this
// process, so there is no point spending a goroutine and an IdP round trip
// every interval on an answer that cannot change.
func (s *shim) refreshLoop() {
	ticker := time.NewTicker(refreshInterval)
	defer ticker.Stop()
	for range ticker.C {
		s.mu.Lock()
		dead := s.status.State == stateExpired
		s.mu.Unlock()
		if dead {
			return
		}
		_ = s.refresh()
	}
}

// healthHandler serves the same JSON the status file holds, so a caller
// that can reach the shim's loopback port does not need filesystem access
// to ask the same question.
func (s *shim) healthHandler(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	status := s.status
	s.mu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(status)
}

// hopByHop headers belong to a single connection and must not be relayed
// across the proxy in either direction (RFC 7230 §6.1).
var hopByHop = map[string]bool{
	"Connection": true, "Keep-Alive": true, "Proxy-Authenticate": true,
	"Proxy-Authorization": true, "Te": true, "Trailer": true,
	"Transfer-Encoding": true, "Upgrade": true,
}

// token returns a usable access token, refreshing when the cached one is
// within the skew of expiry, or the shim's cached verdict on why there is
// none. Once the credential is known dead (stateExpired) it returns deadErr
// straight away — no IdP round trip, because the answer cannot have changed
// since the refresh loop or a prior request already got the final word.
// Serialized: the lock both dedupes a concurrent refresh and guards the
// credential fields it mutates.
func (s *shim) token() (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.status.State == stateExpired {
		return "", s.deadErr
	}
	if !s.creds.needsRefresh(time.Now()) {
		return s.creds.AccessToken, nil
	}
	if err := s.refreshLocked(); err != nil {
		return "", err
	}
	return s.creds.AccessToken, nil
}

// refreshCredential exchanges the refresh token for a fresh access token,
// updating creds in place and persisting the result. Shared by the shim
// (per request) and `enroll pair` (once per run): a pairing run days after
// enrollment still authenticates, because the credential on disk is what
// keeps working, not the access token it was born with.
//
// A permanent refusal (invalid_grant) comes back as *permanentRefreshError;
// everything else — network errors, a 5xx, an OAuth body with some other
// code — is worth retrying and comes back as a plain error.
func refreshCredential(credsPath string, creds *credentials) error {
	// The refresh runs on its own deadline, not a triggering request's
	// context: one caller hanging up must not abort a refresh the others are
	// blocked on.
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	tokens, err := exchange(ctx, creds.TokenEndpoint, url.Values{
		"grant_type":    {"refresh_token"},
		"refresh_token": {creds.RefreshToken},
		"client_id":     {creds.ClientID},
	})
	if err != nil {
		var oauthErr *oauth2Error
		if errors.As(err, &oauthErr) {
			if oauthErr.permanent() {
				return &permanentRefreshError{code: oauthErr.Code}
			}
			return fmt.Errorf("refresh rejected (%s): %s", oauthErr.Code, oauthErr.Description)
		}
		return err
	}
	creds.AccessToken = tokens.AccessToken
	creds.ExpiresIn = tokens.ExpiresIn
	creds.ObtainedAt = time.Now().Unix()
	if tokens.RefreshToken != "" {
		// Honour rotation: an IdP that returns a new refresh token has
		// invalidated the old one, so keeping it would break the next refresh.
		creds.RefreshToken = tokens.RefreshToken
	}
	if err := saveCredentials(credsPath, creds); err != nil {
		// The refresh itself worked; failing to persist only costs one extra
		// refresh at next start. Warn, never fail the caller on it.
		fmt.Fprintf(os.Stderr, "warning: could not persist refreshed credential: %v\n", err)
	}
	return nil
}

// writeTokenError turns a token() failure into what opencode sees. A
// permanent refusal answers 401 with an OpenAI-style body: the Vercel AI SDK
// opencode is built on treats 408/409/429/5xx as retryable and everything
// else — 401 included — as final, so this is the one status that stops
// opencode from burning its retry budget against a credential that will
// never come back without a human re-enrolling. Everything else keeps the
// original behaviour: a transient 502, worth retrying.
func writeTokenError(w http.ResponseWriter, err error) {
	var perr *permanentRefreshError
	if errors.As(err, &perr) {
		writeOpenAIError(w, http.StatusUnauthorized, err.Error(), "invalid_request_error", "enrollment_expired")
		return
	}
	http.Error(w, err.Error(), http.StatusBadGateway)
}

// writeOpenAIError writes the shape opencode's OpenAI-compatible client
// parses for its error message: {"error":{"message","type","code"}}. A
// plain-text body (http.Error's default) would print as opencode's own
// generic "request failed" wrapper instead of this message verbatim.
func writeOpenAIError(w http.ResponseWriter, status int, message, errType, code string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{
		"error": map[string]string{
			"message": message,
			"type":    errType,
			"code":    code,
		},
	})
}

// handler proxies one request. It rewrites the loopback /v1 prefix onto the
// gateway's /v1 base, swaps in a fresh bearer and the recorded x-bill-to, and
// streams the response back unbuffered so SSE completions render token by
// token in opencode.
func (s *shim) handler(w http.ResponseWriter, r *http.Request) {
	accessToken, err := s.token()
	if err != nil {
		writeTokenError(w, err)
		return
	}

	// creds.Gateway already ends in /v1 and the incoming path is /v1/...;
	// trimming the one prefix leaves exactly one /v1 in the target.
	target := s.creds.Gateway + strings.TrimPrefix(r.URL.Path, "/v1")
	if r.URL.RawQuery != "" {
		target += "?" + r.URL.RawQuery
	}
	outbound, err := http.NewRequestWithContext(r.Context(), r.Method, target, r.Body)
	if err != nil {
		http.Error(w, "building the upstream request: "+err.Error(), http.StatusBadGateway)
		return
	}
	for name, values := range r.Header {
		canonical := http.CanonicalHeaderKey(name)
		// Drop the caller's Authorization: the shim, not opencode, owns the
		// credential. x-bill-to below is ours to set for the same reason.
		if hopByHop[canonical] || canonical == "Authorization" {
			continue
		}
		for _, v := range values {
			outbound.Header.Add(name, v)
		}
	}
	outbound.Header.Set("Authorization", "Bearer "+accessToken)
	// An OIDC-token caller names its billing group per request (ADR 0061);
	// a key could not, which is why enrollment mints a token, not a key.
	outbound.Header.Set("x-bill-to", s.creds.Group)

	resp, err := s.client.Do(outbound)
	if err != nil {
		http.Error(w, "reaching the gateway: "+err.Error(), http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	for name, values := range resp.Header {
		if hopByHop[http.CanonicalHeaderKey(name)] {
			continue
		}
		for _, v := range values {
			w.Header().Add(name, v)
		}
	}
	w.WriteHeader(resp.StatusCode)

	flusher, _ := w.(http.Flusher)
	buffer := make([]byte, 32*1024)
	for {
		n, readErr := resp.Body.Read(buffer)
		if n > 0 {
			if _, writeErr := w.Write(buffer[:n]); writeErr != nil {
				return
			}
			if flusher != nil {
				flusher.Flush()
			}
		}
		if readErr != nil {
			return
		}
	}
}

func runServe(args []string) error {
	fs := flagSetWithHelp("serve", serveUsage)
	var credsPath string
	var port int
	fs.StringVar(&credsPath, "creds", "", "")
	fs.IntVar(&port, "port", 0, "")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() > 0 {
		return fmt.Errorf("unexpected arguments: %s", strings.Join(fs.Args(), " "))
	}
	if credsPath == "" {
		path, err := defaultCredsPath()
		if err != nil {
			return err
		}
		credsPath = path
	}
	creds, err := loadCredentials(credsPath)
	if err != nil {
		return err
	}
	// Precedence: an explicit --port, else the port enroll recorded, else the
	// default. The recorded port is what enroll wrote into opencode.json's
	// baseURL, so honouring it is what keeps the two ends agreeing.
	if port == 0 {
		port = creds.ShimPort
	}
	if port == 0 {
		port = defaultShimPort
	}
	addr := fmt.Sprintf("127.0.0.1:%d", port)

	sh := newShim(creds, credsPath, statusPathFor(credsPath))
	// Refresh once before serving anything: know the state (ok, dead,
	// unreachable) up front rather than discovering it on opencode's first
	// request. The error is already logged loudly by transitionLocked; serve
	// still starts either way, since a transient failure here should not
	// block a credential that might still work moments later.
	_ = sh.refresh()
	go sh.refreshLoop()

	mux := http.NewServeMux()
	mux.HandleFunc("/pystino/health", sh.healthHandler)
	mux.HandleFunc("/", sh.handler)

	server := &http.Server{
		Addr:    addr,
		Handler: mux,
		// Bound only the header read: a streamed completion is long-lived by
		// design, so a write or idle deadline here would cut it off.
		ReadHeaderTimeout: 10 * time.Second,
	}
	fmt.Fprintf(os.Stderr, "pystino shim on http://%s (opencode baseURL http://%s/v1)\n", addr, addr)
	fmt.Fprintf(os.Stderr, "forwarding to %s as the enrolled user, billing group %s\n", creds.Gateway, creds.Group)
	fmt.Fprintf(os.Stderr, "spend is not visible here — /v1 has no usage surface; watch it in the console.\n")
	fmt.Fprintf(os.Stderr, "health: http://%s/pystino/health, status file %s\n", addr, sh.statusPath)
	return server.ListenAndServe()
}
