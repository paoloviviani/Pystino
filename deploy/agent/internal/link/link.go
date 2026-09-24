// Package link is the WSS client that dials Cerea (PROTOCOL.md §3): hello,
// welcome, status, req/res, event pushes, credential-state and token-renewal
// frames, with reconnect/backoff and the machine's own confirmation gate
// (nothing is forwarded to the Handler until Cerea says "paired").
package link

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math/rand"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/coder/websocket"
)

// Credential is the single in-process source of truth for the access token
// the link authenticates with (R7): the same source the shim's HTTP
// handler uses, so the shim and the link never disagree about the
// credential's state or race each other refreshing it.
type Credential interface {
	// Token returns a currently valid access token, refreshing first if
	// the cached one is within its skew of expiry.
	Token(ctx context.Context) (string, error)
	// ForceRefresh refreshes regardless of the cached token's remaining
	// lifetime — used after Cerea answers 4401, which means the token it
	// saw was already unacceptable, skew or not.
	ForceRefresh(ctx context.Context) (string, error)
	// NextRenewal is when the link should proactively renew and push a
	// fresh auth frame — nominally ~70% of the current token's lifetime.
	NextRenewal() time.Time
}

// AgentInfo, BackendInfo and PolicyInfo mirror the hello frame's nested
// objects (PROTOCOL.md §5) without this package importing internal/backend
// or internal/policy — the caller assembling a Hello already holds real
// values from both and flattens them here, so link stays a pure transport.
type AgentInfo struct {
	Version  string `json:"version"`
	OS       string `json:"os"`
	Arch     string `json:"arch"`
	Hostname string `json:"hostname"`
}

type BackendInfo struct {
	ID           string          `json:"id"`
	Version      string          `json:"version"`
	Capabilities map[string]bool `json:"capabilities"`
}

type PolicyInfo struct {
	AutoAccept      string   `json:"autoAccept"`
	WorkspaceRoots  []string `json:"workspaceRoots"`
	AllowFreeModels bool     `json:"allowFreeModels"`
}

// Hello is the whole first frame's payload (protocol and type are added by
// the link itself).
type Hello struct {
	Agent          AgentInfo     `json:"agent"`
	Backends       []BackendInfo `json:"backends"`
	Policy         PolicyInfo    `json:"policy"`
	CredentialInfo string        `json:"-"` // set via SetCredentialState before each dial
}

// OpError is a req's refusal, encoded as the res frame's "error" object.
// Code is one of PROTOCOL.md §5's fixed set: not_found, invalid, forbidden,
// unavailable, backend, unsupported.
type OpError struct {
	Code    string
	Message string
}

func (e *OpError) Error() string { return e.Message }

// Handler answers one req frame's op. It runs in its own goroutine per
// request under a per-op deadline the link enforces via ctx; result is
// json.Marshal'd into the res frame's "result" field on success.
type Handler interface {
	Handle(ctx context.Context, op string, args json.RawMessage) (result any, opErr *OpError)
}

// Config is everything one Link needs. Defaults are applied by New for
// zero-valued timeouts/backoff so a caller only sets what it wants to
// override.
type Config struct {
	// CereaOrigin is the origin Cerea serves the link endpoint on, e.g.
	// "https://cerea.example.org" or "http://127.0.0.1:PORT" in tests. The
	// dial target is CereaOrigin + "/api/v2/code/machine", with the scheme
	// swapped for ws/wss.
	CereaOrigin string
	MachineID   string
	MachineName string
	Cred        Credential
	Hello       func() Hello
	Handler     Handler

	// DialTimeout bounds one connection attempt (default 10s, PROTOCOL.md §3).
	DialTimeout time.Duration
	// MinBackoff/MaxBackoff bound reconnect backoff (default 1s / 30s).
	MinBackoff time.Duration
	MaxBackoff time.Duration
	// DefaultOpTimeout/SyncOpTimeout bound req handling (default 15s / 20s).
	DefaultOpTimeout time.Duration
	SyncOpTimeout    time.Duration

	// Logf receives lifecycle-only messages (connecting, connected, paired,
	// reconnecting, giving up) — never frame contents (R6). Defaults to a
	// no-op.
	Logf func(format string, args ...any)
}

