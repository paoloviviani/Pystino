// Package workspaces is the agent's own registry of workspaces: a
// workspace is a directory the operator (or the browser, via
// workspace.create) has pointed the agent at, recorded in one JSON file
// so it survives a restart. Path confinement (PROTOCOL.md §4's
// workspaceRoots) is enforced here, against the policy passed to Create —
// this package owns the enforcement, internal/policy owns the rule.
package workspaces

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"

	"pystino-agent/internal/fsutil"
	"pystino-agent/internal/policy"
)

// Workspace is one registry entry (PROTOCOL.md §6 Types). WorktreeOf and
// Branch are set only on a workspace CreateWorktree registered; IsGitRepo
// is computed at Create time for every workspace, since it gates the
// panel's "New worktree…" action regardless of how the workspace was made.
type Workspace struct {
	ID         string    `json:"id"`
	Name       string    `json:"name"`
	Path       string    `json:"path"`
	CreatedAt  time.Time `json:"createdAt"`
	Archived   bool      `json:"archived"`
	WorktreeOf string    `json:"worktreeOf,omitempty"`
	Branch     string    `json:"branch,omitempty"`
	IsGitRepo  bool      `json:"isGitRepo"`
}

// Registry is the loaded, mutable workspace list, backed by one file.
// Every mutating method persists before returning, so a crash right after a
// call never loses that call's effect silently.
type Registry struct {
	mu   sync.Mutex
	path string
	byID map[string]*Workspace
}

// Load reads path, or starts an empty registry when it does not exist yet
// (a machine's first run).
func Load(path string) (*Registry, error) {
	r := &Registry{path: path, byID: map[string]*Workspace{}}
	body, err := fsutil.ReadFileOrEmpty(path)
	if err != nil {
		return nil, err
	}
	if body == nil {
		return r, nil
	}
	var list []*Workspace
	if err := json.Unmarshal(body, &list); err != nil {
		return nil, fmt.Errorf("parsing %s: %w", path, err)
	}
	for _, w := range list {
		r.byID[w.ID] = w
	}
	return r, nil
}

// saveLocked persists the registry atomically (R8). Caller holds r.mu.
func (r *Registry) saveLocked() error {
	list := make([]*Workspace, 0, len(r.byID))
	for _, w := range r.byID {
		list = append(list, w)
	}
	sort.Slice(list, func(i, j int) bool { return list[i].CreatedAt.Before(list[j].CreatedAt) })
	body, err := json.MarshalIndent(list, "", "  ")
	if err != nil {
		return err
	}
	return fsutil.WriteFileAtomic(r.path, append(body, '\n'), 0o600)
}

// List returns every workspace, sorted by CreatedAt, optionally including
// archived ones (session.list and workspace.list both exclude archived by
// default per PROTOCOL.md §6).
func (r *Registry) List(includeArchived bool) []Workspace {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]Workspace, 0, len(r.byID))
	for _, w := range r.byID {
		if w.Archived && !includeArchived {
			continue
		}
		out = append(out, *w)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].CreatedAt.Before(out[j].CreatedAt) })
	return out
}

// Get returns one workspace by id.
func (r *Registry) Get(id string) (Workspace, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	w, ok := r.byID[id]
	if !ok {
		return Workspace{}, false
	}
	return *w, true
}

// Create validates path (must resolve, must be a directory, must satisfy
// roots) and adds a new entry. roots is the policy's current
// WorkspaceRoots, passed in rather than read from a stored Policy so the
// caller (which already loaded the policy once for the process) is the
// single source of truth for it.
func (r *Registry) Create(name, path string, roots []string) (Workspace, error) {
	return r.create(name, path, roots, "", "")
}

// create is Create plus the worktree bookkeeping CreateWorktree needs; the
// two share every validation step (resolve, must-be-a-dir, workspaceRoots)
// since a worktree's directory is just as much a workspace as any other.
func (r *Registry) create(name, path string, roots []string, worktreeOf, branch string) (Workspace, error) {
	resolved, err := policy.ResolvePath(path)
	if err != nil {
		return Workspace{}, fmt.Errorf("workspace path %q: %w", path, err)
	}
	info, err := os.Stat(resolved)
	if err != nil {
		return Workspace{}, fmt.Errorf("workspace path %q: %w", path, err)
	}
	if !info.IsDir() {
		return Workspace{}, fmt.Errorf("workspace path %q is not a directory", path)
	}
	allowed, err := policy.Allowed(roots, resolved)
	if err != nil {
		return Workspace{}, err
	}
	if !allowed {
		return Workspace{}, fmt.Errorf("workspace path %q is outside every configured workspaceRoot", path)
	}

	id, err := randomID()
	if err != nil {
		return Workspace{}, err
	}
	w := &Workspace{
		ID:         id,
		Name:       name,
		Path:       resolved,
		CreatedAt:  time.Now().UTC(),
		WorktreeOf: worktreeOf,
		Branch:     branch,
		IsGitRepo:  isGitRepo(resolved),
	}

	r.mu.Lock()
	defer r.mu.Unlock()
	r.byID[id] = w
	if err := r.saveLocked(); err != nil {
		delete(r.byID, id)
		return Workspace{}, err
	}
	return *w, nil
}

// isGitRepo is a cheap heuristic (a stat, no subprocess) good enough to
// gate the "New worktree…" affordance: a plain repo has ".git" as a
// directory, a worktree or submodule has it as a file pointing elsewhere.
func isGitRepo(path string) bool {
	_, err := os.Stat(filepath.Join(path, ".git"))
	return err == nil
}

// Rename updates title in place.
func (r *Registry) Rename(id, title string) (Workspace, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	w, ok := r.byID[id]
	if !ok {
		return Workspace{}, fmt.Errorf("no such workspace: %s", id)
	}
	previous := w.Name
	w.Name = title
	if err := r.saveLocked(); err != nil {
		w.Name = previous
		return Workspace{}, err
	}
	return *w, nil
}

// Archive marks a workspace archived; it stays in the registry (so its
// sessions remain reachable by id) but drops out of List's default view.
func (r *Registry) Archive(id string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	w, ok := r.byID[id]
	if !ok {
		return fmt.Errorf("no such workspace: %s", id)
	}
	if w.Archived {
		return nil
	}
	w.Archived = true
	if err := r.saveLocked(); err != nil {
		w.Archived = false
		return err
	}
	return nil
}

func randomID() (string, error) {
	raw := make([]byte, 16)
	if _, err := rand.Read(raw); err != nil {
		return "", fmt.Errorf("minting workspace id: %w", err)
	}
	return hex.EncodeToString(raw), nil
}
