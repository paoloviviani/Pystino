package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sync"

	"pystino-agent/internal/backend"
	"pystino-agent/internal/checkout"
	"pystino-agent/internal/link"
	"pystino-agent/internal/policy"
	"pystino-agent/internal/sessions"
	"pystino-agent/internal/workspaces"
)

// machine ties every internal package together into the one thing the
// link's Handler needs: given an op and its args, do the corresponding
// thing to the workspace registry, the session materializer or the
// backend, and return a result link can marshal into a res frame.
type machine struct {
	workspaces *workspaces.Registry
	back       backend.Backend
	mat        *sessions.Materializer
	pol        policy.Policy

	mu                 sync.Mutex
	sessionWorkspaceID map[string]string // sessionID -> workspace registry id
}

func newMachine(reg *workspaces.Registry, back backend.Backend, mat *sessions.Materializer, pol policy.Policy) *machine {
	return &machine{
		workspaces:         reg,
		back:               back,
		mat:                mat,
		pol:                pol,
		sessionWorkspaceID: map[string]string{},
	}
}

func opErrf(code, format string, args ...any) *link.OpError {
	return &link.OpError{Code: code, Message: fmt.Sprintf(format, args...)}
}

func notFound(what string) *link.OpError  { return opErrf("not_found", "%s not found", what) }
func invalidArgs(err error) *link.OpError { return opErrf("invalid", "bad arguments: %v", err) }
func backendErr(err error) *link.OpError  { return opErrf("backend", "%v", err) }

// trackSession records sessionID's workspace both in the materializer
// (which needs the directory) and here (which needs the registry's own
// opaque workspace id, since a Backend only ever knows a directory).
func (mc *machine) trackSession(w workspaces.Workspace, s backend.Session) {
	mc.mat.Track(w.Path, s)
	mc.mu.Lock()
	mc.sessionWorkspaceID[s.ID] = w.ID
	mc.mu.Unlock()
}

func (mc *machine) resolveSession(sessionID string) (dir, workspaceID string, operr *link.OpError) {
	dir, ok := mc.mat.WorkspaceDir(sessionID)
	if !ok {
		return "", "", notFound("session")
	}
	mc.mu.Lock()
	workspaceID = mc.sessionWorkspaceID[sessionID]
	mc.mu.Unlock()
	return dir, workspaceID, nil
}

// enrich fills in the fields only this process's own state can supply — a
// Backend has no notion of workspace registry ids, pending permissions or
// auto-accept.
func (mc *machine) enrich(s backend.Session, workspaceID string) backend.Session {
	s.WorkspaceID = workspaceID
	s.PendingPermissions = mc.mat.PendingPermissions(s.ID)
	s.AutoAccept = mc.mat.AutoAccept(s.ID)
	if status, ok := mc.mat.Status(s.ID); ok && status != "" {
		s.Status = status
	}
	return s
}

// Handle implements link.Handler: PROTOCOL.md §6's whole op table.
func (mc *machine) Handle(ctx context.Context, op string, args json.RawMessage) (any, *link.OpError) {
	switch op {
	case "workspace.list":
		return mc.opWorkspaceList()
	case "workspace.create":
		return mc.opWorkspaceCreate(args)
	case "workspace.rename":
		return mc.opWorkspaceRename(args)
	case "workspace.archive":
		return mc.opWorkspaceArchive(args)

	case "session.list":
		return mc.opSessionList(ctx, args)
	case "session.get":
		return mc.opSessionGet(ctx, args)
	case "session.create":
		return mc.opSessionCreate(ctx, args)
	case "session.prompt":
		return mc.opSessionPrompt(ctx, args)
	case "session.cancel":
		return mc.opSessionCancel(ctx, args)
	case "session.rename":
		return mc.opSessionRename(ctx, args)
	case "session.archive", "session.delete":
		return mc.opSessionDelete(ctx, args)
	case "session.setMode":
		return mc.opSessionSetMode(ctx, args)
	case "session.setModel":
		return mc.opSessionSetModel(ctx, args)
	case "session.setAutoAccept":
		return mc.opSessionSetAutoAccept(args)
	case "session.sync":
		return mc.opSessionSync(ctx, args)
	case "session.diff":
		return mc.opSessionDiff(ctx, args)
	case "session.children":
		return mc.opSessionChildren(ctx, args)
	case "session.compact":
		return mc.opSessionCompact(ctx, args)

	case "permission.reply":
		return mc.opPermissionReply(ctx, args)

	case "backend.modes":
		return mc.opBackendModes(ctx, args)
	case "backend.models":
		return mc.opBackendModels(ctx, args)

	default:
		return nil, opErrf("unsupported", "unknown op %q", op)
	}
}

