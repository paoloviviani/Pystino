package opencode

import (
	"testing"

	"pystino-agent/internal/backend"
)

func TestSessionFromMap(t *testing.T) {
	s := sessionFromMap(map[string]any{
		"id": "ses_1", "title": "My Session", "parentID": "ses_parent",
		"time": map[string]any{"created": float64(1000), "updated": float64(2000)},
	})
	if s.ID != "ses_1" || s.Title != "My Session" || s.ParentID != "ses_parent" {
		t.Fatalf("unexpected session: %+v", s)
	}
	if s.CreatedAt.IsZero() || s.UpdatedAt.IsZero() {
		t.Fatalf("timestamps not parsed: %+v", s)
	}
	if s.Backend != "opencode" {
		t.Errorf("Backend = %q, want opencode", s.Backend)
	}
}

func TestPartFromMapText(t *testing.T) {
	p := partFromMap(map[string]any{
		"id": "prt_1", "messageID": "msg_1", "role": "assistant", "type": "text", "text": "hello",
	})
	if p.Type != backend.PartText || p.Text != "hello" || p.MessageID != "msg_1" {
		t.Fatalf("unexpected part: %+v", p)
	}
}

func TestPartFromMapTool(t *testing.T) {
	// state is nested (found live against opencode 1.18.31): status/input
	// /output/title all live under part.state, not the part's top level.
	p := partFromMap(map[string]any{
		"id": "prt_2", "messageID": "msg_1", "type": "tool", "callID": "call_1",
		"tool": "bash",
		"state": map[string]any{
			"status": "completed",
			"input":  map[string]any{"command": "echo hi"},
			"output": "hi\n",
		},
	})
	if p.Type != backend.PartTool || p.CallID != "call_1" || p.Tool != "bash" {
		t.Fatalf("unexpected part: %+v", p)
	}
	if p.ToolStatus != backend.ToolCompleted {
		t.Errorf("ToolStatus = %q, want completed", p.ToolStatus)
	}
	if p.Input["command"] != "echo hi" {
		t.Errorf("Input lost: %+v", p.Input)
	}
}

func TestModeFromAgentMapFiltersUtilityAgents(t *testing.T) {
	cases := []struct {
		name, mode string
		wantOK     bool
	}{
		{"build", "primary", true},
		{"plan", "primary", true},
		{"compaction", "primary", false},
		{"summary", "primary", false},
		{"title", "primary", false},
		{"some-subagent", "subagent", false},
	}
	for _, c := range cases {
		_, ok := modeFromAgentMap(map[string]any{"name": c.name, "mode": c.mode})
		if ok != c.wantOK {
			t.Errorf("modeFromAgentMap(name=%q, mode=%q) ok = %v, want %v", c.name, c.mode, ok, c.wantOK)
		}
	}
}

func TestUsageFromMessageMap(t *testing.T) {
	u := usageFromMessageMap(map[string]any{
		"tokens": map[string]any{
			"input": float64(100), "output": float64(50), "reasoning": float64(10),
			"cache": map[string]any{"read": float64(5), "write": float64(2)},
		},
		"cost": float64(0.0123),
	})
	if u == nil {
		t.Fatal("expected non-nil usage")
	}
	if u.Input != 100 || u.Output != 50 || u.Reasoning != 10 || u.CacheRead != 5 || u.CacheWrite != 2 {
		t.Fatalf("unexpected usage: %+v", u)
	}
	if u.ContextUsed != 167 {
		t.Errorf("ContextUsed = %d, want 167", u.ContextUsed)
	}
	if u.Cost != 0.0123 {
		t.Errorf("Cost = %v", u.Cost)
	}
}

func TestUsageFromMessageMapNoTokens(t *testing.T) {
	if u := usageFromMessageMap(map[string]any{"role": "user"}); u != nil {
		t.Fatalf("expected nil usage for a message with no tokens, got %+v", u)
	}
}

func TestSplitModelID(t *testing.T) {
	providerID, modelID := splitModelID("pystino/coder-large")
	if providerID != "pystino" || modelID != "coder-large" {
		t.Fatalf("got %q / %q", providerID, modelID)
	}
	providerID, modelID = splitModelID("no-slash")
	if providerID != "" || modelID != "no-slash" {
		t.Fatalf("malformed id: got %q / %q", providerID, modelID)
	}
}

func TestCapabilitiesMatchesProtocolSpec(t *testing.T) {
	b := New(Config{})
	c := b.Capabilities()
	want := backend.Capabilities{Diff: true, Children: true, Usage: true, Compact: true, Images: true, Files: true, Worktrees: false, AutoAccept: true}
	if c != want {
		t.Fatalf("Capabilities = %+v, want %+v", c, want)
	}
	if b.ID() != "opencode" {
		t.Errorf("ID() = %q", b.ID())
	}
}

func TestOverlayRoundTrip(t *testing.T) {
	dir := t.TempDir()
	b := New(Config{OverlayPath: dir + "/overlay.json"})
	if err := b.loadOverlay(); err != nil {
		t.Fatal(err)
	}
	if err := b.setOverlay("ses_1", sessionOverlay{ModeID: "build", ModelID: "pystino/coder-large"}); err != nil {
		t.Fatal(err)
	}

	b2 := New(Config{OverlayPath: dir + "/overlay.json"})
	if err := b2.loadOverlay(); err != nil {
		t.Fatal(err)
	}
	ov := b2.getOverlay("ses_1")
	if ov.ModeID != "build" || ov.ModelID != "pystino/coder-large" {
		t.Fatalf("overlay did not survive reload: %+v", ov)
	}
}

