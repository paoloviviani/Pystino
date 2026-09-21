package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

// deviceAuthorization is the RFC 8628 §3.2 response. Interval and expiry are
// defaulted here, not at use sites: an IdP that omits them gets the
// specification's own floor (5s polling) and its suggested lifetime (600s),
// so a sparse response still produces a correct loop.
type deviceAuthorization struct {
	DeviceCode              string `json:"device_code"`
	UserCode                string `json:"user_code"`
	VerificationURI         string `json:"verification_uri"`
	VerificationURIComplete string `json:"verification_uri_complete"`
	ExpiresIn               int    `json:"expires_in"`
	Interval                int    `json:"interval"`
}

// requestDeviceCode starts the device flow. Errors carry the endpoint's own
// RFC 6749 words (invalid_client, ...) via the same typed error the token
// endpoint produces, so a refused client_id reads as the refusal it is.
func requestDeviceCode(ctx context.Context, endpoint, clientID string) (*deviceAuthorization, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint,
		strings.NewReader(url.Values{"client_id": {clientID}, "scope": {enrollScopes}}.Encode()))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	resp, err := httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("device authorization request: %w", err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return nil, fmt.Errorf("reading device authorization response: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, oauthErrorFromBody("device authorization endpoint", body, resp.StatusCode)
	}
	var authz deviceAuthorization
	if err := json.Unmarshal(body, &authz); err != nil {
		return nil, fmt.Errorf("parsing device authorization response: %w", err)
	}
	if authz.DeviceCode == "" || authz.UserCode == "" {
		return nil, fmt.Errorf("device authorization response is missing the device or user code")
	}
	if authz.VerificationURI == "" && authz.VerificationURIComplete == "" {
		return nil, fmt.Errorf("device authorization response names no verification URI")
	}
	// A response carrying only verification_uri_complete still names a page:
	// fall back rather than refusing a IdP that is merely terse.
	if authz.VerificationURI == "" {
		authz.VerificationURI = authz.VerificationURIComplete
	}
	if authz.Interval <= 0 {
		authz.Interval = 5
	}
	if authz.ExpiresIn <= 0 {
		authz.ExpiresIn = 600
	}
	return &authz, nil
}

// deviceFlowHooks isolates the two things a unit test replaces: the wall
// clock and the wait between polls. Production hooks sleep the real
// interval and read the real clock; tests no-op the sleep and move time by
// hand, so the poll loop is exercised in milliseconds.
type deviceFlowHooks struct {
	sleep func(context.Context, time.Duration) error
	now   func() time.Time
}

func defaultDeviceHooks() deviceFlowHooks {
	return deviceFlowHooks{
		sleep: func(ctx context.Context, d time.Duration) error {
			timer := time.NewTimer(d)
			defer timer.Stop()
			select {
			case <-timer.C:
				return nil
			case <-ctx.Done():
				return ctx.Err()
			}
		},
		now: time.Now,
	}
}

func runDeviceFlow(ctx context.Context, doc *discovery, clientID string) (*tokenSet, error) {
	return runDeviceFlowWithHooks(ctx, doc, clientID, defaultDeviceHooks())
}

// runDeviceFlowWithHooks polls until the human approves at the verification
// URI, the code expires, or the issuer refuses. The sleep comes before the
// first poll on purpose: RFC 8628 §3.5 says the client waits at least
// interval between polls, and polling immediately after printing the code
// races the human who has not reached the browser yet.
func runDeviceFlowWithHooks(ctx context.Context, doc *discovery, clientID string, hooks deviceFlowHooks) (*tokenSet, error) {
	authz, err := requestDeviceCode(ctx, doc.DeviceAuthorizationEndpoint, clientID)
	if err != nil {
		return nil, err
	}
	interval := time.Duration(authz.Interval) * time.Second
	deadline := hooks.now().Add(time.Duration(authz.ExpiresIn) * time.Second)

	fmt.Fprintf(os.Stderr, "sign in with the device code flow:\n  open:  %s\n  enter: %s\n",
		authz.VerificationURI, authz.UserCode)
	if hasDisplay() {
		if authz.VerificationURIComplete != "" {
			openBrowser(authz.VerificationURIComplete)
		} else {
			openBrowser(authz.VerificationURI)
		}
	}

	for {
		if hooks.now().After(deadline) {
			return nil, fmt.Errorf("the user code %s expired before approval; run 'enroll enroll' again", authz.UserCode)
		}
		if err := hooks.sleep(ctx, interval); err != nil {
			return nil, fmt.Errorf("waiting for approval: %w", err)
		}
		tokens, err := exchange(ctx, doc.TokenEndpoint, url.Values{
			"grant_type":  {"urn:ietf:params:oauth:grant-type:device_code"},
			"device_code": {authz.DeviceCode},
			"client_id":   {clientID},
		})
		if err == nil {
			return tokens, nil
		}
		var oerr *oauth2Error
		// Transport and parse failures are not polling states: retrying them
		// would mask an unreachable issuer as "still waiting".
		if !errors.As(err, &oerr) {
			return nil, err
		}
		switch oerr.Code {
		case "authorization_pending":
			continue
		case "slow_down":
			// §3.5: the penalty is exactly 5 seconds, added to the interval
			// the issuer advertised — not a doubling we invent.
			interval += 5 * time.Second
			continue
		case "expired_token":
			return nil, fmt.Errorf("the user code %s expired before approval; run 'enroll enroll' again", authz.UserCode)
		case "access_denied":
			return nil, fmt.Errorf("the sign-in was denied at the identity provider")
		default:
			return nil, err
		}
	}
}