func (mc *machine) opWorkspaceList() (any, *link.OpError) {
	return map[string]any{"workspaces": orEmpty(mc.workspaces.List(false))}, nil
}

func (mc *machine) opWorkspaceCreate(args json.RawMessage) (any, *link.OpError) {
	var a struct {
		Path  string `json:"path"`
		Title string `json:"title,omitempty"`
	}
	if err := json.Unmarshal(args, &a); err != nil {
		return nil, invalidArgs(err)
	}
	title := a.Title
	if title == "" {
		title = a.Path
	}
	w, err := mc.workspaces.Create(title, a.Path, mc.pol.WorkspaceRoots)
	if err != nil {
		return nil, opErrf("forbidden", "%v", err)
	}
	return map[string]any{"workspace": w}, nil
}

func (mc *machine) opWorkspaceRename(args json.RawMessage) (any, *link.OpError) {
	var a struct {
		WorkspaceID string `json:"workspaceId"`
		Title       string `json:"title"`
	}
	if err := json.Unmarshal(args, &a); err != nil {
		return nil, invalidArgs(err)
	}
	w, err := mc.workspaces.Rename(a.WorkspaceID, a.Title)
	if err != nil {
		return nil, notFound("workspace")
	}
	return map[string]any{"workspace": w}, nil
}

func (mc *machine) opWorkspaceArchive(args json.RawMessage) (any, *link.OpError) {
	var a struct {
		WorkspaceID string `json:"workspaceId"`
	}
	if err := json.Unmarshal(args, &a); err != nil {
		return nil, invalidArgs(err)
	}
	if err := mc.workspaces.Archive(a.WorkspaceID); err != nil {
		return nil, notFound("workspace")
	}
	return map[string]any{}, nil
}

func (mc *machine) opSessionList(ctx context.Context, args json.RawMessage) (any, *link.OpError) {
	var a struct {
		WorkspaceID string `json:"workspaceId,omitempty"`
	}
	if len(args) > 0 {
		if err := json.Unmarshal(args, &a); err != nil {
			return nil, invalidArgs(err)
		}
	}
	var wsList []workspaces.Workspace
	if a.WorkspaceID != "" {
		w, ok := mc.workspaces.Get(a.WorkspaceID)
		if !ok {
			return nil, notFound("workspace")
		}
		wsList = []workspaces.Workspace{w}
	} else {
		wsList = mc.workspaces.List(false)
	}
	out := []backend.Session{}
	for _, w := range wsList {
		sessList, err := mc.back.ListSessions(ctx, w.Path)
		if err != nil {
			return nil, backendErr(err)
		}
		for _, s := range sessList {
			mc.trackSession(w, s)
			// A subagent's session belongs under its parent's task call
			// (session.children), not beside it in the workspace's list.
			if s.ParentID != "" {
				continue
			}
			out = append(out, mc.enrich(s, w.ID))
		}
	}
	return map[string]any{"sessions": orEmpty(out)}, nil
}

func (mc *machine) opSessionGet(ctx context.Context, args json.RawMessage) (any, *link.OpError) {
	var a struct {
		SessionID string `json:"sessionId"`
	}
	if err := json.Unmarshal(args, &a); err != nil {
		return nil, invalidArgs(err)
	}
	dir, workspaceID, operr := mc.resolveSession(a.SessionID)
	if operr != nil {
		return nil, operr
	}
	s, err := mc.back.GetSession(ctx, dir, a.SessionID)
	if err != nil {
		return nil, backendErr(err)
	}
	return map[string]any{"session": mc.enrich(s, workspaceID)}, nil
}

func (mc *machine) opSessionCreate(ctx context.Context, args json.RawMessage) (any, *link.OpError) {
	var a struct {
		WorkspaceID string `json:"workspaceId"`
		Title       string `json:"title,omitempty"`
		ModeID      string `json:"modeId,omitempty"`
		ModelID     string `json:"modelId,omitempty"`
	}
	if err := json.Unmarshal(args, &a); err != nil {
		return nil, invalidArgs(err)
	}
	w, ok := mc.workspaces.Get(a.WorkspaceID)
	if !ok {
		return nil, notFound("workspace")
	}
	s, err := mc.back.CreateSession(ctx, w.Path, backend.CreateSessionOptions{Title: a.Title, ModeID: a.ModeID, ModelID: a.ModelID})
	if err != nil {
		return nil, backendErr(err)
	}
	mc.trackSession(w, s)
	return map[string]any{"session": mc.enrich(s, w.ID)}, nil
}

