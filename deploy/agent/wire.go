package main

import (
	"pystino-agent/internal/backend"
)

// eventToWire renders a normalized backend.Event into the wire shape
// PROTOCOL.md §7 defines for an "event" frame's "event" field. It lives
// here (package main), not in internal/backend, because backend.Event
// deliberately carries no json tags of its own — Kind and the "message"
// field both wanting the JSON key "message" for two different kinds is
// exactly the ambiguity encoding/json resolves by silently dropping both,
// so this is written out by hand instead of trusted to struct tags.
func eventToWire(ev backend.Event) map[string]any {
	m := map[string]any{"kind": string(ev.Kind)}
	switch ev.Kind {
	case backend.EventMessage:
		m["message"] = ev.Message
	case backend.EventPart:
		m["part"] = ev.Part
	case backend.EventDelta:
		m["messageId"] = ev.MessageID
		m["partId"] = ev.PartID
		m["role"] = ev.Role
		m["field"] = ev.Field
		m["delta"] = ev.Delta
	case backend.EventPartRemoved:
		m["messageId"] = ev.MessageID
		m["partId"] = ev.PartID
	case backend.EventStatus:
		m["status"] = ev.Status
		if ev.Detail != "" {
			m["detail"] = ev.Detail
		}
	case backend.EventPermissionAsked:
		m["request"] = ev.Request
	case backend.EventPermissionReplied:
		m["requestId"] = ev.RequestID
		m["decision"] = ev.Decision
		m["by"] = ev.By
	case backend.EventUsage:
		m["usage"] = ev.Usage
	case backend.EventSession:
		m["session"] = ev.Session
	case backend.EventError:
		m["message"] = ev.ErrorMessage
		if ev.ErrorCode != "" {
			m["code"] = ev.ErrorCode
		}
	case backend.EventTodo:
		m["todos"] = ev.Todos
	case backend.EventQuestionAsked:
		request := map[string]any{
			"id":        ev.QuestionRequestID,
			"questions": orEmpty(ev.Questions),
		}
		if ev.QuestionCallID != "" {
			request["callId"] = ev.QuestionCallID
		}
		m["request"] = request
	case backend.EventQuestionResolved:
		m["requestId"] = ev.QuestionRequestID
		if ev.QuestionDecision == "rejected" {
			m["rejected"] = true
		} else {
			m["answers"] = orEmpty(ev.QuestionAnswers)
		}
	}
	return m
}
