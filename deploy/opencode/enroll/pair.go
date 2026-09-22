package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"strings"
	"time"
)

const pairUsage = `enroll pair — pair this machine's daemon into the chat's /code panel.

Usage:
  enroll pair [--chat URL] [--name NAME] [--creds PATH]

  --chat   The chat's base URL, e.g. https://llm.example.org/chat. Default:
           derived from the stored gateway (the deployment serves the chat
           beside the gateway, at <gateway origin>/chat).
  --name   Name shown for this machine in the panel (default: hostname).
  --creds  Credential file written by 'enroll enroll' (default
           <config-dir>/opencode/pystino-credentials.json).

Authenticates to the chat with the enrollment's own access token (the same
IdP the chat trusts), runs 'paseo daemon pair --json' for the pairing offer,
and POSTs it to the chat. The manual path stays: 'paseo daemon pair' prints
a link that a human can paste into the panel.
`

type pairOptions struct {
	chat  string
	name  string
	creds string
}

func runPair(args []string) error {
	fs := flagSetWithHelp("pair", pairUsage)
	opts := pairOptions{}
	fs.StringVar(&opts.chat, "chat", "", "")
	fs.StringVar(&opts.name, "name", "", "")
	fs.StringVar(&opts.creds, "creds", "", "")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() > 0 {
		return fmt.Errorf("unexpected arguments: %s", strings.Join(fs.Args(), " "))
	}
	return pair(context.Background(), &opts)
}

func pair(ctx context.Context, opts *pairOptions) error {
	// Every network step inside carries its own timeout; this bounds the
	// whole run, chiefly so a wedged `paseo daemon pair` cannot hang a
	// setup script on it.
	ctx, cancel := context.WithTimeout(ctx, pairDeadline)
	defer cancel()

	credsPath := opts.creds
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

	// A pairing run can happen days after enrollment: refresh inside the
	// skew so the bearer the POST carries is fresh enough for the chat's
	// userinfo validation. Failure names the remedy, same words as the shim.
	if creds.needsRefresh(time.Now()) {
		if err := refreshCredential(credsPath, creds); err != nil {
			return err
		}
	}

	chatURL, err := resolveChat(opts.chat, creds.Gateway)
	if err != nil {
		return err
	}

	name := opts.name
	if name == "" {
		hostname, err := os.Hostname()
		if err != nil {
			return fmt.Errorf("resolving this machine's name: %w (pass --name)", err)
		}
		name = hostname
	}

	offerURL, err := fetchPairingOffer(ctx)
	if err != nil {
		return err
	}
	// Decode before sending: a malformed offer fails here, on the machine
	// that produced it, rather than as a 400 the operator must correlate.
	if _, err := extractOffer(offerURL); err != nil {
		return err
	}

	device, err := postPairing(ctx, chatURL, creds.AccessToken, name, offerURL)
	if err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, "paired %s\n", device)
	fmt.Fprintf(os.Stderr, "find this machine in the chat's /code panel.\n")
	return nil
}

// resolveChat turns the --chat flag (or the stored gateway) into the chat's
// base URL. The deployment's chat lives beside the gateway on one origin —
// the gateway answers /v1, the chat answers /chat — and the stored gateway is
// that origin's /v1 base, so the derivation is: drop /v1, append /chat.
func resolveChat(flagValue, gateway string) (string, error) {
	raw := strings.TrimSpace(flagValue)
	if raw == "" {
		origin, err := gatewayOrigin(gateway)
		if err != nil {
			return "", err
		}
		raw = origin + "/chat"
	}
	parsed, err := url.Parse(raw)
	if err != nil || parsed.Scheme == "" || parsed.Host == "" {
		return "", fmt.Errorf("--chat must be an absolute http(s) URL: %s", raw)
	}
	return strings.TrimSuffix(raw, "/"), nil
}

// gatewayOrigin strips the /v1 base the enroll flow normalized the gateway
// into. A gateway without the /v1 suffix is tolerated (trimmed of any
// trailing slash as-is); anything that is not http(s) at all is a credential
// file that predates the shape this command needs.
func gatewayOrigin(gateway string) (string, error) {
	if gateway == "" {
		return "", fmt.Errorf("no gateway in the stored credential — re-run 'enroll enroll', or pass --chat")
	}
	origin := strings.TrimSuffix(gateway, "/v1")
	origin = strings.TrimSuffix(origin, "/")
	if !strings.HasPrefix(origin, "http://") && !strings.HasPrefix(origin, "https://") {
		return "", fmt.Errorf("stored gateway %q is not an http(s) URL — re-run 'enroll enroll', or pass --chat", gateway)
	}
	return origin, nil
}

