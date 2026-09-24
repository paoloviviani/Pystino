package workspaces

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"path/filepath"
	"strings"
)

// CreateWorktree runs `git worktree add` for the repository behind from
// and registers the resulting directory as a new workspace carrying
// {worktreeOf: from.ID, branch} (PROTOCOL.md §6). The destination sits
// beside the repository itself — <repo parent>/<repo name>.worktrees/
// <branch> — computed from git's own idea of the repo's top level, so it
// lands there regardless of whether from.Path is the repo root or a
// subdirectory of it. Using the git CLI directly (rather than opencode's
// experimental /experimental/worktree) keeps this backend-agnostic.
func (r *Registry) CreateWorktree(ctx context.Context, from Workspace, branch, base string, roots []string) (Workspace, error) {
	sanitized, err := sanitizeBranchPath(branch)
	if err != nil {
		return Workspace{}, err
	}

	repoRoot, err := gitOutput(ctx, from.Path, "rev-parse", "--show-toplevel")
	if err != nil {
		return Workspace{}, fmt.Errorf("workspace %q is not a git repository: %w", from.Name, err)
	}
	repoRoot = strings.TrimSpace(repoRoot)
	worktreesDir := repoRoot + ".worktrees"
	dest := filepath.Join(worktreesDir, sanitized)
	if !strings.HasPrefix(dest+string(filepath.Separator), filepath.Clean(worktreesDir)+string(filepath.Separator)) {
		return Workspace{}, fmt.Errorf("branch name %q escapes the worktrees directory", branch)
	}

	ref := base
	if ref == "" {
		ref = "HEAD"
	}
	if _, err := gitOutput(ctx, from.Path, "worktree", "add", "-b", sanitized, dest, ref); err != nil {
		return Workspace{}, fmt.Errorf("git worktree add: %w", err)
	}

	w, err := r.create(sanitized, dest, roots, from.ID, sanitized)
	if err != nil {
		// The registry refused the resulting path (e.g. outside
		// workspaceRoots): leave no orphaned worktree behind.
		_, _ = gitOutput(ctx, from.Path, "worktree", "remove", "--force", dest)
		return Workspace{}, err
	}
	return w, nil
}

// RemoveWorktree runs `git worktree remove` from repoPath — the source
// workspace's own directory, since worktree metadata lives in the main
// repo's .git and that's what git needs to operate on it. Without force,
// git itself refuses when the worktree has modified or untracked files;
// that refusal is exactly the "unless force" PROTOCOL.md §6 asks for, so
// there is no separate dirty check here.
func RemoveWorktree(ctx context.Context, repoPath, worktreePath string, force bool) error {
	args := []string{"worktree", "remove"}
	if force {
		args = append(args, "--force")
	}
	args = append(args, worktreePath)
	if _, err := gitOutput(ctx, repoPath, args...); err != nil {
		return fmt.Errorf("git worktree remove: %w", err)
	}
	return nil
}

func gitOutput(ctx context.Context, dir string, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, "git", append([]string{"-C", dir}, args...)...)
	var out, errBuf bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &errBuf
	if err := cmd.Run(); err != nil {
		msg := strings.TrimSpace(errBuf.String())
		if msg == "" {
			msg = err.Error()
		}
		return "", fmt.Errorf("%s", msg)
	}
	return out.String(), nil
}

// sanitizeBranchPath validates branch is safe to use as a path component —
// it becomes the worktree's directory name, possibly nested when it
// contains "/", which git branch names allow. ".." anywhere is refused
// outright (PROTOCOL.md §6): git would happily create such a ref while the
// resulting path climbed out of <repo>.worktrees.
func sanitizeBranchPath(branch string) (string, error) {
	if branch == "" {
		return "", fmt.Errorf("branch name must not be empty")
	}
	if strings.ContainsRune(branch, 0) {
		return "", fmt.Errorf("branch name %q contains a null byte", branch)
	}
	if strings.Contains(branch, "..") {
		return "", fmt.Errorf("branch name %q must not contain \"..\"", branch)
	}
	if strings.HasPrefix(branch, "/") || strings.HasSuffix(branch, "/") {
		return "", fmt.Errorf("branch name %q must not start or end with \"/\"", branch)
	}
	return branch, nil
}
