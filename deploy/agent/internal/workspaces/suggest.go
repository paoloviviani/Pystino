package workspaces

import (
	"os"
	"path/filepath"
	"strings"

	"pystino-agent/internal/policy"
)

// Directory is one entry in workspace.suggest's answer (PROTOCOL.md §6):
// directory autocomplete for the "Add workspace" dialog.
type Directory struct {
	Path      string `json:"path"`
	Name      string `json:"name"`
	IsGitRepo bool   `json:"isGitRepo"`
}

// maxSuggestions caps how many subdirectories Suggest returns, so a huge
// directory never turns one keystroke into a huge frame.
const maxSuggestions = 20

// Suggest lists at most maxSuggestions subdirectories of prefix's parent
// directory whose name starts with prefix's own basename (a trailing "/",
// or an empty prefix, means "everything in that directory"). roots
// confines the result exactly as workspace.create does, except an empty
// roots list here means "anything under $HOME" rather than "unrestricted":
// this populates a dropdown as someone types, and a machine with no
// configured roots still should not use it to browse the whole filesystem.
// Symlinks are followed one level to decide directory-ness and to resolve
// the confinement check, but never into /proc or /sys.
func Suggest(prefix string, roots []string) ([]Directory, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return nil, err
	}

	trailingSlash := prefix == "" || prefix == "~" || strings.HasSuffix(prefix, "/")
	expanded := expandHome(prefix, home)
	if expanded == "" {
		expanded = home
	}
	abs, err := filepath.Abs(expanded)
	if err != nil {
		return nil, err
	}

	var parent, base string
	if trailingSlash {
		parent, base = abs, ""
	} else {
		parent, base = filepath.Dir(abs), filepath.Base(abs)
	}
	if underProcOrSys(parent) {
		return []Directory{}, nil
	}

	effectiveRoots := roots
	if len(effectiveRoots) == 0 {
		effectiveRoots = []string{home}
	}
	resolvedRoots := make([]string, 0, len(effectiveRoots))
	for _, root := range effectiveRoots {
		resolved, err := policy.ResolvePath(root)
		if err != nil {
			continue // an unreadable or since-deleted root just contributes no permission
		}
		resolvedRoots = append(resolvedRoots, resolved)
	}

	entries, err := os.ReadDir(parent)
	if err != nil {
		return []Directory{}, nil // mid-typed or unreadable path: no suggestions, not an error
	}

	showHidden := strings.HasPrefix(base, ".")
	out := []Directory{}
	for _, e := range entries {
		name := e.Name()
		if base != "" && !strings.HasPrefix(name, base) {
			continue
		}
		if !showHidden && strings.HasPrefix(name, ".") {
			continue
		}
		full := filepath.Join(parent, name)
		if !isDirEntry(e, full) {
			continue
		}
		resolved, err := policy.ResolvePath(full)
		if err != nil || underProcOrSys(resolved) {
			continue
		}
		if !withinAny(resolvedRoots, resolved) {
			continue
		}
		out = append(out, Directory{Path: full, Name: name, IsGitRepo: isGitRepo(full)})
		if len(out) == maxSuggestions {
			break
		}
	}
	return out, nil
}

// expandHome resolves a leading "~" or "~/" against home; string
// concatenation (not filepath.Join) so a trailing slash in prefix survives
// for the trailingSlash check above.
func expandHome(prefix, home string) string {
	if prefix == "~" {
		return home
	}
	if strings.HasPrefix(prefix, "~/") {
		return home + "/" + prefix[2:]
	}
	return prefix
}

func underProcOrSys(path string) bool {
	return path == "/proc" || strings.HasPrefix(path, "/proc/") ||
		path == "/sys" || strings.HasPrefix(path, "/sys/")
}

func withinAny(roots []string, path string) bool {
	for _, root := range roots {
		if path == root || strings.HasPrefix(path, root+string(filepath.Separator)) {
			return true
		}
	}
	return false
}

// isDirEntry follows a symlink entry one level to decide directory-ness;
// os.DirEntry.IsDir reports the symlink itself as not-a-dir otherwise.
func isDirEntry(e os.DirEntry, full string) bool {
	if e.Type()&os.ModeSymlink != 0 {
		info, err := os.Stat(full)
		return err == nil && info.IsDir()
	}
	return e.IsDir()
}
