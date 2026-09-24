package opencode

import (
	"testing"

	"pystino-agent/internal/backend"
)

// A compaction part rides the ordinary message.part.updated SSE event, like
// any other part type (PROTOCOL.md §7) — no dedicated event kind.
func TestTranslateEventCompactionPart(t *testing.T) {
	b := New(Config{})
	events := b.translateEvent("/ws", "message.part.updated", map[string]any{
		"part": map[string]any{
			"id": "prt_1", "messageID": "msg_1", "sessionID": "ses_1", "type": "compaction", "auto": true,
		},
	})
	if len(events) != 1 {
		t.Fatalf("got %d events, want 1", len(events))
	}
	part := events[0].Event.Part
	if part == nil || part.Type != backend.PartCompaction || !part.Auto {
		t.Fatalf("Part = %+v, want a compaction part with Auto=true", part)
	}
	if events[0].SessionID != "ses_1" {
		t.Errorf("SessionID = %q, want ses_1", events[0].SessionID)
	}
}

// An assistant message.updated event with tokens caches the session's usage
// (session.get/list read it back, PROTOCOL.md §7) alongside emitting the
// live usage event.
func TestTranslateEventMessageUpdatedCachesUsage(t *testing.T) {
	b := New(Config{})
	events := b.translateEvent("/ws", "message.updated", map[string]any{
		"info": map[string]any{
			"id": "msg_1", "sessionID": "ses_1", "role": "assistant",
			"tokens": map[string]any{"input": float64(10), "output": float64(5)},
		},
	})
	var sawUsage bool
	for _, ev := range events {
		if ev.Event.Kind == backend.EventUsage {
			sawUsage = true
		}
	}
	if !sawUsage {
		t.Fatal("expected a usage event alongside the message event")
	}
	cached := b.withUsage(backend.Session{ID: "ses_1"})
	if cached.Usage == nil || cached.Usage.Input != 10 || cached.Usage.Output != 5 {
		t.Fatalf("cached usage = %+v, want Input=10 Output=5", cached.Usage)
	}
}
