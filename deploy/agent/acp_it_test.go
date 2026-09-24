package main

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"pystino-agent/internal/backend"
	backendacp "pystino-agent/internal/backend/acp"
	"pystino-agent/internal/policy"
	"pystino-agent/internal/sessions"
)

// TestACPIntegration proves the generic ACP adapter (internal/backend/acp)
// against a real `opencode acp` (opencode 1.18.31 on PATH) the same way
// opencode_it_test.go proves the opencode backend against `opencode
// serve`: a prompt streams text to idle, and a tool call's permission ask,
// replied "once", lets the tool run. It is the load-bearing evidence for
// PROTOCOL.md §2's claim that the backend interface is generic — the same
// mock LLM and the same opencode binary, spoken to over a different wire
// protocol, driving the identical internal/backend.Backend contract.
//
// Gated behind PYSTINO_AGENT_ACP_IT=1 (needs opencode and node on PATH,
// plus the sibling thin-cerea checkout for the mock's script — see
// startMockLLM in opencode_it_test.go, the in-process internal/mockllm).
func TestACPIntegration(t *testing.T) {
	if os.Getenv("PYSTINO_AGENT_ACP_IT") != "1" {
		t.Skip("set PYSTINO_AGENT_ACP_IT=1 to run (spawns real `opencode acp` + a mock LLM)")
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
		"OPENCODE_CONFIG=" + configPath,
		"PATH=" + os.Getenv("PATH"),
	}

	acpBackend := backendacp.New(backendacp.Config{
		Command:        []string{"opencode", "acp"},
		Env:            isolatedEnv,
		StartupTimeout: 90 * time.Second,
		Logf:           t.Logf,
	})
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()
	if err := acpBackend.Start(ctx); err != nil {
		t.Fatalf("starting acp backend: %v", err)
	}
	t.Cleanup(func() {
		if err := acpBackend.Stop(); err != nil {
			t.Logf("stopping acp backend: %v", err)
		}
	})

	if id := acpBackend.ID(); id != "acp:OpenCode" {
		t.Errorf("ID() = %q, want %q", id, "acp:OpenCode")
	}

	mat := sessions.New(acpBackend, policy.Default())
	if err := mat.Start(ctx); err != nil {
		t.Fatalf("subscribing to acp backend: %v", err)
	}

	sess, err := acpBackend.CreateSession(ctx, workDir, backend.CreateSessionOptions{Title: "it"})
	if err != nil {
		t.Fatalf("creating session: %v", err)
	}
	mat.Track(workDir, sess)

	t.Run("prompt streams text to idle", func(t *testing.T) {
		setMockScenario(t, mockOrigin, "plainText")
		if err := acpBackend.Prompt(ctx, workDir, sess.ID, backend.Prompt{Text: "say hello"}); err != nil {
			t.Fatalf("prompt: %v", err)
		}
		events := drainEvents(t, mat, sess.ID, 30*time.Second, isIdle)

		var sawPart bool
		var assembled string
		for _, ev := range events {
			switch ev.Kind {
			case backend.EventPart:
				if ev.Part != nil && ev.Part.Type == backend.PartText && ev.Part.Role == "assistant" {
					sawPart = true
					assembled = ev.Part.Text
				}
			case backend.EventDelta:
				assembled += ev.Delta
			}
		}
		if !sawPart {
			t.Error("never saw an assistant text part upsert")
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
		if err := acpBackend.Prompt(ctx, workDir, sess.ID, backend.Prompt{Text: "run the command"}); err != nil {
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

		if err := acpBackend.ReplyPermission(ctx, workDir, sess.ID, requestID, backend.DecisionOnce, ""); err != nil {
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
}

// itFreePort, startMockLLM, setMockScenario, drainEvents and isIdle are
// shared with opencode_it_test.go (same package, same file's helpers) —
// this test intentionally drives the identical mock LLM setup through a
// different backend to prove the interface is generic, not to duplicate
// test infrastructure.