func (mc *machine) opSessionPrompt(ctx context.Context, args json.RawMessage) (any, *link.OpError) {
	var a struct {
		SessionID       string               `json:"sessionId"`
		Text            string               `json:"text"`
		ClientMessageID string               `json:"clientMessageId,omitempty"`
		Attachments     []backend.Attachment `json:"attachments,omitempty"`
	}
	if err := json.Unmarshal(args, &a); err != nil {
		return nil, invalidArgs(err)
	}
	dir, _, operr := mc.resolveSession(a.SessionID)
	if operr != nil {
		return nil, operr
	}
	err := mc.back.Prompt(ctx, dir, a.SessionID, backend.Prompt{
		Text: a.Text, ClientMessageID: a.ClientMessageID, Attachments: a.Attachments,
	})
	if err != nil {
		return nil, backendErr(err)
	}
	return map[string]any{}, nil
}

func (mc *machine) opSessionCancel(ctx context.Context, args json.RawMessage) (any, *link.OpError) {
	var a struct {
		SessionID string `json:"sessionId"`
	}
	if err := json.Unmarshal(args, &a); err != nil {
		return nil, invalidArgs(err)
	}
	dir, _, operr := mc.resolveSession(a.SessionID)
	if operr != nil {
		return nil, operr
	}
	if err := mc.back.Cancel(ctx, dir, a.SessionID); err != nil {
		return nil, backendErr(err)
	}
	return map[string]any{}, nil
}

func (mc *machine) opSessionRename(ctx context.Context, args json.RawMessage) (any, *link.OpError) {
	var a struct {
		SessionID string `json:"sessionId"`
		Title     string `json:"title"`
	}
	if err := json.Unmarshal(args, &a); err != nil {
		return nil, invalidArgs(err)
	}
	dir, workspaceID, operr := mc.resolveSession(a.SessionID)
	if operr != nil {
		return nil, operr
	}
	s, err := mc.back.RenameSession(ctx, dir, a.SessionID, a.Title)
	if err != nil {
		return nil, backendErr(err)
	}
	return map[string]any{"session": mc.enrich(s, workspaceID)}, nil
}

func (mc *machine) opSessionDelete(ctx context.Context, args json.RawMessage) (any, *link.OpError) {
	var a struct {
		SessionID string `json:"sessionId"`
	}
	if err := json.Unmarshal(args, &a); err != nil {
		return nil, invalidArgs(err)
	}
	dir, _, operr := mc.resolveSession(a.SessionID)
	if operr != nil {
		return nil, operr
	}
	if err := mc.back.DeleteSession(ctx, dir, a.SessionID); err != nil {
		return nil, backendErr(err)
	}
	return map[string]any{}, nil
}

func (mc *machine) opSessionSetMode(ctx context.Context, args json.RawMessage) (any, *link.OpError) {
	var a struct {
		SessionID string `json:"sessionId"`
		ModeID    string `json:"modeId"`
	}
	if err := json.Unmarshal(args, &a); err != nil {
		return nil, invalidArgs(err)
	}
	dir, workspaceID, operr := mc.resolveSession(a.SessionID)
	if operr != nil {
		return nil, operr
	}
	s, err := mc.back.SetMode(ctx, dir, a.SessionID, a.ModeID)
	if err != nil {
		return nil, backendErr(err)
	}
	return map[string]any{"session": mc.enrich(s, workspaceID)}, nil
}

func (mc *machine) opSessionSetModel(ctx context.Context, args json.RawMessage) (any, *link.OpError) {
	var a struct {
		SessionID string `json:"sessionId"`
		ModelID   string `json:"modelId"`
	}
	if err := json.Unmarshal(args, &a); err != nil {
		return nil, invalidArgs(err)
	}
	dir, workspaceID, operr := mc.resolveSession(a.SessionID)
	if operr != nil {
		return nil, operr
	}
	if !mc.pol.AllowFreeModels {
		filtered := mc.pol.FilterModelIDs([]string{a.ModelID})
		if len(filtered) == 0 {
			return nil, opErrf("forbidden", "model %q is not a gateway model, and this machine does not allow free models", a.ModelID)
		}
	}
	s, err := mc.back.SetModel(ctx, dir, a.SessionID, a.ModelID)
	if err != nil {
		return nil, backendErr(err)
	}
	return map[string]any{"session": mc.enrich(s, workspaceID)}, nil
}

