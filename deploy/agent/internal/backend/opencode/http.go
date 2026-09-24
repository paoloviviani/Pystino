package opencode

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"pystino-agent/internal/backend"
)

// doJSONTimeout bounds every control-plane call doJSON makes. All of them
// are supposed to be fast — session CRUD, prompt_async (204, fire and
// forget; the model's actual reply streams over SSE separately), abort,
// permission replies, listings — never a call that legitimately runs long.
// Capping it here means a single wedged request can't hang a caller
// forever regardless of what deadline (if any) it happened to pass in.
const doJSONTimeout = 20 * time.Second

// Compile-time checks: Backend must satisfy the floor interface and every
// optional capability its Capabilities() advertises as true.
var (
	_ backend.Backend    = (*Backend)(nil)
	_ backend.Differ     = (*Backend)(nil)
	_ backend.Childrener = (*Backend)(nil)
	_ backend.Compactor  = (*Backend)(nil)
	_ backend.Asker      = (*Backend)(nil)
)

// doJSON issues one request against the supervised opencode instance,
// basic-authenticated, and decodes a JSON response into out (nil to
// discard the body — POST /session/:id/prompt_async answers 204).
func (b *Backend) doJSON(ctx context.Context, method, path string, body any, out any) error {
	ctx, cancel := context.WithTimeout(ctx, doJSONTimeout)
	defer cancel()
	var reader io.Reader
	if body != nil {
		buf, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(buf)
	}
	req, err := http.NewRequestWithContext(ctx, method, b.baseURL()+path, reader)
	if err != nil {
		return err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	req.SetBasicAuth("opencode", b.cfg.Password)

	resp, err := b.client.Do(req)
	if err != nil {
		return fmt.Errorf("opencode %s %s: %w", method, path, err)
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if resp.StatusCode >= 300 {
		return fmt.Errorf("opencode %s %s: status %d: %s", method, path, resp.StatusCode, strings.TrimSpace(string(respBody)))
	}
	if out == nil || len(respBody) == 0 {
		return nil
	}
	if err := json.Unmarshal(respBody, out); err != nil {
		return fmt.Errorf("opencode %s %s: parsing response: %w", method, path, err)
	}
	return nil
}

func directoryQuery(workspaceDir string) string {
	return "?directory=" + url.QueryEscape(workspaceDir)
}

func (b *Backend) Capabilities() backend.Capabilities {
	return backend.Capabilities{
		Diff: true, Children: true, Usage: true, Compact: true,
		Images: true, Files: true, Worktrees: false, AutoAccept: true,
		Questions: true,
	}
}

func (b *Backend) ListSessions(ctx context.Context, workspaceDir string) ([]backend.Session, error) {
	var raw []any
	if err := b.doJSON(ctx, http.MethodGet, "/session"+directoryQuery(workspaceDir), nil, &raw); err != nil {
		return nil, err
	}
	out := make([]backend.Session, 0, len(raw))
	for _, m := range asMaps(raw) {
		out = append(out, b.withUsage(sessionFromMap(m)))
	}
	return out, nil
}

func (b *Backend) GetSession(ctx context.Context, _ string, sessionID string) (backend.Session, error) {
	var m map[string]any
	if err := b.doJSON(ctx, http.MethodGet, "/session/"+url.PathEscape(sessionID), nil, &m); err != nil {
		return backend.Session{}, err
	}
	s := sessionFromMap(m)
	ov := b.getOverlay(sessionID)
	s.ModeID, s.ModelID = ov.ModeID, ov.ModelID
	return b.withUsage(s), nil
}

func (b *Backend) CreateSession(ctx context.Context, workspaceDir string, opts backend.CreateSessionOptions) (backend.Session, error) {
	body := map[string]any{}
	if opts.Title != "" {
		body["title"] = opts.Title
	}
	var m map[string]any
	if err := b.doJSON(ctx, http.MethodPost, "/session"+directoryQuery(workspaceDir), body, &m); err != nil {
		return backend.Session{}, err
	}
	s := sessionFromMap(m)
	if opts.ModeID != "" || opts.ModelID != "" {
		if err := b.setOverlay(s.ID, sessionOverlay{ModeID: opts.ModeID, ModelID: opts.ModelID}); err != nil {
			return backend.Session{}, err
		}
	}
	s.ModeID, s.ModelID = opts.ModeID, opts.ModelID
	return b.withUsage(s), nil
}

func (b *Backend) RenameSession(ctx context.Context, _ string, sessionID, title string) (backend.Session, error) {
	var m map[string]any
	if err := b.doJSON(ctx, http.MethodPatch, "/session/"+url.PathEscape(sessionID), map[string]any{"title": title}, &m); err != nil {
		return backend.Session{}, err
	}
	s := sessionFromMap(m)
	ov := b.getOverlay(sessionID)
	s.ModeID, s.ModelID = ov.ModeID, ov.ModelID
	return b.withUsage(s), nil
}

func (b *Backend) DeleteSession(ctx context.Context, _ string, sessionID string) error {
	return b.doJSON(ctx, http.MethodDelete, "/session/"+url.PathEscape(sessionID), nil, nil)
}

func (b *Backend) Prompt(ctx context.Context, _ string, sessionID string, prompt backend.Prompt) error {
	parts := make([]map[string]any, 0, 1+len(prompt.Attachments))
	if prompt.Text != "" {
		parts = append(parts, map[string]any{"type": "text", "text": prompt.Text})
	}
	for _, a := range prompt.Attachments {
		parts = append(parts, map[string]any{
			"type": "file", "mime": a.Mime, "filename": a.Filename, "url": a.URL,
		})
	}
	body := map[string]any{"parts": parts}
	ov := b.getOverlay(sessionID)
	if ov.ModeID != "" {
		body["agent"] = ov.ModeID
	}
	if ov.ModelID != "" {
		providerID, modelID := splitModelID(ov.ModelID)
		body["model"] = map[string]string{"providerID": providerID, "modelID": modelID}
	}
	// prompt_async's documented body has no field for an externally chosen
	// message id, so the clientMessageId is matched to whichever new user
	// message shows up next for this session (PROTOCOL.md §7) rather than
	// sent in the request. Worth re-checking against a live GET /doc if the
	// IT test ever shows a way to pass one explicitly — that would be more
	// precise than the "next message" heuristic.
	b.claimPendingClientMessageID(sessionID, prompt.ClientMessageID)
	return b.doJSON(ctx, http.MethodPost, "/session/"+url.PathEscape(sessionID)+"/prompt_async", body, nil)
}

func (b *Backend) Cancel(ctx context.Context, _ string, sessionID string) error {
	return b.doJSON(ctx, http.MethodPost, "/session/"+url.PathEscape(sessionID)+"/abort", nil, nil)
}

func (b *Backend) SetMode(ctx context.Context, workspaceDir, sessionID, modeID string) (backend.Session, error) {
	ov := b.getOverlay(sessionID)
	ov.ModeID = modeID
	if err := b.setOverlay(sessionID, ov); err != nil {
		return backend.Session{}, err
	}
	return b.GetSession(ctx, workspaceDir, sessionID)
}

func (b *Backend) SetModel(ctx context.Context, workspaceDir, sessionID, modelID string) (backend.Session, error) {
	ov := b.getOverlay(sessionID)
	ov.ModelID = modelID
	if err := b.setOverlay(sessionID, ov); err != nil {
		return backend.Session{}, err
	}
	return b.GetSession(ctx, workspaceDir, sessionID)
}

func (b *Backend) ReplyPermission(ctx context.Context, workspaceDir string, _ string, requestID string, decision backend.Decision, message string) error {
	body := map[string]any{"reply": string(decision)}
	if message != "" {
		body["message"] = message
	}
	// directory is optional in the OpenAPI schema but load-bearing in
	// practice (found live): omitting it 404s with PermissionNotFoundError
	// even for a request id that was just seen in a live permission.asked
	// event for this exact session.
	return b.doJSON(ctx, http.MethodPost, "/permission/"+url.PathEscape(requestID)+"/reply"+directoryQuery(workspaceDir), body, nil)
}

func (b *Backend) Modes(ctx context.Context, _ string) ([]backend.Mode, error) {
	var raw []any
	if err := b.doJSON(ctx, http.MethodGet, "/agent", nil, &raw); err != nil {
		return nil, err
	}
	out := make([]backend.Mode, 0, len(raw))
	for _, m := range asMaps(raw) {
		if mode, ok := modeFromAgentMap(m); ok {
			out = append(out, mode)
		}
	}
	return out, nil
}

func (b *Backend) Models(ctx context.Context, _ string) ([]backend.Model, error) {
	var root map[string]any
	if err := b.doJSON(ctx, http.MethodGet, "/config/providers", nil, &root); err != nil {
		return nil, err
	}
	defaults := getMap(root, "default")
	var out []backend.Model
	newLimits := map[string]int{}
	for _, p := range asMaps(getSlice(root, "providers")) {
		providerID := getStr(p, "id")
		modelsField, _ := p["models"].(map[string]any)
		for modelID, raw := range modelsField {
			mm, _ := raw.(map[string]any)
			if mm == nil {
				continue
			}
			id := providerID + "/" + modelID
			model := backend.Model{
				ID:         id,
				Label:      getStr(mm, "name"),
				ProviderID: providerID,
			}
			if model.Label == "" {
				model.Label = modelID
			}
			if limit := getMap(mm, "limit"); limit != nil {
				model.ContextWindow = getInt(limit, "context")
				newLimits[id] = model.ContextWindow
			}
			if attach := getMap(mm, "attachment"); attach != nil {
				model.Images = getBool(attach, "image")
			}
			model.Reasoning = getBool(mm, "reasoning")
			if defaults != nil && getStr(defaults, providerID) == modelID {
				model.IsDefault = true
			}
			out = append(out, model)
		}
	}
	b.modelsMu.Lock()
	b.modelLimit = newLimits
	b.modelsMu.Unlock()
	return out, nil
}

func (b *Backend) Transcript(ctx context.Context, _ string, sessionID string) (backend.Transcript, error) {
	var raw []any
	if err := b.doJSON(ctx, http.MethodGet, "/session/"+url.PathEscape(sessionID)+"/message", nil, &raw); err != nil {
		return backend.Transcript{}, err
	}
	tr := backend.Transcript{Status: backend.StatusIdle}
	for _, entry := range asMaps(raw) {
		info := getMap(entry, "info")
		if info == nil {
			info = entry
		}
		msg := messageFromMap(info)
		b.resolveClientMessageID(sessionID, &msg)
		var parts []backend.Part
		for _, pm := range asMaps(getSlice(entry, "parts")) {
			parts = append(parts, partFromMap(pm))
		}
		tr.Messages = append(tr.Messages, backend.TranscriptEntry{Message: msg, Parts: parts})
		if msg.Role == "assistant" {
			if u := usageFromMessageMap(info); u != nil {
				b.fillContextMax(info, u)
				tr.Usage = u
				b.setSessionUsage(sessionID, u)
			}
			if msg.Error != "" {
				tr.Status = backend.StatusError
			}
		}
	}

	var permsRaw []any
	if err := b.doJSON(ctx, http.MethodGet, "/permission", nil, &permsRaw); err == nil {
		for _, pm := range asMaps(permsRaw) {
			req := permissionFromMap(pm)
			if req.SessionID == sessionID {
				tr.Permissions = append(tr.Permissions, req)
			}
		}
	}
	return tr, nil
}

func (b *Backend) Diff(ctx context.Context, _ string, sessionID string) ([]backend.FileDiff, error) {
	var raw []any
	if err := b.doJSON(ctx, http.MethodGet, "/session/"+url.PathEscape(sessionID)+"/diff", nil, &raw); err != nil {
		return nil, err
	}
	out := make([]backend.FileDiff, 0, len(raw))
	for _, m := range asMaps(raw) {
		out = append(out, fileDiffFromMap(m))
	}
	return out, nil
}

func (b *Backend) Children(ctx context.Context, _ string, sessionID string) ([]backend.Session, error) {
	var raw []any
	if err := b.doJSON(ctx, http.MethodGet, "/session/"+url.PathEscape(sessionID)+"/children", nil, &raw); err != nil {
		return nil, err
	}
	out := make([]backend.Session, 0, len(raw))
	for _, m := range asMaps(raw) {
		out = append(out, sessionFromMap(m))
	}
	return out, nil
}

func (b *Backend) Compact(ctx context.Context, _ string, sessionID string) error {
	return b.doJSON(ctx, http.MethodPost, "/session/"+url.PathEscape(sessionID)+"/summarize", nil, nil)
}

// ReplyQuestion answers a pending question.asked (the user-question tool
// design). Like ReplyPermission, directory is required in practice even
// though the OpenAPI schema marks it optional — found live, the same
// PermissionNotFoundError-style 404 without it.
func (b *Backend) ReplyQuestion(ctx context.Context, workspaceDir, _, requestID string, answers [][]string) error {
	body := map[string]any{"answers": answers}
	return b.doJSON(ctx, http.MethodPost, "/question/"+url.PathEscape(requestID)+"/reply"+directoryQuery(workspaceDir), body, nil)
}

// RejectQuestion dismisses a pending question.asked without answering it —
// opencode reports the tool call itself as an error ("The user dismissed
// this question"), verified live.
func (b *Backend) RejectQuestion(ctx context.Context, workspaceDir, _, requestID string) error {
	return b.doJSON(ctx, http.MethodPost, "/question/"+url.PathEscape(requestID)+"/reject"+directoryQuery(workspaceDir), nil, nil)
}

// splitModelID turns "<providerID>/<modelID>" into its two halves. A
// malformed id (no slash) is passed through as the model half with an
// empty provider, which opencode will simply refuse — better than this
// package guessing.
func splitModelID(id string) (providerID, modelID string) {
	if i := strings.IndexByte(id, '/'); i >= 0 {
		return id[:i], id[i+1:]
	}
	return "", id
}
