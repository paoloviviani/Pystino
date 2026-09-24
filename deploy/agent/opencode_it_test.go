package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"pystino-agent/internal/backend"
	backendopencode "pystino-agent/internal/backend/opencode"
	"pystino-agent/internal/policy"
	"pystino-agent/internal/sessions"
)

// mockOpenAIScript is the mock upstream this test drives via its
// /__control/* plane (see the file for the exact shapes). It lives in a
// sibling worktree, not this one — it's Cerea's test fixture, reused here
// rather than duplicated.
const mockOpenAIScript = "/home/ubuntu/.paseo/worktrees/thin-cerea/tests/mock-openai.ts"

// itFreePort picks a free loopback port without holding the listener open
// — the same small race every test in this codebase that needs to hand a
// port to a child process accepts.
func itFreePort(t *testing.T) int {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port
}

// startMockLLM launches the mock OpenAI-compatible server and waits for its
// control-plane health check. Registers cleanup that kills it by PID —
// never leave it running past this test.
func startMockLLM(t *testing.T, port int) string {
	t.Helper()
	if _, err := os.Stat(mockOpenAIScript); err != nil {
		t.Skipf("mock LLM script not found at %s: %v", mockOpenAIScript, err)
	}
	cmd := exec.Command("node", mockOpenAIScript)
	cmd.Env = append(os.Environ(), fmt.Sprintf("MOCK_OPENAI_PORT=%d", port))
	var stderr strings.Builder
	cmd.Stderr = &stderr
	// Belt and suspenders alongside the Kill in t.Cleanup below: if this
	// test binary itself dies without running cleanup (e.g. `go test
	// -timeout` firing), the kernel still reaps this child rather than
	// leaving a mock LLM listening forever.
	cmd.SysProcAttr = &syscall.SysProcAttr{Pdeathsig: syscall.SIGKILL}
	if err := cmd.Start(); err != nil {
		t.Fatalf("starting mock LLM: %v", err)
	}
	t.Cleanup(func() {
		if cmd.Process != nil {
			_ = cmd.Process.Kill()
			_, _ = cmd.Process.Wait()
		}
	})

	origin := fmt.Sprintf("http://127.0.0.1:%d", port)
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		resp, err := http.Get(origin + "/__control/health")
		if err == nil {
			resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return origin
			}
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatalf("mock LLM never became healthy (stderr: %s)", stderr.String())
	return ""
}

