package opencode

import (
	"time"

	"pystino-agent/internal/backend"
)

// millisToTime converts an opencode epoch-millisecond timestamp (0 = unset)
// into a time.Time, leaving the zero value for 0 rather than mapping it to
// the Unix epoch — a Session/Message with no timestamp yet should compare
// as IsZero(), not as 1970.
func millisToTime(ms float64) time.Time {
	if ms <= 0 {
		return time.Time{}
	}
	return time.UnixMilli(int64(ms))
}

// This file maps opencode's own JSON shapes onto the normalized backend
// types. Fields are read defensively (multiple candidate keys, tolerant of
// absence) rather than via strict structs: opencode's server API is not
// covered by a stability guarantee the way its wire protocol version is,
// and a renamed or added field should degrade gracefully instead of
// breaking decoding outright. The IT test (PYSTINO_AGENT_OPENCODE_IT=1)
// against a real, pinned opencode is what actually pins these shapes; this
// mapping is written from PROTOCOL.md §2's live-verified endpoint notes and
// corrected against that test as needed.

func getStr(m map[string]any, keys ...string) string {
	for _, k := range keys {
		if v, ok := m[k]; ok {
			if s, ok := v.(string); ok {
				return s
			}
		}
	}
	return ""
}

func getFloat(m map[string]any, keys ...string) float64 {
	for _, k := range keys {
		if v, ok := m[k]; ok {
			switch n := v.(type) {
			case float64:
				return n
			case int:
				return float64(n)
			}
		}
	}
	return 0
}

func getInt(m map[string]any, keys ...string) int {
	return int(getFloat(m, keys...))
}

func getBool(m map[string]any, keys ...string) bool {
	for _, k := range keys {
		if v, ok := m[k]; ok {
			if b, ok := v.(bool); ok {
				return b
			}
		}
	}
	return false
}

func getMap(m map[string]any, keys ...string) map[string]any {
	for _, k := range keys {
		if v, ok := m[k]; ok {
			if sub, ok := v.(map[string]any); ok {
				return sub
			}
		}
	}
	return nil
}

func getSlice(m map[string]any, keys ...string) []any {
	for _, k := range keys {
		if v, ok := m[k]; ok {
			if s, ok := v.([]any); ok {
				return s
			}
		}
	}
	return nil
}

func asMaps(items []any) []map[string]any {
	out := make([]map[string]any, 0, len(items))
	for _, it := range items {
		if m, ok := it.(map[string]any); ok {
			out = append(out, m)
		}
	}
	return out
}

// sessionFromMap builds a backend.Session from one entry of GET /session,
// POST /session, or GET /session/:id. WorkspaceID is left empty: opencode
// only knows a filesystem "directory", not the agent's own workspace
// registry id, so the caller (the op dispatcher, which holds both) fills
// it in.
func sessionFromMap(m map[string]any) backend.Session {
	s := backend.Session{
		ID:       getStr(m, "id"),
		Backend:  "opencode",
		Title:    getStr(m, "title"),
		Status:   backend.StatusIdle,
		ParentID: getStr(m, "parentID", "parentId"),
	}
	if t := getMap(m, "time"); t != nil {
		s.CreatedAt = millisToTime(getFloat(t, "created"))
		s.UpdatedAt = millisToTime(getFloat(t, "updated"))
	}
	return s
}

func messageFromMap(m map[string]any) backend.Message {
	msg := backend.Message{
		ID:       getStr(m, "id"),
		Role:     getStr(m, "role"),
		ParentID: getStr(m, "parentID", "parentId"),
	}
	if t := getMap(m, "time"); t != nil {
		msg.CreatedAt = millisToTime(getFloat(t, "created"))
		if ms := getFloat(t, "completed"); ms > 0 {
			completed := millisToTime(ms)
			msg.CompletedAt = &completed
		}
	}
	// An abort is how opencode records a person pressing Stop (session.cancel),
	// not a failure: surfacing it as an error would show a failed turn for a
	// deliberate stop, so it ends the message like a normal finish.
	if e := getMap(m, "error"); e != nil && getStr(e, "name") != "MessageAbortedError" {
		msg.Error = getStr(e, "message", "name")
	}
	return msg
}

