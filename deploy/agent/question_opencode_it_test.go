package main

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	"pystino-agent/internal/backend"
	backendopencode "pystino-agent/internal/backend/opencode"
	"pystino-agent/internal/policy"
	"pystino-agent/internal/sessions"
)

// TestOpencodeQuestionIntegration spawns a real, pinned opencode (1.18.31)
// against a mock OpenAI-compatible upstream and proves the whole
// user-question tool design end to end: the model's own call to opencode's
// built-in "question" tool becomes question.asked on the normalized stream,
// answering it through the Asker capability resolves it as question.resolved
// with the chosen labels, the tool call itself completes, and the turn
// resumes with the model's own follow-up text. Gated behind
// PYSTINO_AGENT_OPENCODE_IT=1, same as opencode_it_test.go.
func TestOpencodeQuestionIntegration(t *testing.T) {
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

	asker, ok := backend.Backend(ocBackend).(backend.Asker)
	if !ok {
		t.Fatal("opencode backend does not implement backend.Asker")
	}

	mat := sessions.New(ocBackend, policy.Default())
	if err := mat.Start(ctx); err != nil {
		t.Fatalf("subscribing to opencode: %v", err)
	}

	sess, err := ocBackend.CreateSession(ctx, workDir, backend.CreateSessionOptions{Title: "it"})
	if err != nil {
		t.Fatalf("creating session: %v", err)
	}
	mat.Track(workDir, sess)

	setMockScenario(t, mockOrigin, map[string]any{
		"toolCalls": []map[string]any{{
			"id":   "call_q1",
			"name": "question",
			"arguments": `{"questions":[{"question":"Which approach?","header":"Approach",` +
				`"options":[{"label":"A","description":"Do A"},{"label":"B","description":"Do B"}],"multiple":false}]}`,
		}},
		"content":      []string{"Thanks for answering."},
		"finishReason": "stop",
	})
	if err := ocBackend.Prompt(ctx, workDir, sess.ID, backend.Prompt{Text: "ask me something"}); err != nil {
		t.Fatalf("prompt: %v", err)
	}

	var requestID string
	drainEvents(t, mat, sess.ID, 30*time.Second, func(ev backend.Event) bool {
		if ev.Kind == backend.EventQuestionAsked {
			requestID = ev.QuestionRequestID
			if len(ev.Questions) != 1 || ev.Questions[0].Question != "Which approach?" {
				t.Fatalf("Questions = %+v", ev.Questions)
			}
			return true
		}
		return false
	})
	if requestID == "" {
		t.Fatal("never saw question.asked")
	}

	if err := asker.ReplyQuestion(ctx, workDir, sess.ID, requestID, [][]string{{"A"}}); err != nil {
		t.Fatalf("ReplyQuestion: %v", err)
	}

	var sawResolved, sawCompletedTool bool
	events := drainEvents(t, mat, sess.ID, 30*time.Second, isIdle)
	for _, ev := range events {
		if ev.Kind == backend.EventQuestionResolved && ev.QuestionRequestID == requestID {
			if ev.QuestionDecision != "answered" || len(ev.QuestionAnswers) != 1 || ev.QuestionAnswers[0][0] != "A" {
				t.Fatalf("resolved event = %+v", ev)
			}
			sawResolved = true
		}
		if ev.Kind == backend.EventPart && ev.Part != nil && ev.Part.Type == backend.PartTool &&
			ev.Part.Tool == "question" && ev.Part.ToolStatus == backend.ToolCompleted {
			sawCompletedTool = true
		}
	}
	if !sawResolved {
		t.Error("never saw question.resolved for this request")
	}
	if !sawCompletedTool {
		t.Error("the question tool call never reached status completed")
	}
}
