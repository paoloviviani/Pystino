package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"

	"pystino-agent/internal/backend"
	backendacp "pystino-agent/internal/backend/acp"
	backendopencode "pystino-agent/internal/backend/opencode"
	"pystino-agent/internal/fsutil"
	"pystino-agent/internal/link"
	"pystino-agent/internal/policy"
	"pystino-agent/internal/sessions"
	"pystino-agent/internal/workspaces"
)

// agentVersion is pystino-agent's own version. Bumped by hand until a
// release process assigns it from a tag.
const agentVersion = "0.1.0"

const runUsage = `pystino-agent run — supervise opencode and dial out to Cerea.

Usage:
  pystino-agent run [--cerea URL] [options]

  --cerea URL         Cerea origin to dial (default: the origin recorded at
                      enroll time, if any — see 'enroll --cerea').
  --creds PATH        Credential file (default <config-dir>/opencode/
                      pystino-credentials.json).
  --state-dir PATH    Directory for this run's own state: policy.json,
                      the workspace registry, the machine id, the opencode
                      mode/model overlay (default: beside --creds).
  --no-shim           Don't start the local shim; --opencode-config must
                      already point opencode at a reachable provider (used
                      by tests, e.g. a mock LLM).
  --backend NAME      Which backend to run: "opencode" (default) or "acp"
                      (any ACP agent — PROTOCOL.md §2).
  --opencode-bin PATH   opencode binary (default "opencode", resolved on PATH).
  --opencode-config PATH  Exported as OPENCODE_CONFIG for the spawned
                      opencode, overriding its normal config discovery.
  --acp-command CMD   Command line for the ACP agent, only used with
                      --backend acp (default "opencode acp").
  --machine-name NAME   Display name for this machine (default: hostname).
  --port PORT         Override the shim's port (only meaningful without
                      --no-shim; default: the port enroll recorded).
`

type runOptions struct {
	cerea          string
	credsPath      string
	stateDir       string
	noShim         bool
	backendKind    string
	opencodeBin    string
	opencodeConfig string
	acpCommand     string
	machineName    string
	port           int
}

func runRun(args []string) error {
	fs := flagSetWithHelp("run", runUsage)
	opts := runOptions{}
	fs.StringVar(&opts.cerea, "cerea", "", "")
	fs.StringVar(&opts.credsPath, "creds", "", "")
	fs.StringVar(&opts.stateDir, "state-dir", "", "")
	fs.BoolVar(&opts.noShim, "no-shim", false, "")
	fs.StringVar(&opts.backendKind, "backend", "opencode", "")
	fs.StringVar(&opts.opencodeBin, "opencode-bin", "opencode", "")
	fs.StringVar(&opts.opencodeConfig, "opencode-config", "", "")
	fs.StringVar(&opts.acpCommand, "acp-command", "opencode acp", "")
	fs.StringVar(&opts.machineName, "machine-name", "", "")
	fs.IntVar(&opts.port, "port", 0, "")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() > 0 {
		return fmt.Errorf("unexpected arguments: %s", strings.Join(fs.Args(), " "))
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	return runAgent(ctx, &opts)
}

func logf(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "pystino-agent: "+format+"\n", args...)
}