// errGiveUp signals Run to stop reconnecting for good (4403: revoked or
// otherwise permanently forbidden).
var errGiveUp = errors.New("link: forbidden, giving up")

// ErrExpired signals the caller that credential refresh (on a 4401) itself
// failed permanently — the caller (run's top level) is expected to react
// the way serve.go's shim already does to a dead credential.
var ErrExpired = errors.New("link: credential refresh failed")

// Link is one machine's connection to Cerea. Not safe for concurrent Run
// calls; everything else is.
type Link struct {
	cfg Config

	mu      sync.Mutex
	paired  bool
	conn    *websocket.Conn
	connCtx context.Context // valid only while conn != nil; for writes issued from other goroutines
	writeMu sync.Mutex
}

func New(cfg Config) *Link {
	if cfg.DialTimeout == 0 {
		cfg.DialTimeout = 10 * time.Second
	}
	if cfg.MinBackoff == 0 {
		cfg.MinBackoff = time.Second
	}
	if cfg.MaxBackoff == 0 {
		cfg.MaxBackoff = 30 * time.Second
	}
	if cfg.DefaultOpTimeout == 0 {
		cfg.DefaultOpTimeout = 15 * time.Second
	}
	if cfg.SyncOpTimeout == 0 {
		cfg.SyncOpTimeout = 20 * time.Second
	}
	if cfg.Logf == nil {
		cfg.Logf = func(string, ...any) {}
	}
	return &Link{cfg: cfg}
}

// Paired reports whether Cerea has confirmed this machine (PROTOCOL.md §4):
// before that, every req is refused without reaching the Handler.
func (l *Link) Paired() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.paired
}

// Run dials, serves, and reconnects with backoff until ctx is cancelled or
// Cerea closes with 4403 (revoked — PROTOCOL.md §3/§4), which Run treats as
// permanent and returns errGiveUp for.
func (l *Link) Run(ctx context.Context) error {
	backoff := l.cfg.MinBackoff
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		err := l.runOnce(ctx)
		if errors.Is(err, errGiveUp) {
			l.cfg.Logf("link: machine revoked, not reconnecting")
			return err
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
		l.cfg.Logf("link: disconnected, reconnecting in %s", backoff)
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(jitter(backoff)):
		}
		backoff *= 2
		if backoff > l.cfg.MaxBackoff {
			backoff = l.cfg.MaxBackoff
		}
	}
}

func jitter(d time.Duration) time.Duration {
	// +/- 20%, so many machines reconnecting after the same outage don't
	// all knock at once.
	delta := time.Duration(rand.Int63n(int64(d)*2/5)) - d*2/10
	return d + delta
}

func dialURL(origin string) (string, error) {
	origin = strings.TrimSuffix(origin, "/")
	switch {
	case strings.HasPrefix(origin, "https://"):
		return "wss://" + strings.TrimPrefix(origin, "https://") + "/api/v2/code/machine", nil
	case strings.HasPrefix(origin, "http://"):
		return "ws://" + strings.TrimPrefix(origin, "http://") + "/api/v2/code/machine", nil
	default:
		return "", fmt.Errorf("cerea origin must be http(s): %q", origin)
	}
}

