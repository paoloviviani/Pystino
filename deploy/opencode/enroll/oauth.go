package main

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"time"
)

// discovery is the slice of the IdP metadata this client actually uses. The
// device endpoint is optional: older IdPs predate RFC 8628, and its absence
// is what steers a headless box back toward the loopback error, not a guess.
type discovery struct {
	AuthorizationEndpoint       string `json:"authorization_endpoint"`
	TokenEndpoint               string `json:"token_endpoint"`
	DeviceAuthorizationEndpoint string `json:"device_authorization_endpoint"`
}

// tokenSet is one successful token response, both flows alike. expiresIn
// arrives in seconds; an IdP that omits it gets the conservative default so
// the shim refreshes early rather than serving a dead token.
type tokenSet struct {
	AccessToken  string `json:"access_token"`
	RefreshToken string `json:"refresh_token"`
	ExpiresIn    int    `json:"expires_in"`
}

const defaultExpiresIn = 300

// httpClient is the one place timeouts are set: every IdP and gateway call
// goes through here, so no flow can hang forever on a black-holed address.
var httpClient = &http.Client{Timeout: 30 * time.Second}

func fetchDiscovery(ctx context.Context, issuer string) (*discovery, error) {
	metaURL := strings.TrimSuffix(issuer, "/") + "/.well-known/openid-configuration"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, metaURL, nil)
	if err != nil {
		return nil, err
	}
	resp, err := httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("fetching discovery from %s: %w", metaURL, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("discovery at %s answered HTTP %d", metaURL, resp.StatusCode)
	}
	var doc discovery
	if err := json.NewDecoder(resp.Body).Decode(&doc); err != nil {
		return nil, fmt.Errorf("parsing discovery document: %w", err)
	}
	if doc.TokenEndpoint == "" {
		return nil, fmt.Errorf("discovery document names no token endpoint")
	}
	return &doc, nil
}

// pkceVerifier mints a high-entropy code verifier. The unreserved alphabet
// keeps it legal in both the query string and the token POST body with no
// further encoding to get wrong.
func pkceVerifier() (string, error) {
	raw := make([]byte, 48)
	if _, err := rand.Read(raw); err != nil {
		return "", fmt.Errorf("minting PKCE verifier: %w", err)
	}
	return base64.RawURLEncoding.EncodeToString(raw), nil
}

// pkceChallenge derives the S256 challenge. Plain PKCE is never offered: the
// client is public, so the verifier is the only per-flow secret it has.
func pkceChallenge(verifier string) string {
	sum := sha256.Sum256([]byte(verifier))
	return base64.RawURLEncoding.EncodeToString(sum[:])
}

// randomHex is state/nonce material: unpredictable per flow, single use.
func randomHex(bytes int) (string, error) {
	raw := make([]byte, bytes)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(raw), nil
}

// exchange posts one token request and decodes the success body. Error bodies
// follow RFC 6749 (error/error_description), which is what the caller sees;
// anything else is reported by status so a proxy's HTML never parses as JSON.
func exchange(tokenEndpoint string, form url.Values) (*tokenSet, error) {
	resp, err := httpClient.PostForm(tokenEndpoint, form)
	if err != nil {
		return nil, fmt.Errorf("token request: %w", err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return nil, fmt.Errorf("reading token response: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		var oauthErr struct {
			Error       string `json:"error"`
			Description string `json:"error_description"`
		}
		if json.Unmarshal(body, &oauthErr) == nil && oauthErr.Error != "" {
			return nil, fmt.Errorf("token endpoint refused: %s (%s)", oauthErr.Error, oauthErr.Description)
		}
		return nil, fmt.Errorf("token endpoint answered HTTP %d", resp.StatusCode)
	}
	var tokens tokenSet
	if err := json.Unmarshal(body, &tokens); err != nil {
		return nil, fmt.Errorf("parsing token response: %w", err)
	}
	if tokens.AccessToken == "" {
		return nil, fmt.Errorf("token response carried no access token")
	}
	if tokens.ExpiresIn <= 0 {
		tokens.ExpiresIn = defaultExpiresIn
	}
	return &tokens, nil
}

// loopbackCallback waits for the single authorize redirect. The listener is
// already bound (ephemeral port) so the redirect URI is exact before the
// browser ever opens; anything that is not our state or not a code is a
// refusal, never a guess.
func loopbackCallback(listener net.Listener, state string, timeout time.Duration) (string, error) {
	type result struct {
		code string
		err  error
	}
	done := make(chan result, 1)
	server := &http.Server{
		Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path != "/callback" {
				http.NotFound(w, r)
				return
			}
			query := r.URL.Query()
			if query.Get("state") != state {
				done <- result{err: fmt.Errorf("callback state mismatch: not the flow this client started")}
				fmt.Fprint(w, "Login failed: mismatched state. You can close this tab.")
				return
			}
			if oauthErr := query.Get("error"); oauthErr != "" {
				done <- result{err: fmt.Errorf("identity provider refused: %s (%s)", oauthErr, query.Get("error_description"))}
				fmt.Fprint(w, "Login refused by the identity provider. You can close this tab.")
				return
			}
			code := query.Get("code")
			if code == "" {
				done <- result{err: fmt.Errorf("callback carried no code")}
				fmt.Fprint(w, "Login failed: no code. You can close this tab.")
				return
			}
			done <- result{code: code}
			fmt.Fprint(w, "Signed in — return to the terminal. You can close this tab.")
		}),
		ReadHeaderTimeout: 10 * time.Second,
	}
	go func() {
		_ = server.Serve(listener)
	}()
	defer server.Close()
	select {
	case res := <-done:
		return res.code, res.err
	case <-time.After(timeout):
		return "", fmt.Errorf("timed out waiting for the browser callback")
	}
}

