package main

import (
	"os"

	"pystino-agent/internal/fsutil"
)

// writeFileAtomic is the package-main name for fsutil.WriteFileAtomic (R8),
// kept so store.go/health.go read the same as before internal/fsutil
// existed. internal/policy, internal/workspaces and internal/sessions call
// fsutil directly.
func writeFileAtomic(path string, body []byte, perm os.FileMode) error {
	return fsutil.WriteFileAtomic(path, body, perm)
}