// runOnce performs exactly one connection's lifecycle: dial, hello,
// welcome, then serve frames until the connection ends.
func (l *Link) runOnce(ctx context.Context) error {
	target, err := dialURL(l.cfg.CereaOrigin)
	if err != nil {
		return err
	}
	token, err := l.cfg.Cred.Token(ctx)
	if err != nil {
		return fmt.Errorf("getting access token: %w", err)
	}

	dialCtx, cancel := context.WithTimeout(ctx, l.cfg.DialTimeout)
	defer cancel()
	header := http.Header{}
	header.Set("Authorization", "Bearer "+token)
	header.Set("X-Pystino-Machine-Id", l.cfg.MachineID)
	header.Set("X-Pystino-Machine-Name", l.cfg.MachineName)

	l.cfg.Logf("link: dialing %s", l.cfg.CereaOrigin)
	conn, _, err := websocket.Dial(dialCtx, target, &websocket.DialOptions{
		HTTPHeader:   header,
		Subprotocols: []string{"pystino-machine.v1"},
	})
	if err != nil {
		return fmt.Errorf("dialing cerea: %w", err)
	}
	defer conn.CloseNow()

	connCtx, connCancel := context.WithCancel(ctx)
	defer connCancel()

	l.mu.Lock()
	l.conn = conn
	l.connCtx = connCtx
	l.paired = false
	l.mu.Unlock()
	defer func() {
		l.mu.Lock()
		l.conn = nil
		l.paired = false
		l.mu.Unlock()
	}()

	hello := l.cfg.Hello()
	if err := l.writeFrame(connCtx, map[string]any{
		"type":     "hello",
		"protocol": 1,
		"agent":    hello.Agent,
		"backends": hello.Backends,
		"policy":   hello.Policy,
		"credential": map[string]string{
			"state": defaultString(hello.CredentialInfo, "ok"),
		},
	}); err != nil {
		return fmt.Errorf("sending hello: %w", err)
	}

	var welcome struct {
		Type     string `json:"type"`
		DeviceID string `json:"deviceId"`
		Status   string `json:"status"`
	}
	if err := l.readFrame(connCtx, &welcome); err != nil {
		return fmt.Errorf("reading welcome: %w", err)
	}
	if welcome.Type != "welcome" {
		return fmt.Errorf("expected welcome, got %q", welcome.Type)
	}
	l.setPaired(welcome.Status == "paired")
	l.cfg.Logf("link: connected (device %s, status %s)", welcome.DeviceID, welcome.Status)

	renewalDone := make(chan struct{})
	go func() {
		defer close(renewalDone)
		l.renewalLoop(connCtx)
	}()
	defer func() { connCancel(); <-renewalDone }()

	return l.serve(connCtx, conn)
}

func defaultString(s, def string) string {
	if s == "" {
		return def
	}
	return s
}

func (l *Link) setPaired(v bool) {
	l.mu.Lock()
	l.paired = v
	l.mu.Unlock()
}

// renewalLoop proactively renews the access token at ~70% of its lifetime
// (PROTOCOL.md §3) and pushes an auth frame, for as long as this connection
// lives.
func (l *Link) renewalLoop(ctx context.Context) {
	for {
		next := l.cfg.Cred.NextRenewal()
		wait := time.Until(next)
		if wait < 0 {
			wait = 0
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait):
		}
		token, err := l.cfg.Cred.Token(ctx)
		if err != nil {
			l.cfg.Logf("link: token renewal failed: %v", err)
			continue
		}
		if err := l.writeFrame(ctx, map[string]any{"type": "auth", "token": token}); err != nil {
			return
		}
	}
}

// serve reads frames until the connection ends, dispatching each.
func (l *Link) serve(ctx context.Context, conn *websocket.Conn) error {
	for {
		var envelope struct {
			Type string `json:"type"`
		}
		raw, err := l.readRaw(ctx, conn)
		if err != nil {
			var closeErr websocket.CloseError
			if errors.As(err, &closeErr) {
				switch closeErr.Code {
				case 4403:
					return errGiveUp
				case 4401:
					if _, refreshErr := l.cfg.Cred.ForceRefresh(ctx); refreshErr != nil {
						return fmt.Errorf("%w: %v", ErrExpired, refreshErr)
					}
					return fmt.Errorf("link: token rejected (4401), refreshed for next dial")
				}
			}
			return err
		}
		if err := json.Unmarshal(raw, &envelope); err != nil {
			continue // malformed frame: ignore, per PROTOCOL.md §5 forward-compat rule
		}
		switch envelope.Type {
		case "status":
			var status struct {
				Status string `json:"status"`
			}
			if json.Unmarshal(raw, &status) == nil && status.Status == "paired" {
				l.setPaired(true)
				l.cfg.Logf("link: paired")
			}
		case "req":
			var req struct {
				ID   string          `json:"id"`
				Op   string          `json:"op"`
				Args json.RawMessage `json:"args"`
			}
			if json.Unmarshal(raw, &req) != nil {
				continue
			}
			go l.handleReq(ctx, req.ID, req.Op, req.Args)
		default:
			// Unknown frame types are ignored (forward compatibility,
			// PROTOCOL.md §5).
		}
	}
}

