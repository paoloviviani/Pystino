// Package policy holds the machine's own veto (PROTOCOL.md §4 "C4"): a
// policy.json set once by `enroll` flags, loaded by `run`, and never
// writable over the link. Cerea can ask for what the policy allows; it can
// never change the policy itself.
package policy

import (
	"encoding/json"
	"fmt"
	"path/filepath"
	"strings"

	"pystino-agent/internal/fsutil"
)

// AutoAccept is a string, not a bool, because the wire (PROTOCOL.md §5
// Policy) uses the same two words and a third value ("ask", say) is a
// plausible future addition that a bool could not carry without a breaking
// change.
type AutoAccept string

const (
	AutoAcceptAllowed AutoAccept = "allowed"
	AutoAcceptDenied  AutoAccept = "denied"
)

// Policy is the whole file (PROTOCOL.md §5). Defaults are all the safe
// answer: no auto-accept, no workspace outside what's explicitly listed
// once any root is configured, no models beyond the gateway's own.
type Policy struct {
	AutoAccept      AutoAccept `json:"autoAccept"`
	WorkspaceRoots  []string   `json:"workspaceRoots"`
	AllowFreeModels bool       `json:"allowFreeModels"`
}

// GatewayProviderID is the provider id `enroll` writes into opencode.json
// (store.go's buildOpencodeConfig). When AllowFreeModels is false, this is
// the only provider whose models are ever listed or accepted — see
// FilterModelIDs.
const GatewayProviderID = "pystino"

// Default is what a machine with no policy.json at all gets: everything
// closed. `enroll`'s flags are what opens any of it.
func Default() Policy {
	return Policy{AutoAccept: AutoAcceptDenied, AllowFreeModels: false}
}

// Load reads policy.json, or returns Default() when the file does not
// exist — a machine that has never run `enroll` with any policy flag has
// the safe defaults, not a missing-file error.
func Load(path string) (Policy, error) {
	body, err := fsutil.ReadFileOrEmpty(path)
	if err != nil {
		return Policy{}, err
	}
	if body == nil {
		return Default(), nil
	}
	var p Policy
	if err := json.Unmarshal(body, &p); err != nil {
		return Policy{}, fmt.Errorf("parsing %s: %w", path, err)
	}
	if p.AutoAccept == "" {
		p.AutoAccept = AutoAcceptDenied
	}
	return p, nil
}

// Save persists policy.json atomically at 0600 (R8, via fsutil).
func Save(path string, p Policy) error {
	body, err := json.MarshalIndent(p, "", "  ")
	if err != nil {
		return err
	}
	return fsutil.WriteFileAtomic(path, append(body, '\n'), 0o600)
}

// AutoAcceptAllowed reports whether the machine permits any session to
// turn on auto-accept at all. A session flag on top of this (internal
// /sessions) is what actually turns replies automatic for one session; this
// is only ever a veto, never an override in the other direction.
func (p Policy) AutoAcceptAllowed() bool {
	return p.AutoAccept == AutoAcceptAllowed
}

// AllowsWorkspace reports whether path is permitted by p.WorkspaceRoots. No
// roots configured means unrestricted (the enroll-time default: an operator
// who never passed --workspace-root hasn't opted into confinement).
func (p Policy) AllowsWorkspace(path string) (bool, error) {
	return Allowed(p.WorkspaceRoots, path)
}

// Allowed is the pure, testable check behind AllowsWorkspace: path is
// permitted when roots is empty, or when path resolves (Clean +
// EvalSymlinks) to one of roots or a descendant of one. Resolving both
// sides defeats a workspace path that is a symlink pointing outside every
// configured root — a bare prefix check on the unresolved strings would
// miss that.
func Allowed(roots []string, path string) (bool, error) {
	if len(roots) == 0 {
		return true, nil
	}
	resolved, err := ResolvePath(path)
	if err != nil {
		return false, err
	}
	for _, root := range roots {
		resolvedRoot, err := ResolvePath(root)
		if err != nil {
			// An unreadable or since-deleted root can't match anything; it
			// simply contributes no permission, rather than failing every
			// check that happens to run after it.
			continue
		}
		if resolved == resolvedRoot || strings.HasPrefix(resolved, resolvedRoot+string(filepath.Separator)) {
			return true, nil
		}
	}
	return false, nil
}

// ResolvePath makes path absolute, cleans it, and resolves symlinks — the
// canonical form every workspace-root comparison in this package is done
// against.
func ResolvePath(path string) (string, error) {
	abs, err := filepath.Abs(path)
	if err != nil {
		return "", fmt.Errorf("resolving %s: %w", path, err)
	}
	resolved, err := filepath.EvalSymlinks(abs)
	if err != nil {
		return "", fmt.Errorf("resolving %s: %w", path, err)
	}
	return filepath.Clean(resolved), nil
}

// FilterModelIDs applies AllowFreeModels to a list of "<providerId>/<model>"
// ids: unrestricted when true, else only ids under GatewayProviderID.
// Kept provider-agnostic (ids, not backend.Model) so this package never
// needs to import internal/backend.
func (p Policy) FilterModelIDs(ids []string) []string {
	if p.AllowFreeModels {
		return ids
	}
	prefix := GatewayProviderID + "/"
	out := make([]string, 0, len(ids))
	for _, id := range ids {
		if strings.HasPrefix(id, prefix) {
			out = append(out, id)
		}
	}
	return out
}
