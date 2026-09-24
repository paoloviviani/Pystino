// Package checkout computes a workspace's uncommitted changes with git itself.
//
// A diff pane answers "what is different on disk", which is a property of the
// workspace, not of whichever coding agent produced it: opencode's per-session
// diff only tracks edits made through its own edit tools, so a file written by
// a shell command never appeared. Asking git keeps the pane backend-agnostic.
package checkout

import (
	"bytes"
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"pystino-agent/internal/backend"
)

// Limits keep one pathological checkout (a build dir, a binary blob) from
// turning a diff request into megabytes on the wire.
const (
	MaxFiles     = 200
	MaxFileBytes = 256 << 10
)

// ErrNotARepo means the workspace is not inside a git work tree; callers fall
// back to the backend's own notion of a diff.
var ErrNotARepo = errors.New("not a git work tree")

// Diff lists dir's uncommitted changes against HEAD, untracked files included.
func Diff(ctx context.Context, dir string) ([]backend.FileDiff, error) {
	if out, err := git(ctx, dir, "rev-parse", "--is-inside-work-tree"); err != nil || strings.TrimSpace(out) != "true" {
		return nil, ErrNotARepo
	}
	hasHead := true
	if _, err := git(ctx, dir, "rev-parse", "--verify", "-q", "HEAD"); err != nil {
		hasHead = false // a fresh repo with no commit: everything is added
	}
	status, err := git(ctx, dir, "status", "--porcelain=v1", "-z", "--untracked-files=all")
	if err != nil {
		return nil, err
	}
	files := []backend.FileDiff{}
	entries := strings.Split(status, "\x00")
	for i := 0; i < len(entries) && len(files) < MaxFiles; i++ {
		entry := entries[i]
		if len(entry) < 4 {
			continue
		}
		code, path := entry[:2], entry[3:]
		if code[0] == 'R' || code[0] == 'C' {
			i++ // porcelain -z puts a rename's source in the next entry
		}
		fd := backend.FileDiff{Path: path, Status: backend.FileModified}
		switch {
		case code == "??" || code[0] == 'A' || !hasHead:
			fd.Status = backend.FileAdded
		case code[0] == 'D' || code[1] == 'D':
			fd.Status = backend.FileDeleted
		}
		if fd.Status != backend.FileAdded {
			fd.Before, _ = git(ctx, dir, "show", "HEAD:"+path)
		}
		if fd.Status != backend.FileDeleted {
			fd.After = readCapped(filepath.Join(dir, path))
		}
		fd.Before = capText(fd.Before)
		fd.Additions, fd.Deletions = lineDelta(fd.Before, fd.After)
		files = append(files, fd)
	}
	return files, nil
}

func git(ctx context.Context, dir string, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, "git", append([]string{"-C", dir}, args...)...)
	var out bytes.Buffer
	cmd.Stdout = &out
	err := cmd.Run()
	return out.String(), err
}

func readCapped(path string) string {
	f, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer f.Close()
	buf := make([]byte, MaxFileBytes+1)
	n, _ := f.Read(buf)
	return capText(string(buf[:n]))
}

// capText truncates oversized or binary content to a marker the pane can show.
func capText(s string) string {
	if strings.IndexByte(s, 0) >= 0 {
		return "(binary file)"
	}
	if len(s) > MaxFileBytes {
		return s[:MaxFileBytes] + "\n… (truncated)"
	}
	return s
}

// lineDelta is a cheap line-count estimate (not an LCS diff): enough for the
// +N/−M badge, while the pane itself aligns before and after.
func lineDelta(before, after string) (int, int) {
	b, a := lines(before), lines(after)
	seen := map[string]int{}
	for _, l := range b {
		seen[l]++
	}
	add := 0
	for _, l := range a {
		if seen[l] > 0 {
			seen[l]--
		} else {
			add++
		}
	}
	del := 0
	for _, n := range seen {
		del += n
	}
	return add, del
}

func lines(s string) []string {
	if s == "" {
		return nil
	}
	return strings.Split(strings.TrimSuffix(s, "\n"), "\n")
}