func runAgent(ctx context.Context, opts *runOptions) error {
	credsPath := opts.credsPath
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

	stateDir := opts.stateDir
	if stateDir == "" {
		stateDir = filepath.Dir(credsPath)
	}
	if err := os.MkdirAll(stateDir, 0o700); err != nil {
		return fmt.Errorf("creating state dir: %w", err)
	}

	cereaOrigin := opts.cerea
	if cereaOrigin == "" {
		cereaOrigin = creds.CereaOrigin
	}
	if cereaOrigin == "" {
		return fmt.Errorf("no Cerea origin: pass --cerea, or re-run 'pystino-agent enroll --cerea <origin>'")
	}
	cereaOrigin = strings.TrimSuffix(cereaOrigin, "/")

	pol, err := policy.Load(filepath.Join(stateDir, policyFileName))
	if err != nil {
		return err
	}

	machineID, err := loadOrMintMachineID(filepath.Join(stateDir, "machine-id"))
	if err != nil {
		return err
	}

	machineName := opts.machineName
	if machineName == "" {
		if h, err := os.Hostname(); err == nil {
			machineName = h
		} else {
			machineName = "unknown"
		}
	}

	// The shim is always built: it is the single in-process credential
	// source (R7) both its own HTTP proxy and the link's WSS auth draw
	// from, whether or not the proxy itself is listening.
	shimPort := opts.port
	if shimPort == 0 {
		shimPort = creds.ShimPort
	}
	if shimPort == 0 {
		shimPort = defaultShimPort
	}
	sh := newShim(creds, credsPath, statusPathFor(credsPath), shimPort)
	_ = sh.refresh()
	go sh.refreshLoop()

	var shimServer *http.Server
	if !opts.noShim {
		if creds.Gateway == "" {
			return fmt.Errorf("creds file has no gateway: re-run 'pystino-agent enroll' (the shim has nothing to forward to)")
		}
		mux := http.NewServeMux()
		mux.HandleFunc("/pystino/health", sh.requireLocalAuth(sh.healthHandler))
		mux.HandleFunc("/", sh.requireLocalAuth(sh.handler))
		addr := fmt.Sprintf("127.0.0.1:%d", shimPort)
		shimServer = &http.Server{Addr: addr, Handler: mux, ReadHeaderTimeout: 10 * time.Second}
		go func() {
			if err := shimServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
				logf("shim server error: %v", err)
			}
		}()
		logf("shim listening on http://%s", addr)
	}

	back, err := startBackend(ctx, opts, stateDir, logf)
	if err != nil {
		return err
	}
	logf("%s started (backend %s %s)", opts.backendKind, back.ID(), back.Version())

	mat := sessions.New(back, pol)
	if err := mat.Start(ctx); err != nil {
		return fmt.Errorf("subscribing to %s: %w", back.ID(), err)
	}

	reg, err := workspaces.Load(filepath.Join(stateDir, "workspaces.json"))
	if err != nil {
		return err
	}

	mc := newMachine(reg, back, mat, pol)
	for _, w := range reg.List(true) {
		sessList, err := back.ListSessions(ctx, w.Path)
		if err != nil {
			logf("warning: listing sessions for workspace %q (%s): %v", w.Name, w.Path, err)
			continue
		}
		for _, s := range sessList {
			mc.trackSession(w, s)
		}
	}

	lnk := link.New(link.Config{
		CereaOrigin: cereaOrigin,
		MachineID:   machineID,
		MachineName: machineName,
		Cred:        sh,
		Hello:       func() link.Hello { return buildHello(back, pol) },
		Handler:     mc,
		Logf:        func(format string, args ...any) { logf(format, args...) },
	})
	sh.onExpired = func(message string) {
		_ = lnk.PublishCredentialState("expired", message)
	}

	eventsDone := make(chan struct{})
	go func() {
		defer close(eventsDone)
		forwardEvents(ctx, mat, lnk)
	}()

	linkErr := make(chan error, 1)
	go func() { linkErr <- lnk.Run(ctx) }()

	var runErr error
	select {
	case <-ctx.Done():
		logf("shutting down")
	case err := <-linkErr:
		if err != nil && !errors.Is(err, context.Canceled) {
			logf("link stopped: %v", err)
			runErr = err
		}
	}

	_ = lnk.Close()
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if shimServer != nil {
		_ = shimServer.Shutdown(shutdownCtx)
	}
	if err := back.Stop(); err != nil {
		logf("stopping %s: %v", back.ID(), err)
	}
	<-eventsDone
	return runErr
}

// runningBackend is backend.Backend plus the lifecycle methods every
// concrete backend implementation (opencode, acp) provides — Start/Stop
// live outside the interface because Subscribe's caller (sessions.New)
// only ever needs the former; run.go is the one place that needs both.
type runningBackend interface {
	backend.Backend
	Stop() error
}

