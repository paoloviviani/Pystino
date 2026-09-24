// Package opencode implements internal/backend.Backend over `opencode
// serve`'s HTTP + SSE API (verified live against 1.18.31; PROTOCOL.md §2/§3
// records why this backend targets the server API rather than `opencode
// acp`). The agent spawns and supervises the opencode process itself: this
// package owns its lifecycle end to end, from picking a port through
// restarting it with backoff if it dies to killing it (and anything it
// spawned) on Stop.
package opencode

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"sync"
	"time"
)

// Config is everything needed to spawn and reach one opencode instance.
type Config struct {
	// Bin is the opencode binary (default "opencode": resolved via PATH).
	Bin string
	// Hostname/Port are what opencode serve binds. Port 0 picks a free one.
	Hostname string
	Port     int
	// Password is OPENCODE_SERVER_PASSWORD (basic auth, user "opencode").
	// A random one is minted if empty.
	Password string
	// ConfigPath, if set, is exported as OPENCODE_CONFIG so a caller (tests,
	// --opencode-config) can point opencode at a specific config file
	// instead of its usual discovery.
	ConfigPath string
	// Env, if non-nil, replaces the child's environment outright (tests use
	// this for an isolated HOME/XDG_*, and to point at a mock LLM). Nil
	// means inherit os.Environ().
	Env []string
	// OverlayPath, if set, persists the per-session mode/model overlay
	// (opencode has no server-side memory of a session's chosen mode/model
	// across prompts) across agent restarts.
	OverlayPath string
	// StartupTimeout bounds Start's wait for the first health check
	// (default 30s). A first run on a cold cache can be slower than that;
	// the integration test overrides it rather than this package assuming
	// every environment is warm.
	StartupTimeout time.Duration
	// Logf receives lifecycle-only messages: started, healthy, restarting,
	// exited (R6 — never frame or response bodies).
	Logf func(format string, args ...any)
}

const (
	defaultStartupTimeout = 30 * time.Second
	healthPollEvery       = 200 * time.Millisecond
	restartMinDelay       = time.Second
	restartMaxDelay       = 30 * time.Second
	stopGrace             = 3 * time.Second
)

// Backend is internal/backend.Backend over one supervised opencode
// process.
type Backend struct {
	cfg Config

	client *http.Client

	mu      sync.Mutex
	cmd     *exec.Cmd
	exited  chan struct{}
	stopped bool

	stopCh chan struct{}
	doneCh chan struct{}

	overlayMu sync.Mutex
	overlay   map[string]sessionOverlay

	// clientMsgMu/clientMessageIDs is the durable half of the
	// clientMessageId mapping (PROTOCOL.md §7): opencode message id ->
	// clientMessageId, persisted alongside the overlay. pendingMu
	// /pendingClientMsg is the transient half — a sessionID -> clientMessageId
	// waiting for Prompt's next new user message to show up in an event —
	// and is not persisted: losing a pending entry to a crash only means
	// one message's id can't be recovered, not a correctness problem.
	clientMsgMu      sync.Mutex
	clientMessageIDs map[string]string
	pendingMu        sync.Mutex
	pendingClientMsg map[string]string

	// modelsMu/modelLimits caches GET /config/providers's context-window
	// hints per model id, so Usage events (which only carry token counts)
	// can fill in ContextMax without a request per event.
	modelsMu   sync.Mutex
	modelLimit map[string]int
}

type sessionOverlay struct {
	ModeID  string `json:"modeId,omitempty"`
	ModelID string `json:"modelId,omitempty"`
}

// New builds a Backend. Start must be called before any other method.
func New(cfg Config) *Backend {
	if cfg.Bin == "" {
		cfg.Bin = "opencode"
	}
	if cfg.Hostname == "" {
		cfg.Hostname = "127.0.0.1"
	}
	if cfg.Logf == nil {
		cfg.Logf = func(string, ...any) {}
	}
	return &Backend{
		cfg:              cfg,
		client:           &http.Client{},
		overlay:          map[string]sessionOverlay{},
		clientMessageIDs: map[string]string{},
		pendingClientMsg: map[string]string{},
		modelLimit:       map[string]int{},
	}
}

// ID/Version implement backend.Backend.
func (b *Backend) ID() string      { return "opencode" }
func (b *Backend) Version() string { return "1.18.31" }

func pickFreePort() (int, error) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port, nil
}

func randomHex(n int) (string, error) {
	raw := make([]byte, n)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return hex.EncodeToString(raw), nil
}