func (mc *machine) opSessionSetAutoAccept(args json.RawMessage) (any, *link.OpError) {
	var a struct {
		SessionID string `json:"sessionId"`
		Enabled   bool   `json:"enabled"`
	}
	if err := json.Unmarshal(args, &a); err != nil {
		return nil, invalidArgs(err)
	}
	if err := mc.mat.SetAutoAccept(a.SessionID, a.Enabled); err != nil {
		if err == sessions.ErrAutoAcceptForbidden {
			return nil, opErrf("forbidden", "%v", err)
		}
		return nil, notFound("session")
	}
	_, workspaceID, operr := mc.resolveSession(a.SessionID)
	if operr != nil {
		return nil, operr
	}
	return map[string]any{"session": map[string]any{
		"id": a.SessionID, "workspaceId": workspaceID, "autoAccept": mc.mat.AutoAccept(a.SessionID),
	}}, nil
}

func (mc *machine) opSessionSync(ctx context.Context, args json.RawMessage) (any, *link.OpError) {
	var a struct {
		SessionID string `json:"sessionId"`
		Epoch     string `json:"epoch,omitempty"`
		AfterSeq  int64  `json:"afterSeq,omitempty"`
	}
	if err := json.Unmarshal(args, &a); err != nil {
		return nil, invalidArgs(err)
	}
	res, err := mc.mat.Sync(ctx, a.SessionID, a.Epoch, a.AfterSeq)
	if err != nil {
		if err == sessions.ErrUnknownSession {
			return nil, notFound("session")
		}
		return nil, backendErr(err)
	}
	out := map[string]any{"epoch": res.Epoch, "seq": res.Seq}
	if res.Snapshot != nil {
		out["snapshot"] = res.Snapshot
		return out, nil
	}
	envs := make([]map[string]any, 0, len(res.Events))
	for _, env := range res.Events {
		envs = append(envs, map[string]any{
			"sessionId": env.SessionID, "epoch": env.Epoch, "seq": env.Seq, "event": eventToWire(env.Event),
		})
	}
	out["events"] = orEmpty(envs)
	return out, nil
}

func (mc *machine) opSessionDiff(ctx context.Context, args json.RawMessage) (any, *link.OpError) {
	var a struct {
		SessionID string `json:"sessionId"`
	}
	if err := json.Unmarshal(args, &a); err != nil {
		return nil, invalidArgs(err)
	}
	dir, _, operr := mc.resolveSession(a.SessionID)
	if operr != nil {
		return nil, operr
	}
	// The checkout's uncommitted changes, from git, are what the pane shows: a
	// backend's own session diff misses files a shell command wrote. Only a
	// workspace outside git falls back to the backend's notion of a diff.
	if files, err := checkout.Diff(ctx, dir); err == nil {
		return map[string]any{"files": orEmpty(files)}, nil
	} else if !errors.Is(err, checkout.ErrNotARepo) {
		return nil, backendErr(err)
	}
	differ, ok := mc.back.(backend.Differ)
	if !ok {
		return nil, opErrf("unsupported", "backend %s has no diff capability", mc.back.ID())
	}
	files, err := differ.Diff(ctx, dir, a.SessionID)
	if err != nil {
		return nil, backendErr(err)
	}
	return map[string]any{"files": orEmpty(files)}, nil
}

func (mc *machine) opSessionChildren(ctx context.Context, args json.RawMessage) (any, *link.OpError) {
	var a struct {
		SessionID string `json:"sessionId"`
	}
	if err := json.Unmarshal(args, &a); err != nil {
		return nil, invalidArgs(err)
	}
	dir, workspaceID, operr := mc.resolveSession(a.SessionID)
	if operr != nil {
		return nil, operr
	}
	childrener, ok := mc.back.(backend.Childrener)
	if !ok {
		return nil, opErrf("unsupported", "backend %s has no children capability", mc.back.ID())
	}
	children, err := childrener.Children(ctx, dir, a.SessionID)
	if err != nil {
		return nil, backendErr(err)
	}
	// Each child is anchored at the parent's tool call that spawned it: the
	// parent's transcript records the child's session id on that call.
	spawnedBy := map[string]string{}
	if parent, err := mc.mat.Sync(ctx, a.SessionID, "", 0); err == nil && parent.Snapshot != nil {
		for _, entry := range parent.Snapshot.Messages {
			for _, part := range entry.Parts {
				if part.Type == backend.PartTool && part.SubtaskSessionID != "" {
					spawnedBy[part.SubtaskSessionID] = part.CallID
				}
			}
		}
	}
	out := make([]backend.Session, 0, len(children))
	for _, c := range children {
		c.ParentToolCallID = spawnedBy[c.ID]
		out = append(out, mc.enrich(c, workspaceID))
	}
	return map[string]any{"sessions": orEmpty(out)}, nil
}