// TestResolveClientMessageIDClaimsNextUserMessage pins the fallback mapping
// (PROTOCOL.md §7): a prompt's clientMessageId is attached to the next new
// user message seen for that session, and the mapping then persists so a
// later lookup (a restart, a re-seeded transcript) still finds it without
// needing a pending claim again.
func TestResolveClientMessageIDClaimsNextUserMessage(t *testing.T) {
	dir := t.TempDir()
	b := New(Config{OverlayPath: dir + "/overlay.json"})
	if err := b.loadOverlay(); err != nil {
		t.Fatal(err)
	}
	b.claimPendingClientMessageID("ses_1", "client-msg-abc")

	msg := backend.Message{ID: "msg_new", Role: "user"}
	b.resolveClientMessageID("ses_1", &msg)
	if msg.ClientMessageID != "client-msg-abc" {
		t.Fatalf("ClientMessageID = %q, want client-msg-abc", msg.ClientMessageID)
	}

	// A second, unrelated user message on the same session must not also
	// claim it — the pending entry is consumed exactly once.
	msg2 := backend.Message{ID: "msg_other", Role: "user"}
	b.resolveClientMessageID("ses_1", &msg2)
	if msg2.ClientMessageID != "" {
		t.Fatalf("second message must not claim the same pending id: %+v", msg2)
	}

	// The mapping survives a fresh Backend loading the same overlay file —
	// simulating an agent restart, or Transcript() re-seeding after one.
	b2 := New(Config{OverlayPath: dir + "/overlay.json"})
	if err := b2.loadOverlay(); err != nil {
		t.Fatal(err)
	}
	reseeded := backend.Message{ID: "msg_new", Role: "user"}
	b2.resolveClientMessageID("ses_1", &reseeded)
	if reseeded.ClientMessageID != "client-msg-abc" {
		t.Fatalf("mapping did not survive reload: %+v", reseeded)
	}
}

func TestResolveClientMessageIDIgnoresAssistantMessages(t *testing.T) {
	b := New(Config{})
	b.claimPendingClientMessageID("ses_1", "client-msg-abc")
	msg := backend.Message{ID: "msg_assistant", Role: "assistant"}
	b.resolveClientMessageID("ses_1", &msg)
	if msg.ClientMessageID != "" {
		t.Fatalf("an assistant message must never claim a pending clientMessageId: %+v", msg)
	}
}

// CompactionPart (opencode's OpenAPI, GET /doc, 1.18.31): a marker part on
// the assistant message that summarized the session. `auto` is the one
// field PROTOCOL.md's normalized `compaction` part carries.
func TestPartFromMapCompaction(t *testing.T) {
	p := partFromMap(map[string]any{
		"id": "prt_3", "messageID": "msg_1", "sessionID": "ses_1", "type": "compaction", "auto": true,
	})
	if p.Type != backend.PartCompaction {
		t.Fatalf("Type = %q, want compaction", p.Type)
	}
	if !p.Auto {
		t.Errorf("Auto = false, want true")
	}
}

func TestPartFromMapCompactionManual(t *testing.T) {
	p := partFromMap(map[string]any{
		"id": "prt_4", "messageID": "msg_1", "type": "compaction", "auto": false,
	})
	if p.Auto {
		t.Errorf("Auto = true, want false")
	}
}

// The session-usage cache (session.get/list, PROTOCOL.md §7): opencode's own
// session object carries no usage field, so the backend caches the latest
// Usage it has observed per session and answers withUsage from that cache.
func TestSessionUsageCache(t *testing.T) {
	b := New(Config{})
	if got := b.withUsage(backend.Session{ID: "s1"}); got.Usage != nil {
		t.Fatalf("Usage = %+v, want nil before any observation", got.Usage)
	}

	u := &backend.Usage{Input: 10, Output: 5, ContextUsed: 15}
	b.setSessionUsage("s1", u)

	got := b.withUsage(backend.Session{ID: "s1"})
	if got.Usage != u {
		t.Fatalf("Usage = %+v, want the cached pointer %+v", got.Usage, u)
	}
	// A different session's cache entry must not leak onto this one.
	if got := b.withUsage(backend.Session{ID: "s2"}); got.Usage != nil {
		t.Fatalf("Usage = %+v, want nil for an untouched session", got.Usage)
	}
}

// A Stop is not a failure: opencode's abort error must not reach Cerea as one.
func TestAbortedMessageIsNotAnError(t *testing.T) {
	aborted := messageFromMap(map[string]any{"id": "m", "role": "assistant",
		"error": map[string]any{"name": "MessageAbortedError", "data": map[string]any{"message": "aborted"}}})
	if aborted.Error != "" {
		t.Errorf("aborted message error = %q, want none", aborted.Error)
	}
	failed := messageFromMap(map[string]any{"id": "m", "role": "assistant",
		"error": map[string]any{"name": "APIError", "message": "upstream 500"}})
	if failed.Error == "" {
		t.Error("a real provider error must still be reported")
	}
}

// The task tool's metadata names the child session; the part must carry it so
// session.children can anchor the subagent at this call.
func TestTaskToolPartCarriesChildSession(t *testing.T) {
	p := partFromMap(map[string]any{"id": "p", "messageID": "m", "type": "tool", "callID": "call_task", "tool": "task",
		"state": map[string]any{"status": "running", "input": map[string]any{"description": "d"},
			"metadata": map[string]any{"sessionId": "ses_child"}}})
	if p.SubtaskSessionID != "ses_child" || p.CallID != "call_task" {
		t.Fatalf("part = %+v", p)
	}
}
