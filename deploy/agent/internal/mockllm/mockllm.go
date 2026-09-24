// Package mockllm is a minimal OpenAI-compatible upstream for the agent's
// integration tests: GET /v1/models and streaming POST /v1/chat/completions,
// scripted through the same /__control plane as the chat app's mock
// (POST /__control/scenario {"scenario": {...}}).
//
// It lives here, in Go and in-process, so the real-opencode tests need no
// Node and no second repository: they run the same on a laptop, on this box
// and in a scheduled CI job against opencode's latest release.
package mockllm

import (
	"encoding/json"
	"fmt"
	"net/http"
	"sync"
	"time"
)

// ToolCall is one call the upstream asks the client to make.
type ToolCall struct {
	ID        string `json:"id"`
	Name      string `json:"name"`
	Arguments string `json:"arguments"`
}

// Scenario scripts every following completion until the next one is set.
type Scenario struct {
	Content      []string   `json:"content"`
	ChunkDelayMs int        `json:"chunkDelayMs"`
	ToolCalls    []ToolCall `json:"toolCalls"`
	FinishReason string     `json:"finishReason"`
}

var defaultScenario = Scenario{Content: []string{"Hello", " from", " the", " mock", " server", "."}, ChunkDelayMs: 10, FinishReason: "stop"}

// namedScenarios mirrors the chat app's SCENARIOS names the tests use.
var namedScenarios = map[string]Scenario{"plainText": defaultScenario}

// Server is an http.Handler; its zero value is not usable, use New.
type Server struct {
	mu       sync.Mutex
	scenario Scenario
}

func New() *Server { return &Server{scenario: defaultScenario} }

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	switch {
	case r.URL.Path == "/__control/health":
		writeJSON(w, map[string]bool{"ok": true})
	case r.URL.Path == "/__control/scenario" && r.Method == http.MethodPost:
		var body struct {
			Scenario json.RawMessage `json:"scenario"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		// A scenario is either an inline script or a name, as in the chat app's mock.
		var sc Scenario
		var name string
		if json.Unmarshal(body.Scenario, &name) == nil {
			named, ok := namedScenarios[name]
			if !ok {
				http.Error(w, "unknown scenario "+name, http.StatusBadRequest)
				return
			}
			sc = named
		} else if err := json.Unmarshal(body.Scenario, &sc); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		s.mu.Lock()
		s.scenario = sc
		s.mu.Unlock()
		writeJSON(w, map[string]bool{"ok": true})
	case r.URL.Path == "/v1/models":
		writeJSON(w, map[string]any{"object": "list", "data": []map[string]any{{"id": "mock-model", "object": "model"}}})
	case r.URL.Path == "/v1/chat/completions" && r.Method == http.MethodPost:
		s.complete(w, r)
	default:
		http.NotFound(w, r)
	}
}

func (s *Server) complete(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Model    string           `json:"model"`
		Stream   bool             `json:"stream"`
		Messages []map[string]any `json:"messages"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	s.mu.Lock()
	sc := s.scenario
	s.mu.Unlock()
	// Once a tool result is in the history the call has happened: answer with
	// text, or the client loops forever.
	toolResultSeen := false
	for _, m := range req.Messages {
		if m["role"] == "tool" {
			toolResultSeen = true
		}
	}
	calls := sc.ToolCalls
	if toolResultSeen {
		calls = nil
	}
	finish := sc.FinishReason
	if len(calls) > 0 {
		finish = "tool_calls"
	} else if finish == "" {
		finish = "stop"
	}

	if !req.Stream {
		msg := map[string]any{"role": "assistant", "content": join(sc.Content)}
		if len(calls) > 0 {
			msg["tool_calls"] = wireCalls(calls)
		}
		writeJSON(w, map[string]any{"id": "chatcmpl-mock", "object": "chat.completion", "model": req.Model,
			"choices": []map[string]any{{"index": 0, "message": msg, "finish_reason": finish}}})
		return
	}

	w.Header().Set("Content-Type", "text/event-stream")
	flusher, _ := w.(http.Flusher)
	send := func(delta map[string]any, finishReason any) {
		chunk := map[string]any{"id": "chatcmpl-mock", "object": "chat.completion.chunk", "created": 0, "model": req.Model,
			"choices": []map[string]any{{"index": 0, "delta": delta, "finish_reason": finishReason}}}
		body, _ := json.Marshal(chunk)
		fmt.Fprintf(w, "data: %s\n\n", body)
		if flusher != nil {
			flusher.Flush()
		}
	}
	send(map[string]any{"role": "assistant"}, nil)
	if len(calls) > 0 {
		send(map[string]any{"tool_calls": wireCalls(calls)}, nil)
	} else {
		for _, token := range sc.Content {
			select {
			case <-r.Context().Done():
				return // the client cancelled (a Stop): end the stream like a real upstream
			case <-time.After(time.Duration(sc.ChunkDelayMs) * time.Millisecond):
			}
			send(map[string]any{"content": token}, nil)
		}
	}
	send(map[string]any{}, finish)
	fmt.Fprint(w, "data: [DONE]\n\n")
	if flusher != nil {
		flusher.Flush()
	}
}

func wireCalls(calls []ToolCall) []map[string]any {
	out := make([]map[string]any, len(calls))
	for i, c := range calls {
		out[i] = map[string]any{"index": i, "id": c.ID, "type": "function",
			"function": map[string]any{"name": c.Name, "arguments": c.Arguments}}
	}
	return out
}

func join(parts []string) string {
	s := ""
	for _, p := range parts {
		s += p
	}
	return s
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}