func partFromMap(m map[string]any) backend.Part {
	p := backend.Part{
		ID:        getStr(m, "id"),
		MessageID: getStr(m, "messageID", "messageId"),
		Role:      getStr(m, "role"),
		Type:      backend.PartType(getStr(m, "type")),
	}
	switch p.Type {
	case backend.PartText, backend.PartReason:
		p.Text = getStr(m, "text")
		p.Synthetic = getBool(m, "synthetic")
	case backend.PartTool:
		p.CallID = getStr(m, "callID", "callId")
		p.Tool = getStr(m, "tool")
		// Everything about a tool call's progress lives under "state"
		// (found live — 1.18.31 does not put status/input/output/title at
		// the part's top level): {input, status, output?, title?, time,
		// metadata}. status observed: pending, running, completed.
		state := getMap(m, "state")
		if state == nil {
			state = m
		}
		p.ToolStatus = backend.ToolStatus(getStr(state, "status"))
		// opencode's task tool names the child session it spawned in its
		// metadata; carrying it on the part is what links a subagent to the call.
		if md := getMap(state, "metadata"); md != nil {
			p.SubtaskSessionID = getStr(md, "sessionId", "sessionID")
		}
		p.Title = getStr(state, "title")
		if in := getMap(state, "input"); in != nil {
			p.Input = in
		}
		p.Output = getStr(state, "output")
		if e := getMap(state, "error"); e != nil {
			p.ToolError = getStr(e, "message", "name")
		} else {
			p.ToolError = getStr(state, "error")
		}
	case backend.PartFile:
		p.Mime = getStr(m, "mime")
		p.Filename = getStr(m, "filename")
		p.URL = getStr(m, "url")
	case backend.PartSubtask:
		p.SubtaskSessionID = getStr(m, "sessionID", "sessionId")
		p.Description = getStr(m, "description")
		p.Agent = getStr(m, "agent")
	case backend.PartCompaction:
		p.Auto = getBool(m, "auto")
	}
	return p
}

func permissionFromMap(m map[string]any) backend.PermissionRequest {
	tool := getMap(m, "tool")
	req := backend.PermissionRequest{
		ID:        getStr(m, "id"),
		SessionID: getStr(m, "sessionID", "sessionId"),
		Tool:      getStr(m, "permission", "tool", "type"),
		Title:     getStr(m, "title"),
	}
	if patterns := getSlice(m, "patterns"); patterns != nil {
		for _, p := range patterns {
			if s, ok := p.(string); ok {
				req.Patterns = append(req.Patterns, s)
			}
		}
	}
	if md := getMap(m, "metadata"); md != nil {
		req.Metadata = md
	}
	if tool != nil {
		req.MessageID = getStr(tool, "messageID", "messageId")
		req.CallID = getStr(tool, "callID", "callId")
	}
	if always := getSlice(m, "always"); always != nil {
		for _, a := range always {
			if s, ok := a.(string); ok {
				req.Always = append(req.Always, s)
			}
		}
	}
	return req
}

// modeFromAgentMap converts one GET /agent entry, returning ok=false for
// anything that is not a user-facing primary mode (PROTOCOL.md §2's
// "modes = entries with mode 'primary', hide compaction/summary/title").
func modeFromAgentMap(m map[string]any) (backend.Mode, bool) {
	name := getStr(m, "name")
	mode := getStr(m, "mode")
	if mode != "primary" {
		return backend.Mode{}, false
	}
	switch name {
	case "compaction", "summary", "title":
		return backend.Mode{}, false
	}
	return backend.Mode{ID: name, Label: name, Description: getStr(m, "description")}, true
}

func fileDiffFromMap(m map[string]any) backend.FileDiff {
	return backend.FileDiff{
		Path:      getStr(m, "path", "file"),
		Status:    backend.FileDiffStatus(getStr(m, "status")),
		Before:    getStr(m, "before"),
		After:     getStr(m, "after"),
		Additions: getInt(m, "additions"),
		Deletions: getInt(m, "deletions"),
	}
}

func todoFromMap(m map[string]any) backend.Todo {
	return backend.Todo{
		ID:       getStr(m, "id"),
		Content:  getStr(m, "content"),
		Status:   backend.TodoStatus(getStr(m, "status")),
		Priority: getStr(m, "priority"),
	}
}

// usageFromMessageMap builds a Usage from an assistant message's tokens
// object, or nil if it carries none (a user message, or an assistant
// message opencode hasn't attached usage to yet).
func usageFromMessageMap(m map[string]any) *backend.Usage {
	tokens := getMap(m, "tokens")
	if tokens == nil {
		return nil
	}
	u := &backend.Usage{
		Input:     getInt(tokens, "input"),
		Output:    getInt(tokens, "output"),
		Reasoning: getInt(tokens, "reasoning"),
		Cost:      getFloat(m, "cost"),
	}
	if cache := getMap(tokens, "cache"); cache != nil {
		u.CacheRead = getInt(cache, "read")
		u.CacheWrite = getInt(cache, "write")
	}
	u.ContextUsed = u.Input + u.Output + u.Reasoning + u.CacheRead + u.CacheWrite
	return u
}

// setSessionUsage records sessionID's latest known Usage, observed off the
// event stream (opencode's own session object carries no usage field).
func (b *Backend) setSessionUsage(sessionID string, u *backend.Usage) {
	b.usageMu.Lock()
	b.sessionUsage[sessionID] = u
	b.usageMu.Unlock()
}

// withUsage fills s.Usage from the cache setSessionUsage populates, so
// session.get/list carry the latest usage without a Transcript() round trip
// per call. A session untouched by any event since this process started has
// no cache entry yet and keeps s.Usage nil — the caller's Transcript() (or
// session.sync's snapshot) is what backfills that case.
func (b *Backend) withUsage(s backend.Session) backend.Session {
	b.usageMu.Lock()
	s.Usage = b.sessionUsage[s.ID]
	b.usageMu.Unlock()
	return s
}