// Start picks a port and password if not already set, launches the
// supervise loop, and waits for the first health check to pass.
func (b *Backend) Start(ctx context.Context) error {
	if b.cfg.Port == 0 {
		port, err := pickFreePort()
		if err != nil {
			return fmt.Errorf("opencode: picking a port: %w", err)
		}
		b.cfg.Port = port
	}
	if b.cfg.Password == "" {
		pw, err := randomHex(16)
		if err != nil {
			return fmt.Errorf("opencode: minting server password: %w", err)
		}
		b.cfg.Password = pw
	}
	if err := b.loadOverlay(); err != nil {
		return err
	}

	b.stopCh = make(chan struct{})
	b.doneCh = make(chan struct{})
	go b.superviseLoop(ctx)

	timeout := b.cfg.StartupTimeout
	if timeout == 0 {
		timeout = defaultStartupTimeout
	}
	if err := b.waitHealthy(ctx, timeout); err != nil {
		return err
	}
	b.cfg.Logf("opencode: healthy on %s", b.baseURL())
	return nil
}

func (b *Backend) baseURL() string {
	return fmt.Sprintf("http://%s:%d", b.cfg.Hostname, b.cfg.Port)
}

// superviseLoop keeps opencode running: launch, wait for it to exit, and —
// unless Stop was called — relaunch after a backoff.
func (b *Backend) superviseLoop(ctx context.Context) {
	defer close(b.doneCh)
	delay := restartMinDelay
	first := true
	for {
		select {
		case <-ctx.Done():
			return
		case <-b.stopCh:
			return
		default:
		}
		err := b.runOnce(ctx)
		select {
		case <-ctx.Done():
			return
		case <-b.stopCh:
			return
		default:
		}
		if !first {
			b.cfg.Logf("opencode: exited (%v), restarting in %s", err, delay)
		} else {
			b.cfg.Logf("opencode: exited before becoming healthy (%v), retrying in %s", err, delay)
		}
		first = false
		select {
		case <-ctx.Done():
			return
		case <-b.stopCh:
			return
		case <-time.After(delay):
		}
		delay *= 2
		if delay > restartMaxDelay {
			delay = restartMaxDelay
		}
	}
}

func (b *Backend) runOnce(ctx context.Context) error {
	args := []string{"serve", "--hostname", b.cfg.Hostname, "--port", fmt.Sprint(b.cfg.Port)}
	cmd := exec.Command(b.cfg.Bin, args...)
	env := b.cfg.Env
	if env == nil {
		env = os.Environ()
	}
	env = append(append([]string{}, env...), "OPENCODE_SERVER_PASSWORD="+b.cfg.Password)
	if b.cfg.ConfigPath != "" {
		env = append(env, "OPENCODE_CONFIG="+b.cfg.ConfigPath)
	}
	cmd.Env = env
	cmd.Stdout = nil
	cmd.Stderr = nil
	setProcAttrs(cmd)

	if err := cmd.Start(); err != nil {
		return fmt.Errorf("starting opencode: %w", err)
	}
	exited := make(chan struct{})
	b.mu.Lock()
	b.cmd = cmd
	b.exited = exited
	b.mu.Unlock()

	err := cmd.Wait()
	close(exited)
	b.mu.Lock()
	b.cmd = nil
	b.mu.Unlock()
	return err
}

func (b *Backend) waitHealthy(ctx context.Context, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	url := b.baseURL() + "/global/health"
	for {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
		if err == nil {
			req.SetBasicAuth("opencode", b.cfg.Password)
			resp, doErr := b.client.Do(req)
			if doErr == nil {
				resp.Body.Close()
				if resp.StatusCode == http.StatusOK {
					return nil
				}
			}
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("opencode: did not become healthy within %s", timeout)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(healthPollEvery):
		}
	}
}

// Stop asks the supervise loop to stop restarting, then SIGTERMs the
// current process (group), escalating to SIGKILL after stopGrace if it
// hasn't exited. Idempotent; safe to call even if Start never completed.
func (b *Backend) Stop() error {
	b.mu.Lock()
	if b.stopped {
		b.mu.Unlock()
		return nil
	}
	b.stopped = true
	stopCh, doneCh := b.stopCh, b.doneCh
	cmd, exited := b.cmd, b.exited
	b.mu.Unlock()

	if stopCh != nil {
		close(stopCh)
	}
	if cmd != nil && cmd.Process != nil {
		signalGroupTerm(cmd)
		select {
		case <-exited:
		case <-time.After(stopGrace):
			signalGroupKill(cmd)
			<-exited
		}
	}
	if doneCh != nil {
		<-doneCh
	}
	return b.saveOverlay()
}
