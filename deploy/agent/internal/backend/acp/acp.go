// Package acp implements internal/backend.Backend over the Agent Client
// Protocol (JSON-RPC 2.0, newline-delimited over a child process's stdio).
// It is the generic *second* backend PROTOCOL.md §2 calls for: any ACP
// agent can be plugged in behind it (opencode's own `opencode acp`, Gemini
// CLI, Claude Code via claude-code-acp, Pi via pi-acp), reporting whichever
// subset of capabilities that agent actually has. It necessarily reports
// fewer capabilities than internal/backend/opencode (no diff, children,
// usage or compact — see Capabilities) because ACP itself has no wire
// message for any of those (PROTOCOL.md §2's comparison).
//
// Wire shapes below are verified live against `opencode acp` (opencode
// 1.18.31 on PATH; see acp_it_test.go) except where noted as following the
// published ACP schema instead — opencode's own ACP mode does not (yet)
// match the schema everywhere (session/new answers with a bespoke
// configOptions list rather than modes/models, found live), so mapping.go
// reads both shapes defensively, the same discipline
// internal/backend/opencode's mapping.go documents.
package acp

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"sync"
	"time"

	"pystino-agent/internal/backend"
)

// Config is everything needed to spawn and speak ACP to one agent process.
type Config struct {
	// Command is argv for the agent, e.g. []string{"opencode", "acp"}.
	Command []string
	// Env, if non-nil, replaces the child's environment outright (nil
	// means inherit os.Environ(), matching internal/backend/opencode's
	// Config.Env).
	Env []string
	// StartupTimeout bounds Start's wait for the initialize handshake to
	// complete (default 30s).
	StartupTimeout time.Duration
	// Logf receives lifecycle-only messages: started, ready, restarting,
	// exited (R6 — never frame or response bodies, which may carry a
	// user's prompt text).
	Logf func(format string, args ...any)
}

const (
	defaultStartupTimeout = 30 * time.Second
	restartMinDelay       = time.Second
	restartMaxDelay       = 30 * time.Second
	stopGrace             = 3 * time.Second
	rpcCallTimeout        = 20 * time.Second
)

// acpAgentCapabilities is initialize's agentCapabilities, read defensively
// (sessionCapabilities' entries are small objects in practice, not bools —
// found live; present() in mapping.go treats "key exists, not false" as
// advertised).
type acpAgentCapabilities struct {
	loadSession   bool
	promptImage   bool
	sessionList   bool
	sessionClose  bool
	sessionResume bool
}

func parseAgentCapabilities(raw map[string]any) acpAgentCapabilities {
	prompt := getMap(raw, "promptCapabilities")
	sess := getMap(raw, "sessionCapabilities")
	return acpAgentCapabilities{
		loadSession:   getBool(raw, "loadSession"),
		promptImage:   getBool(prompt, "image"),
		sessionList:   present(sess, "list"),
		sessionClose:  present(sess, "close"),
		sessionResume: present(sess, "resume"),
	}
}

// Backend is internal/backend.Backend over one supervised ACP agent
// process.
type Backend struct {
	cfg Config

	mu      sync.Mutex
	cmd     *exec.Cmd
	conn    *rpcConn
	exited  chan struct{}
	stopped bool

	stopCh chan struct{}
	doneCh chan struct{}

	readyOnce sync.Once
	ready     chan struct{}

	infoMu      sync.Mutex
	agentName   string
	agentVer    string
	agentCaps   acpAgentCapabilities
	initialized bool

	reg *registry

	events   chan backend.BackendEvent
	eventsMu sync.Mutex
}

// New builds a Backend. Start must be called before any other method.
func New(cfg Config) *Backend {
	if cfg.Logf == nil {
		cfg.Logf = func(string, ...any) {}
	}
	return &Backend{
		cfg:   cfg,
		reg:   newRegistry(),
		ready: make(chan struct{}),
	}
}

func (b *Backend) ID() string {
	b.infoMu.Lock()
	defer b.infoMu.Unlock()
	if b.agentName == "" {
		return "acp"
	}
	return "acp:" + b.agentName
}

func (b *Backend) Version() string {
	b.infoMu.Lock()
	defer b.infoMu.Unlock()
	return b.agentVer
}

func (b *Backend) Capabilities() backend.Capabilities {
	b.infoMu.Lock()
	images := b.agentCaps.promptImage
	b.infoMu.Unlock()
	// Diff/Children/Usage/Compact/Worktrees: ACP has no wire message for
	// any of these (PROTOCOL.md §2). AutoAccept is true regardless of the
	// underlying agent: the materializer (internal/sessions) implements
	// auto-accept generically by answering session/request_permission
	// itself, not something a backend opts into.
	return backend.Capabilities{
		Diff: false, Children: false, Usage: false, Compact: false,
		Images: images, Files: false, Worktrees: false, AutoAccept: true,
	}
}

