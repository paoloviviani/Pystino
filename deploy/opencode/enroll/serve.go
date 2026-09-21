package main

import (
	"context"
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
`

// shim is the running proxy: opencode speaks OpenAI to it over loopback, and
// it speaks to the gateway as the enrolled human. It owns the one thing
// opencode cannot — a token that expires (ADR 0040) — refreshing under a lock
// so a burst of requests triggers one refresh rather than one each.
type shim struct {
	mu        sync.Mutex
	creds     *credentials
	credsPath string
	// No Timeout: a chat completion streams for as long as the model talks,
	// so the request's own context (opencode's connection) is what bounds a
	// call — a client hang-up cancels the upstream and nothing else does.
	client *http.Client
}

func newShim(creds *credentials, credsPath string) *shim {
	return &shim{creds: creds, credsPath: credsPath, client: &http.Client{}}
}

// hopByHop headers belong to a single connection and must not be relayed
// across the proxy in either direction (RFC 7230 §6.1).
var hopByHop = map[string]bool{
	"Connection": true, "Keep-Alive": true, "Proxy-Authenticate": true,
	"Proxy-Authorization": true, "Te": true, "Trailer": true,
	"Transfer-Encoding": true, "Upgrade": true,
}

// token returns a usable access token, refreshing when the cached one is
// within the skew of expiry. Serialized: the lock both dedupes a concurrent
// refresh and guards the credential fields it mutates.
func (s *shim) token() (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.creds.needsRefresh(time.Now()) {
		return s.creds.AccessToken, nil
	}
	// The refresh runs on its own deadline, not a triggering request's
	// context: one caller hanging up must not abort a refresh the others are
	// blocked on.
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	tokens, err := exchange(ctx, s.creds.TokenEndpoint, url.Values{
		"grant_type":    {"refresh_token"},
		"refresh_token": {s.creds.RefreshToken},
		"client_id":     {s.creds.ClientID},
	})
	if err != nil {
		var oauthErr *oauth2Error
		if errors.As(err, &oauthErr) {
			return "", fmt.Errorf("refresh rejected (%s): the enrollment expired or was revoked — re-run 'enroll enroll'", oauthErr.Code)
		}
		return "", err
	}
	s.creds.AccessToken = tokens.AccessToken
	s.creds.ExpiresIn = tokens.ExpiresIn
	s.creds.ObtainedAt = time.Now().Unix()
	if tokens.RefreshToken != "" {
		// Honour rotation: an IdP that returns a new refresh token has
		// invalidated the old one, so keeping it would break the next refresh.
		s.creds.RefreshToken = tokens.RefreshToken
	}
	if err := saveCredentials(s.credsPath, s.creds); err != nil {
		// The refresh itself worked; failing to persist only costs one extra
		// refresh at next start. Warn, never fail the request on it.
		fmt.Fprintf(os.Stderr, "warning: could not persist refreshed credential: %v\n", err)
	}
	return s.creds.AccessToken, nil
}

// handler proxies one request. It rewrites the loopback /v1 prefix onto the
// gateway's /v1 base, swaps in a fresh bearer and the recorded x-bill-to, and
// streams the response back unbuffered so SSE completions render token by
// token in opencode.
func (s *shim) handler(w http.ResponseWriter, r *http.Request) {
	accessToken, err := s.token()
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
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

	server := &http.Server{
		Addr:    addr,
		Handler: http.HandlerFunc(newShim(creds, credsPath).handler),
		// Bound only the header read: a streamed completion is long-lived by
		// design, so a write or idle deadline here would cut it off.
		ReadHeaderTimeout: 10 * time.Second,
	}
	fmt.Fprintf(os.Stderr, "pystino shim on http://%s (opencode baseURL http://%s/v1)\n", addr, addr)
	fmt.Fprintf(os.Stderr, "forwarding to %s as the enrolled user, billing group %s\n", creds.Gateway, creds.Group)
	fmt.Fprintf(os.Stderr, "spend is not visible here — /v1 has no usage surface; watch it in the console.\n")
	return server.ListenAndServe()
}