// openBrowser is best effort on every platform: the URL is always printed,
// so a box whose opener is missing (or lies) still completes by pasting.
func openBrowser(url string) {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "darwin":
		cmd = exec.Command("open", url)
	case "windows":
		cmd = exec.Command("rundll32", "url.dll,FileProtocolHandler", url)
	default:
		if os.Getenv("DISPLAY") == "" && os.Getenv("WAYLAND_DISPLAY") == "" {
			return
		}
		cmd = exec.Command("xdg-open", url)
	}
	_ = cmd.Start()
}

// runLoopbackFlow performs the authorization-code + PKCE exchange against a
// loopback redirect. The ephemeral port is bound first so redirect_uri is
// exact — the IdPs register loopback-with-any-port, and the contract is this
// shape: http://127.0.0.1:<port>/callback.
func runLoopbackFlow(ctx context.Context, doc *discovery, clientID string) (*tokenSet, error) {
	if doc.AuthorizationEndpoint == "" {
		return nil, fmt.Errorf("no authorization endpoint in discovery: this IdP cannot do the browser flow")
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return nil, fmt.Errorf("binding loopback listener: %w", err)
	}
	defer listener.Close()
	redirectURI := "http://" + listener.Addr().String() + "/callback"

	verifier, err := pkceVerifier()
	if err != nil {
		return nil, err
	}
	state, err := randomHex(16)
	if err != nil {
		return nil, fmt.Errorf("minting state: %w", err)
	}
	nonce, err := randomHex(16)
	if err != nil {
		return nil, fmt.Errorf("minting nonce: %w", err)
	}

	authQuery := url.Values{
		"response_type":         {"code"},
		"client_id":             {clientID},
		"redirect_uri":          {redirectURI},
		"scope":                 {"openid profile email groups"},
		"state":                 {state},
		"nonce":                 {nonce},
		"code_challenge":        {pkceChallenge(verifier)},
		"code_challenge_method": {"S256"},
	}
	authURL := doc.AuthorizationEndpoint + "?" + authQuery.Encode()
	fmt.Fprintf(os.Stderr, "opening your browser at:\n  %s\n", authURL)
	openBrowser(authURL)

	code, err := loopbackCallback(listener, state, 5*time.Minute)
	if err != nil {
		return nil, err
	}
	return exchange(doc.TokenEndpoint, url.Values{
		"grant_type":    {"authorization_code"},
		"code":          {code},
		"redirect_uri":  {redirectURI},
		"client_id":     {clientID},
		"code_verifier": {verifier},
	})
}

// hasDisplay reports whether a browser opener has any chance of working.
// darwin and windows always can; elsewhere a display server must be present.
func hasDisplay() bool {
	if runtime.GOOS == "darwin" || runtime.GOOS == "windows" {
		return true
	}
	return os.Getenv("DISPLAY") != "" || os.Getenv("WAYLAND_DISPLAY") != ""
}