// Start launches the supervise loop and waits for the first successful
// initialize handshake.
func (b *Backend) Start(ctx context.Context) error {
	if len(b.cfg.Command) == 0 {
		return fmt.Errorf("acp: empty Command")
	}
	b.stopCh = make(chan struct{})
	b.doneCh = make(chan struct{})
	go b.superviseLoop(ctx)

	timeout := b.cfg.StartupTimeout
	if timeout == 0 {
		timeout = defaultStartupTimeout
	}
	select {
	case <-b.ready:
		b.cfg.Logf("acp: ready (%s %s)", b.ID(), b.Version())
		return nil
	case <-time.After(timeout):
		return fmt.Errorf("acp: %s did not become ready within %s", strings.Join(b.cfg.Command, " "), timeout)
	case <-ctx.Done():
		return ctx.Err()
	}
}

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
			b.cfg.Logf("acp: agent exited (%v), restarting in %s", err, delay)
		} else {
			b.cfg.Logf("acp: agent exited before becoming ready (%v), retrying in %s", err, delay)
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

// runOnce spawns the agent, wires its stdio to an rpcConn, handshakes, and
// blocks until the connection ends (stdout closes) or the process exits.
func (b *Backend) runOnce(ctx context.Context) error {
	cmd := exec.Command(b.cfg.Command[0], b.cfg.Command[1:]...)
	env := b.cfg.Env
	if env == nil {
		env = os.Environ()
	}
	cmd.Env = env
	setProcAttrs(cmd)

	stdin, err := cmd.StdinPipe()
	if err != nil {
		return fmt.Errorf("acp: stdin pipe: %w", err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return fmt.Errorf("acp: stdout pipe: %w", err)
	}
	stderrTail := newTailBuffer(4096)
	cmd.Stderr = stderrTail

	if err := cmd.Start(); err != nil {
		return fmt.Errorf("acp: starting %s: %w", strings.Join(b.cfg.Command, " "), err)
	}
	exited := make(chan struct{})
	b.mu.Lock()
	b.cmd = cmd
	b.exited = exited
	b.mu.Unlock()

	connErr := b.serveConn(ctx, stdout, stdin)
	if connErr != nil && cmd.Process != nil {
		// serveConn returning an error other than "the connection ended"
		// (the common case, conn.err from readLoop hitting EOF because the
		// process exited on its own) means the handshake itself failed —
		// e.g. a bad initialize response — while the process is still
		// alive. Without this, cmd.Wait() below would block forever on a
		// process that has no reason to exit on its own.
		signalGroupKill(cmd)
	}

	waitErr := cmd.Wait()
	close(exited)
	b.mu.Lock()
	b.cmd = nil
	b.mu.Unlock()

	if connErr != nil {
		return connErr
	}
	if waitErr != nil {
		if tail := stderrTail.String(); tail != "" {
			return fmt.Errorf("%w (stderr: %s)", waitErr, tail)
		}
		return waitErr
	}
	return fmt.Errorf("acp: agent exited")
}

// serveConn wires stdout/stdin into a fresh rpcConn, handshakes, publishes
// it as the Backend's current connection, and blocks until the read loop
// ends. Split out from runOnce so unit tests can drive it directly over an
// in-process pipe instead of a real subprocess (see acp_test.go's fake
// agent).
func (b *Backend) serveConn(ctx context.Context, stdout io.Reader, stdin io.WriteCloser) error {
	conn := newRPCConn(stdin, b.handleRequest, b.handleNotify, b.cfg.Logf)
	go conn.readLoop(stdout)

	if err := b.handshake(ctx, conn); err != nil {
		_ = stdin.Close()
		return err
	}

	b.mu.Lock()
	b.conn = conn
	b.mu.Unlock()

	b.readyOnce.Do(func() { close(b.ready) })

	<-conn.done

	b.mu.Lock()
	if b.conn == conn {
		b.conn = nil
	}
	b.mu.Unlock()
	return conn.err
}

func (b *Backend) handshake(ctx context.Context, conn *rpcConn) error {
	ctx, cancel := context.WithTimeout(ctx, rpcCallTimeout)
	defer cancel()
	raw, err := conn.call(ctx, "initialize", map[string]any{
		"protocolVersion": 1,
		"clientCapabilities": map[string]any{
			"fs":       map[string]any{"readTextFile": false, "writeTextFile": false},
			"terminal": false,
		},
	})
	if err != nil {
		return fmt.Errorf("acp: initialize: %w", err)
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		return fmt.Errorf("acp: initialize: parsing response: %w", err)
	}
	caps := parseAgentCapabilities(getMap(m, "agentCapabilities"))
	info := getMap(m, "agentInfo")

	b.infoMu.Lock()
	b.agentName = getStr(info, "name")
	b.agentVer = getStr(info, "version")
	b.agentCaps = caps
	b.initialized = true
	b.infoMu.Unlock()
	return nil
}

// currentConn returns the live connection, or an error if the agent is
// between (re)connects — a caller (an op dispatched from Cerea) sees this
// as a plain "backend" error rather than hanging.
func (b *Backend) currentConn() (*rpcConn, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.conn == nil {
		return nil, fmt.Errorf("acp: agent not connected")
	}
	return b.conn, nil
}

// Stop asks the supervise loop to stop restarting, then SIGTERMs the
// current process (group), escalating to SIGKILL after stopGrace —
// mirrors internal/backend/opencode.Backend.Stop exactly (same shutdown
// contract, same reasoning).
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
	return nil
}