func setMockScenario(t *testing.T, origin string, scenario any) {
	t.Helper()
	body, err := json.Marshal(map[string]any{"scenario": scenario})
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.Post(origin+"/__control/scenario", "application/json", strings.NewReader(string(body)))
	if err != nil {
		t.Fatalf("setting mock scenario: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("setting mock scenario: status %d", resp.StatusCode)
	}
}

// drainEvents collects live events for sessionID until stop returns true
// for one of them, or timeout elapses.
func drainEvents(t *testing.T, mat *sessions.Materializer, sessionID string, timeout time.Duration, stop func(backend.Event) bool) []backend.Event {
	t.Helper()
	var out []backend.Event
	deadline := time.After(timeout)
	for {
		select {
		case env := <-mat.Events():
			if env.SessionID != sessionID {
				continue
			}
			out = append(out, env.Event)
			if stop(env.Event) {
				return out
			}
		case <-deadline:
			t.Fatalf("timed out after %s waiting for the expected event; saw %d events: %+v", timeout, len(out), out)
		}
	}
}

func isIdle(ev backend.Event) bool {
	return ev.Kind == backend.EventStatus && ev.Status == backend.StatusIdle
}

// TestOpencodeIntegration spawns a real, pinned opencode (1.18.31) against
// a mock OpenAI-compatible upstream and proves the three things PROTOCOL.md
// promises the opencode backend delivers: a prompt streams text deltas to
// idle; a tool call surfaces a permission ask that, once replied "once",
// lets the tool run; and a cancel mid-stream reaches idle well before the
// scripted stream would finish on its own. Gated behind
// PYSTINO_AGENT_OPENCODE_IT=1 — it needs the opencode and node binaries,
// and a sibling thin-cerea checkout for the mock's script.
func TestOpencodeIntegration(t *testing.T) {
	if os.Getenv("PYSTINO_AGENT_OPENCODE_IT") != "1" {
		t.Skip("set PYSTINO_AGENT_OPENCODE_IT=1 to run (spawns real opencode + a mock LLM)")
	}
	if _, err := exec.LookPath("opencode"); err != nil {
		t.Skipf("opencode not on PATH: %v", err)
	}
	if _, err := exec.LookPath("node"); err != nil {
		t.Skipf("node not on PATH: %v", err)
	}

	mockPort := itFreePort(t)
	mockOrigin := startMockLLM(t, mockPort)

	root := t.TempDir()
	homeDir := filepath.Join(root, "home")
	configDir := filepath.Join(root, "config")
	dataDir := filepath.Join(root, "data")
	cacheDir := filepath.Join(root, "cache")
	workDir := filepath.Join(root, "workdir")
	for _, d := range []string{homeDir, configDir, dataDir, cacheDir, workDir} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}

	opencodeConfig := map[string]any{
		"$schema": "https://opencode.ai/config.json",
		"provider": map[string]any{
			"pystino": map[string]any{
				"npm":  "@ai-sdk/openai-compatible",
				"name": "Pystino Mock",
				"options": map[string]any{
					"baseURL": mockOrigin + "/v1",
					"apiKey":  "test-secret",
				},
				"models": map[string]any{
					"mock-model": map[string]any{
						"name":  "Mock Model",
						"limit": map[string]any{"context": 100000, "output": 8000},
					},
				},
			},
		},
		"enabled_providers": []string{"pystino"},
		"permission":        map[string]any{"bash": "ask"},
	}
	configBody, err := json.MarshalIndent(opencodeConfig, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(root, "opencode.json")
	if err := os.WriteFile(configPath, configBody, 0o644); err != nil {
		t.Fatal(err)
	}

	isolatedEnv := []string{
		"HOME=" + homeDir,
		"XDG_CONFIG_HOME=" + configDir,
		"XDG_DATA_HOME=" + dataDir,
		"XDG_CACHE_HOME=" + cacheDir,
		"PATH=" + os.Getenv("PATH"),
	}

	ocBackend := backendopencode.New(backendopencode.Config{
		ConfigPath:     configPath,
		Env:            isolatedEnv,
		StartupTimeout: 90 * time.Second,
		Logf:           t.Logf,
	})
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()
	if err := ocBackend.Start(ctx); err != nil {
		t.Fatalf("starting opencode: %v", err)
	}
	t.Cleanup(func() {
		if err := ocBackend.Stop(); err != nil {
			t.Logf("stopping opencode: %v", err)
		}
	})

	mat := sessions.New(ocBackend, policy.Default())
	if err := mat.Start(ctx); err != nil {
		t.Fatalf("subscribing to opencode: %v", err)
	}

	sess, err := ocBackend.CreateSession(ctx, workDir, backend.CreateSessionOptions{Title: "it"})
	if err != nil {
		t.Fatalf("creating session: %v", err)
	}
	mat.Track(workDir, sess)

	t.Run("prompt streams text deltas to idle", func(t *testing.T) {
		setMockScenario(t, mockOrigin, "plainText")
		if err := ocBackend.Prompt(ctx, workDir, sess.ID, backend.Prompt{Text: "say hello"}); err != nil {
			t.Fatalf("prompt: %v", err)
		}
		events := drainEvents(t, mat, sess.ID, 30*time.Second, isIdle)

		var sawPart, sawDelta bool
		var assembled string
		for _, ev := range events {
			switch ev.Kind {
			case backend.EventPart:
				if ev.Part != nil && ev.Part.Type == backend.PartText {
					sawPart = true
					assembled = ev.Part.Text
				}
			case backend.EventDelta:
				sawDelta = true
				assembled += ev.Delta
			}
		}
		if !sawPart {
			t.Error("never saw a text part upsert")
		}
		if !sawDelta {
			t.Error("never saw a text delta (opencode may have sent the full text in one part; check the text contract still held)")
		}
		if !strings.Contains(assembled, "Hello") {
			t.Errorf("assembled text = %q, want it to contain the mock's content", assembled)
		}
	})

	t.Run("tool call asks permission, reply once runs it", func(t *testing.T) {
		outFile := filepath.Join(workDir, "out.txt")
		_ = os.Remove(outFile)
		setMockScenario(t, mockOrigin, map[string]any{
			"toolCalls": []map[string]any{{
				"id": "call_1", "name": "bash",
				"arguments": `{"command":"echo hi > out.txt","description":"w"}`,
			}},
			"content":      []string{"Done"},
			"finishReason": "stop",
		})
		if err := ocBackend.Prompt(ctx, workDir, sess.ID, backend.Prompt{Text: "run the command"}); err != nil {
			t.Fatalf("prompt: %v", err)
		}

		var requestID string
		drainEvents(t, mat, sess.ID, 30*time.Second, func(ev backend.Event) bool {
			if ev.Kind == backend.EventPermissionAsked && ev.Request != nil {
				requestID = ev.Request.ID
				return true
			}
			return false
		})
		if requestID == "" {
			t.Fatal("never saw permission.asked for the bash tool call")
		}

		if err := ocBackend.ReplyPermission(ctx, workDir, sess.ID, requestID, backend.DecisionOnce, ""); err != nil {
			t.Fatalf("replying to permission: %v", err)
		}

		events := drainEvents(t, mat, sess.ID, 30*time.Second, isIdle)
		var sawCompletedTool bool
		for _, ev := range events {
			if ev.Kind == backend.EventPart && ev.Part != nil && ev.Part.Type == backend.PartTool && ev.Part.ToolStatus == backend.ToolCompleted {
				sawCompletedTool = true
			}
		}
		if !sawCompletedTool {
			t.Error("never saw the tool part reach status completed")
		}
		body, err := os.ReadFile(outFile)
		if err != nil {
			t.Fatalf("the tool never actually ran: reading %s: %v", outFile, err)
		}
		if strings.TrimSpace(string(body)) != "hi" {
			t.Errorf("out.txt = %q, want %q", body, "hi\n")
		}
	})

	t.Run("cancel mid-stream reaches idle early", func(t *testing.T) {
		tokens := make([]string, 60)
		for i := range tokens {
			tokens[i] = fmt.Sprintf("token-%d ", i)
		}
		setMockScenario(t, mockOrigin, map[string]any{
			"content": tokens, "chunkDelayMs": 200, "finishReason": "stop",
		})
		if err := ocBackend.Prompt(ctx, workDir, sess.ID, backend.Prompt{Text: "go slow"}); err != nil {
			t.Fatalf("prompt: %v", err)
		}

		// Let a little content arrive so there is a live stream to cancel,
		// then cancel it. The full scripted stream would take 60*200ms=12s;
		// idle must arrive well before that.
		drainEvents(t, mat, sess.ID, 10*time.Second, func(ev backend.Event) bool {
			return ev.Kind == backend.EventDelta || ev.Kind == backend.EventPart
		})
		start := time.Now()
		if err := ocBackend.Cancel(ctx, workDir, sess.ID); err != nil {
			t.Fatalf("cancel: %v", err)
		}
		drainEvents(t, mat, sess.ID, 10*time.Second, isIdle)
		if elapsed := time.Since(start); elapsed > 8*time.Second {
			t.Errorf("idle took %s after cancel; expected well under the scripted stream's 12s", elapsed)
		}
	})
}