// pairingOffer is the slice of the offer JSON this command checks before
// sending anything. The relay block rides along but is deliberately not
// read: which relay to dial is the chat's deployment decision, not the
// offer's (the chat probes through its own configured relay).
type pairingOffer struct {
	V                  any    `json:"v"`
	ServerID           string `json:"serverId"`
	DaemonPublicKeyB64 string `json:"daemonPublicKeyB64"`
}

// extractOffer decodes the offer out of the pairing URL — the `#offer=`
// fragment, base64url-encoded JSON (the SDK emits Node's "base64url":
// unpadded). A fragment that yields no daemon identity is a broken offer,
// and sending it would only earn a 400 from the chat.
func extractOffer(pairingURL string) (pairingOffer, error) {
	i := strings.Index(pairingURL, "#offer=")
	if i == -1 {
		return pairingOffer{}, fmt.Errorf("the pairing output carries no #offer= fragment")
	}
	encoded := pairingURL[i+len("#offer="):]
	// Nothing after the fragment belongs to it — a QR-encoded URL can trail
	// query noise, and the fragment ends at the first ? or &.
	if cut := strings.IndexAny(encoded, "?&"); cut != -1 {
		encoded = encoded[:cut]
	}
	decoded, err := base64.RawURLEncoding.DecodeString(encoded)
	if err != nil {
		// The daemon does not pad; tolerate a padded fragment anyway, the
		// way the chat's own parser does.
		decoded, err = base64.URLEncoding.DecodeString(encoded)
	}
	if err != nil {
		return pairingOffer{}, fmt.Errorf("the pairing offer's fragment is not base64url: %w", err)
	}
	var offer pairingOffer
	if err := json.Unmarshal(decoded, &offer); err != nil {
		return pairingOffer{}, fmt.Errorf("the pairing offer's fragment is not readable JSON: %w", err)
	}
	if offer.ServerID == "" || offer.DaemonPublicKeyB64 == "" {
		return pairingOffer{}, fmt.Errorf("the pairing offer is missing the daemon's relay identity (serverId / daemonPublicKeyB64)")
	}
	return offer, nil
}

// pairCLIError is the structured error `paseo daemon pair` prints to stdout
// on a refused pairing attempt (observed shape: {code, message, action}),
// typed so the relay-disabled case can be answered with the actual remedy.
type pairCLIError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
	Action  string `json:"action"`
}

// fetchPairingOffer runs `paseo daemon pair --json` and returns the pairing
// URL it printed. The CLI is only the offer's courier — the daemon mints the
// identity — so the offer must come from the daemon, never be synthesized.
func fetchPairingOffer(ctx context.Context) (string, error) {
	out, err := exec.CommandContext(ctx, "paseo", "daemon", "pair", "--json").Output()
	var stderr []byte
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		stderr = exitErr.Stderr
	}
	// The relay-off case answers RELAY_DISABLED (structured JSON, exit 1) —
	// observed on stderr for one daemon and on stdout for another, so both
	// streams are checked. Its remedy here is the relay wiring — what
	// setup-agent.sh wrote — not the daemon's own "run with --relay" hint,
	// which a launch-override daemon answers with a second error.
	for _, stream := range [][]byte{out, stderr} {
		var cliErr pairCLIError
		if json.Unmarshal(stream, &cliErr) == nil && cliErr.Code == "RELAY_DISABLED" {
			return "", fmt.Errorf("the daemon's relay is not enabled, so the chat could never reach this machine: enable it in the daemon config (setup-agent.sh writes the relay block) and restart the daemon, then re-run 'enroll pair'")
		}
	}
	if err != nil {
		if len(stderr) > 0 {
			return "", fmt.Errorf("'paseo daemon pair' failed: %s", strings.TrimSpace(string(stderr)))
		}
		if exitErr != nil {
			return "", fmt.Errorf("'paseo daemon pair' failed (exit %d): is the daemon running? ('paseo daemon status')", exitErr.ExitCode())
		}
		return "", fmt.Errorf("running 'paseo daemon pair': %w — is the paseo CLI on PATH?", err)
	}
	var pairing struct {
		RelayEnabled bool    `json:"relayEnabled"`
		URL          *string `json:"url"`
		QR           *string `json:"qr"`
	}
	if err := json.Unmarshal(out, &pairing); err != nil {
		return "", fmt.Errorf("could not parse 'paseo daemon pair --json' output: %w", err)
	}
	// The relay must be wired before an offer exists at all: without it the
	// daemon is unreachable from the chat, and pairing would record a row
	// nobody can use.
	if !pairing.RelayEnabled || pairing.URL == nil || *pairing.URL == "" {
		return "", fmt.Errorf("the daemon reported no relay pairing offer — enable the relay in the daemon config (setup-agent.sh writes the relay block) and restart the daemon, then re-run 'enroll pair'")
	}
	return *pairing.URL, nil
}