func opTimeout(cfg Config, op string) time.Duration {
	if op == "session.sync" {
		return cfg.SyncOpTimeout
	}
	return cfg.DefaultOpTimeout
}

func (l *Link) handleReq(ctx context.Context, id, op string, args json.RawMessage) {
	reqCtx, cancel := context.WithTimeout(ctx, opTimeout(l.cfg, op))
	defer cancel()

	if !l.Paired() {
		l.sendError(ctx, id, "forbidden", "awaiting confirmation in the browser")
		return
	}
	result, opErr := l.cfg.Handler.Handle(reqCtx, op, args)
	if opErr != nil {
		l.sendError(ctx, id, opErr.Code, opErr.Message)
		return
	}
	if err := l.writeFrame(ctx, map[string]any{
		"type": "res", "id": id, "ok": true, "result": result,
	}); err != nil {
		l.cfg.Logf("link: sending res for %s failed: %v", id, err)
	}
}

func (l *Link) sendError(ctx context.Context, id, code, message string) {
	_ = l.writeFrame(ctx, map[string]any{
		"type": "res", "id": id, "ok": false,
		"error": map[string]string{"code": code, "message": message},
	})
}

// PublishEvent sends one event frame. Best-effort: if there is no live
// connection, it returns an error and the caller drops it — the
// materializer's ring buffer and session.sync are the actual gap-recovery
// path (PROTOCOL.md §7), not this send succeeding.
func (l *Link) PublishEvent(sessionID, epoch string, seq int64, event json.RawMessage) error {
	l.mu.Lock()
	ctx := l.connCtx
	l.mu.Unlock()
	if ctx == nil {
		return errors.New("link: not connected")
	}
	return l.writeFrame(ctx, map[string]any{
		"type": "event", "sessionId": sessionID, "epoch": epoch, "seq": seq, "event": json.RawMessage(event),
	})
}

// PublishCredentialState sends a credential frame (PROTOCOL.md §5) — used
// so Cerea learns "expired" before the socket necessarily dies, letting it
// show the re-enroll card immediately rather than waiting for a timeout.
func (l *Link) PublishCredentialState(state, detail string) error {
	l.mu.Lock()
	ctx := l.connCtx
	l.mu.Unlock()
	if ctx == nil {
		return errors.New("link: not connected")
	}
	return l.writeFrame(ctx, map[string]any{"type": "credential", "state": state, "detail": detail})
}

func (l *Link) writeFrame(ctx context.Context, v any) error {
	body, err := json.Marshal(v)
	if err != nil {
		return err
	}
	l.mu.Lock()
	conn := l.conn
	l.mu.Unlock()
	if conn == nil {
		return errors.New("link: not connected")
	}
	l.writeMu.Lock()
	defer l.writeMu.Unlock()
	return conn.Write(ctx, websocket.MessageText, body)
}

func (l *Link) readFrame(ctx context.Context, v any) error {
	l.mu.Lock()
	conn := l.conn
	l.mu.Unlock()
	if conn == nil {
		return errors.New("link: not connected")
	}
	raw, err := l.readRaw(ctx, conn)
	if err != nil {
		return err
	}
	return json.Unmarshal(raw, v)
}

func (l *Link) readRaw(ctx context.Context, conn *websocket.Conn) ([]byte, error) {
	_, raw, err := conn.Read(ctx)
	return raw, err
}