func (mc *machine) opSessionCompact(ctx context.Context, args json.RawMessage) (any, *link.OpError) {
	var a struct {
		SessionID string `json:"sessionId"`
	}
	if err := json.Unmarshal(args, &a); err != nil {
		return nil, invalidArgs(err)
	}
	dir, _, operr := mc.resolveSession(a.SessionID)
	if operr != nil {
		return nil, operr
	}
	compactor, ok := mc.back.(backend.Compactor)
	if !ok {
		return nil, opErrf("unsupported", "backend %s has no compact capability", mc.back.ID())
	}
	if err := compactor.Compact(ctx, dir, a.SessionID); err != nil {
		return nil, backendErr(err)
	}
	return map[string]any{}, nil
}

func (mc *machine) opPermissionReply(ctx context.Context, args json.RawMessage) (any, *link.OpError) {
	var a struct {
		SessionID string `json:"sessionId"`
		RequestID string `json:"requestId"`
		Decision  string `json:"decision"`
		Message   string `json:"message,omitempty"`
	}
	if err := json.Unmarshal(args, &a); err != nil {
		return nil, invalidArgs(err)
	}
	dir, _, operr := mc.resolveSession(a.SessionID)
	if operr != nil {
		return nil, operr
	}
	if err := mc.back.ReplyPermission(ctx, dir, a.SessionID, a.RequestID, backend.Decision(a.Decision), a.Message); err != nil {
		return nil, backendErr(err)
	}
	return map[string]any{}, nil
}

func (mc *machine) opBackendModes(ctx context.Context, args json.RawMessage) (any, *link.OpError) {
	var a struct {
		WorkspaceID string `json:"workspaceId,omitempty"`
	}
	if len(args) > 0 {
		if err := json.Unmarshal(args, &a); err != nil {
			return nil, invalidArgs(err)
		}
	}
	dir := ""
	if a.WorkspaceID != "" {
		w, ok := mc.workspaces.Get(a.WorkspaceID)
		if !ok {
			return nil, notFound("workspace")
		}
		dir = w.Path
	}
	modes, err := mc.back.Modes(ctx, dir)
	if err != nil {
		return nil, backendErr(err)
	}
	return map[string]any{"modes": orEmpty(modes)}, nil
}

func (mc *machine) opBackendModels(ctx context.Context, args json.RawMessage) (any, *link.OpError) {
	var a struct {
		WorkspaceID string `json:"workspaceId,omitempty"`
	}
	if len(args) > 0 {
		if err := json.Unmarshal(args, &a); err != nil {
			return nil, invalidArgs(err)
		}
	}
	dir := ""
	if a.WorkspaceID != "" {
		w, ok := mc.workspaces.Get(a.WorkspaceID)
		if !ok {
			return nil, notFound("workspace")
		}
		dir = w.Path
	}
	models, err := mc.back.Models(ctx, dir)
	if err != nil {
		return nil, backendErr(err)
	}
	hidden := 0
	if !mc.pol.AllowFreeModels {
		hidden = len(models)
		filtered := models[:0]
		for _, m := range models {
			if m.ProviderID == policy.GatewayProviderID {
				filtered = append(filtered, m)
			}
		}
		models = filtered
		hidden -= len(models)
	}
	// hidden lets the panel say why the list is short (PROTOCOL.md §6) instead
	// of looking broken; the ids themselves never leave the machine.
	return map[string]any{"models": orEmpty(models), "hidden": hidden}, nil
}

// orEmpty turns a nil slice into an empty one. Go marshals nil as null, and
// PROTOCOL.md types every list as an array: Cerea's strict parsing either
// rejects a null or crashes on it, as the first end-to-end runs showed.
func orEmpty[T any](s []T) []T {
	if s == nil {
		return []T{}
	}
	return s
}