// pairRequestBody is what the machine endpoint expects: the display name and
// the pairing URL verbatim — byte-for-byte what the daemon printed, the same
// thing a human would paste into the panel.
type pairRequestBody struct {
	Name  string `json:"name"`
	Offer string `json:"offer"`
}

// postPairing sends the offer to the chat's machine endpoint and returns a
// short description of the paired device. Accept: application/json matters
// twice — SvelteKit serializes endpoint errors as {"message": ...} only for
// JSON-negotiating callers, and an HTML error page would be unreadable here.
func postPairing(ctx context.Context, chatURL, accessToken, name, offerURL string) (string, error) {
	body, err := json.Marshal(pairRequestBody{Name: name, Offer: offerURL})
	if err != nil {
		return "", err
	}
	endpoint := strings.TrimSuffix(chatURL, "/") + "/api/v2/code/enroll/machine"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, strings.NewReader(string(body)))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Authorization", "Bearer "+accessToken)
	resp, err := httpClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("reaching the chat at %s: %w", endpoint, err)
	}
	defer resp.Body.Close()
	respBody, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return "", fmt.Errorf("reading the chat's answer: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		return "", pairHTTPError(resp.StatusCode, respBody)
	}
	// The endpoint answers superjson (the chat's /code wire format): the
	// useful payload sits under "json", with a "meta" sidecar describing
	// types this command does not need (the device view is strings and ids).
	var parsed struct {
		JSON struct {
			Device struct {
				ID     string `json:"id"`
				Name   string `json:"name"`
				Status string `json:"status"`
			} `json:"device"`
		} `json:"json"`
	}
	if err := json.Unmarshal(respBody, &parsed); err != nil {
		return "", fmt.Errorf("parsing the chat's answer: %w", err)
	}
	device := parsed.JSON.Device
	if device.ID == "" {
		return "", fmt.Errorf("the chat's answer named no device; the pairing state is unknown — check the /code panel")
	}
	return fmt.Sprintf("%q into the chat's /code panel (device %s, %s)", device.Name, device.ID, device.Status), nil
}

// pairHTTPError maps the endpoint's failures onto the remedy that actually
// applies, folding in the server's own message when it sent one (SvelteKit
// errors carry "message"; the login wall's rejections carry "error").
func pairHTTPError(status int, body []byte) error {
	var parsed struct {
		Message string `json:"message"`
		Error   string `json:"error"`
	}
	_ = json.Unmarshal(body, &parsed)
	serverMsg := parsed.Message
	if serverMsg == "" {
		serverMsg = parsed.Error
	}
	detail := ""
	if serverMsg != "" {
		detail = ": " + serverMsg
	}
	switch status {
	case http.StatusUnauthorized:
		return fmt.Errorf("the chat refused the access token (401%s) — the sign-in expired; run 'enroll enroll' again", detail)
	case http.StatusNotFound:
		// Two different 404s reach here: the endpoint's "this token identifies
		// nobody the chat knows" (its message names the remedy), and one from
		// an older deployment or an unconfigured relay — where the identity
		// hint would be wrong, so the server's own message carries it.
		if strings.Contains(strings.ToLower(serverMsg), "log into the chat") {
			return fmt.Errorf("the chat does not know this identity (404) — log into the chat once in a browser, then re-run 'enroll pair'")
		}
		return fmt.Errorf("the chat answered 404%s — check the deployment (a chat without the machine endpoint, or no relay configured)", detail)
	case http.StatusBadGateway:
		return fmt.Errorf("the pairing probe failed (502%s) — check the daemon is running ('paseo daemon status') and the relay is reachable", detail)
	default:
		return fmt.Errorf("the chat refused the pairing (HTTP %d%s)", status, detail)
	}
}

// pairDeadline bounds the whole run: every network step inside carries its
// own timeout, so this only guards a wedged `paseo daemon pair`.
const pairDeadline = 2 * time.Minute