// startBackend builds and starts whichever concrete backend --backend
// selects (PROTOCOL.md §2: opencode is the default, richer implementation;
// acp is the generic adapter any ACP agent can be plugged in behind).
func startBackend(ctx context.Context, opts *runOptions, stateDir string, logf func(string, ...any)) (runningBackend, error) {
	switch opts.backendKind {
	case "", "opencode":
		ocBackend := backendopencode.New(backendopencode.Config{
			Bin:         opts.opencodeBin,
			ConfigPath:  opts.opencodeConfig,
			OverlayPath: filepath.Join(stateDir, "opencode-overlay.json"),
			Logf:        func(format string, args ...any) { logf(format, args...) },
		})
		if err := ocBackend.Start(ctx); err != nil {
			return nil, fmt.Errorf("starting opencode: %w", err)
		}
		return ocBackend, nil
	case "acp":
		cmd := strings.Fields(opts.acpCommand)
		if len(cmd) == 0 {
			return nil, fmt.Errorf("--acp-command must not be empty")
		}
		acpBackend := backendacp.New(backendacp.Config{
			Command: cmd,
			Logf:    func(format string, args ...any) { logf(format, args...) },
		})
		if err := acpBackend.Start(ctx); err != nil {
			return nil, fmt.Errorf("starting acp agent %q: %w", opts.acpCommand, err)
		}
		return acpBackend, nil
	default:
		return nil, fmt.Errorf("unknown --backend %q (want \"opencode\" or \"acp\")", opts.backendKind)
	}
}

// forwardEvents drains the materializer's live event stream onto the link,
// for as long as ctx lives. Best effort by design (link.PublishEvent
// itself is best effort — see its doc): a client's session.sync is the
// actual gap-recovery path, not this goroutine keeping up.
func forwardEvents(ctx context.Context, mat *sessions.Materializer, lnk *link.Link) {
	for {
		select {
		case <-ctx.Done():
			return
		case env, ok := <-mat.Events():
			if !ok {
				return
			}
			raw, err := json.Marshal(eventToWire(env.Event))
			if err != nil {
				continue
			}
			_ = lnk.PublishEvent(env.SessionID, env.Epoch, env.Seq, raw)
		}
	}
}

// buildHello assembles the hello frame's payload from live backend and
// policy state (PROTOCOL.md §5).
func buildHello(back backend.Backend, pol policy.Policy) link.Hello {
	caps := back.Capabilities()
	hostname, _ := os.Hostname()
	// A nil slice marshals to null, and Cerea validates the hello strictly:
	// PROTOCOL.md types workspaceRoots as an array, so an unconfigured
	// policy must say [] or the hello is dropped and the link never pairs.
	roots := pol.WorkspaceRoots
	if roots == nil {
		roots = []string{}
	}
	return link.Hello{
		Agent: link.AgentInfo{
			Version:  agentVersion,
			OS:       runtime.GOOS,
			Arch:     runtime.GOARCH,
			Hostname: hostname,
		},
		Backends: []link.BackendInfo{{
			ID:      back.ID(),
			Version: back.Version(),
			Capabilities: map[string]bool{
				"diff": caps.Diff, "children": caps.Children, "usage": caps.Usage,
				"compact": caps.Compact, "images": caps.Images, "files": caps.Files,
				"worktrees": caps.Worktrees, "autoAccept": caps.AutoAccept,
				"questions": caps.Questions,
			},
		}},
		Policy: link.PolicyInfo{
			AutoAccept:      string(pol.AutoAccept),
			WorkspaceRoots:  roots,
			AllowFreeModels: pol.AllowFreeModels,
		},
	}
}

// loadOrMintMachineID reads a persisted machine id, or mints and saves one
// (PROTOCOL.md §3: "generated once, persisted in the agent state dir; a
// re-enroll mints a new one" — here, deleting this file is what stands in
// for that until enroll itself grows a --state-dir to write it into).
func loadOrMintMachineID(path string) (string, error) {
	body, err := fsutil.ReadFileOrEmpty(path)
	if err != nil {
		return "", err
	}
	if body != nil {
		if id := strings.TrimSpace(string(body)); id != "" {
			return id, nil
		}
	}
	id, err := randomHex(16)
	if err != nil {
		return "", fmt.Errorf("minting machine id: %w", err)
	}
	if err := fsutil.WriteFileAtomic(path, []byte(id+"\n"), 0o600); err != nil {
		return "", err
	}
	return id, nil
}
