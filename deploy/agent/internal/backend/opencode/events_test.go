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

// question.asked carries the whole ask, verified live against opencode
// 1.18.31's built-in "question" tool (PROTOCOL.md's user-question tool
// design): {id, sessionID, questions: [{question, header, options,
// multiple}], tool: {callID}}.
func TestTranslateEventQuestionAsked(t *testing.T) {
	b := New(Config{})
	events := b.translateEvent("/ws", "question.asked", map[string]any{
		"id":        "que_1",
		"sessionID": "ses_1",
		"questions": []any{
			map[string]any{
				"question": "Which approach?",
				"header":   "Approach",
				"options": []any{
					map[string]any{"label": "A", "description": "Do A"},
					map[string]any{"label": "B", "description": "Do B"},
				},
				"multiple": false,
			},
		},
		"tool": map[string]any{"messageID": "msg_1", "callID": "call_1"},
	})
	if len(events) != 1 {
		t.Fatalf("got %d events, want 1", len(events))
	}
	ev := events[0]
	if ev.SessionID != "ses_1" {
		t.Errorf("SessionID = %q, want ses_1", ev.SessionID)
	}
	if ev.Event.Kind != backend.EventQuestionAsked {
		t.Fatalf("Kind = %v, want EventQuestionAsked", ev.Event.Kind)
	}
	if ev.Event.QuestionRequestID != "que_1" || ev.Event.QuestionCallID != "call_1" {
		t.Errorf("RequestID/CallID = %q/%q", ev.Event.QuestionRequestID, ev.Event.QuestionCallID)
	}
	if len(ev.Event.Questions) != 1 || ev.Event.Questions[0].Question != "Which approach?" ||
		ev.Event.Questions[0].Header != "Approach" || len(ev.Event.Questions[0].Options) != 2 ||
		ev.Event.Questions[0].Options[0].Label != "A" || ev.Event.Questions[0].MultiSelect {
		t.Fatalf("Questions = %+v", ev.Event.Questions)
	}
}

// question.replied and question.rejected both normalize to
// EventQuestionResolved (the same "replied"/"rejected" -> one resolved
// event unification permission.replied already does for its own decisions).
func TestTranslateEventQuestionResolved(t *testing.T) {
	b := New(Config{})

	replied := b.translateEvent("/ws", "question.replied", map[string]any{
		"sessionID": "ses_1", "requestID": "que_1",
		"answers": []any{[]any{"A"}, []any{"X", "Y"}},
	})
	if len(replied) != 1 {
		t.Fatalf("got %d events, want 1", len(replied))
	}
	ev := replied[0].Event
	if ev.Kind != backend.EventQuestionResolved || ev.QuestionRequestID != "que_1" || ev.QuestionDecision != "answered" {
		t.Fatalf("replied event = %+v", ev)
	}
	if len(ev.QuestionAnswers) != 2 || ev.QuestionAnswers[0][0] != "A" || len(ev.QuestionAnswers[1]) != 2 {
		t.Fatalf("QuestionAnswers = %+v", ev.QuestionAnswers)
	}

	rejected := b.translateEvent("/ws", "question.rejected", map[string]any{
		"sessionID": "ses_1", "requestID": "que_2",
	})
	if len(rejected) != 1 {
		t.Fatalf("got %d events, want 1", len(rejected))
	}
	ev2 := rejected[0].Event
	if ev2.Kind != backend.EventQuestionResolved || ev2.QuestionRequestID != "que_2" || ev2.QuestionDecision != "rejected" {
		t.Fatalf("rejected event = %+v", ev2)
	}
}
