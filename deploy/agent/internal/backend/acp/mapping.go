package acp

import (
	"encoding/base64"
	"strings"

	"pystino-agent/internal/backend"
)

// This file reads ACP's JSON shapes defensively (multiple candidate keys,
// tolerant of absence), the same discipline internal/backend/opencode's
// mapping.go documents: ACP's own spec has unstable corners (session/new's
// modes/models is a draft; found live, opencode 1.18.31 answers with its
// own configOptions shape instead), so a mapping written against one ACP
// agent should degrade gracefully against another rather than panic or
// silently drop everything. The initialize/session/new/session/prompt/
// session/update shapes below were verified live against opencode 1.18.31's
// `opencode acp` (see acp_it_test.go); the rest follow the published ACP
// schema as closely as a stdlib-only, dependency-free reader reasonably can.

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

// present reports whether m has key with a value that is neither absent,
// null nor false — ACP's initialize result advertises sessionCapabilities
// entries as small objects ({}), not booleans (found live), so "the key
// exists with a non-false value" is what "advertised" means here.
func present(m map[string]any, key string) bool {
	v, ok := m[key]
	if !ok || v == nil {
		return false
	}
	if b, isBool := v.(bool); isBool {
		return b
	}
	return true
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

// mapToolStatus turns ACP's tool_call/tool_call_update status into the
// normalized backend.ToolStatus (PROTOCOL.md §7: pending/running/completed
// /error).
func mapToolStatus(s string) backend.ToolStatus {
	switch s {
	case "pending":
		return backend.ToolPending
	case "in_progress":
		return backend.ToolRunning
	case "completed":
		return backend.ToolCompleted
	case "failed":
		return backend.ToolFailed
	default:
		return backend.ToolPending
	}
}

// toolCallText pulls the human-readable text out of tool_call_update's
// content array ({content:[{type:"content",content:{type:"text",text}}]},
// verified live) or, failing that, a "text"/"output" string anywhere
// reasonable in rawOutput — ACP does not pin rawOutput's shape at all (it
// is the tool's own arbitrary result), so this is best-effort by design.
func toolCallText(m map[string]any) string {
	for _, c := range asMaps(getSlice(m, "content")) {
		inner := getMap(c, "content")
		if inner == nil {
			inner = c
		}
		if t := getStr(inner, "text"); t != "" {
			return t
		}
	}
	if raw := getMap(m, "rawOutput"); raw != nil {
		if t := getStr(raw, "output", "text", "error"); t != "" {
			return t
		}
	}
	return ""
}

// dataURLPayload strips a "data:<mime>;base64,<payload>" URL down to its
// base64 payload, re-encoding if the URL turns out not to already be
// base64 (Attachment.URL is typed as a data: URL for P0 per PROTOCOL.md
// §6, but nothing stops a caller handing this a raw string — decoding then
// re-encoding is cheap insurance against sending the agent malformed
// content instead of failing the whole prompt).
func dataURLPayload(url string) string {
	if i := strings.Index(url, ","); i >= 0 && strings.Contains(url[:i], ";base64") {
		return url[i+1:]
	}
	return base64.StdEncoding.EncodeToString([]byte(url))
}

// promptContentBlocks turns a normalized Prompt into ACP's prompt content
// block array (PROTOCOL.md/the task: text plus images as
// {type:"image",mimeType,data}).
func promptContentBlocks(p backend.Prompt) []map[string]any {
	blocks := make([]map[string]any, 0, 1+len(p.Attachments))
	if p.Text != "" {
		blocks = append(blocks, map[string]any{"type": "text", "text": p.Text})
	}
	for _, a := range p.Attachments {
		if !strings.HasPrefix(a.Mime, "image/") {
			continue
		}
		blocks = append(blocks, map[string]any{
			"type": "image", "mimeType": a.Mime, "data": dataURLPayload(a.URL),
		})
	}
	return blocks
}

// sessionOptions extracts session/new's (or session/load's) advertised
// modes and models. Two shapes are read: the published ACP draft schema
// (modes.availableModes/models.availableModels, per the task) and
// opencode 1.18.31's actual live shape (a flat configOptions list with
// entries id "mode"/"model") — see the file doc for why both are handled.
func sessionOptions(m map[string]any) (modes []backend.Mode, models []backend.Model, currentModeID, currentModelID string) {
	if modesObj := getMap(m, "modes"); modesObj != nil {
		currentModeID = getStr(modesObj, "currentModeId")
		for _, mm := range asMaps(getSlice(modesObj, "availableModes")) {
			modes = append(modes, backend.Mode{ID: getStr(mm, "id"), Label: getStr(mm, "name"), Description: getStr(mm, "description")})
		}
	}
	if modelsObj := getMap(m, "models"); modelsObj != nil {
		currentModelID = getStr(modelsObj, "currentModelId")
		for _, mm := range asMaps(getSlice(modelsObj, "availableModels")) {
			models = append(models, backend.Model{ID: getStr(mm, "modelId", "id"), Label: getStr(mm, "name")})
		}
	}
	for _, opt := range asMaps(getSlice(m, "configOptions")) {
		switch getStr(opt, "id") {
		case "mode":
			if currentModeID == "" {
				currentModeID = getStr(opt, "currentValue")
			}
			if len(modes) == 0 {
				for _, o := range asMaps(getSlice(opt, "options")) {
					modes = append(modes, backend.Mode{ID: getStr(o, "value"), Label: getStr(o, "name"), Description: getStr(o, "description")})
				}
			}
		case "model":
			if currentModelID == "" {
				currentModelID = getStr(opt, "currentValue")
			}
			if len(models) == 0 {
				for _, o := range asMaps(getSlice(opt, "options")) {
					models = append(models, backend.Model{ID: getStr(o, "value"), Label: getStr(o, "name")})
				}
			}
		}
	}
	return modes, models, currentModeID, currentModelID
}
