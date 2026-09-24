// Package fsutil holds the one file-writing rule every state file in this
// program follows (R8): never truncate in place. A crash, a full disk or a
// concurrent writer must never leave a reader looking at a half-written
// credential, policy, registry or session-state file.
package fsutil

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

// WriteFileAtomic writes body to path: the data lands in a temp file in the
// same directory first, is fsynced to disk, then renamed into place — POSIX
// guarantees rename is atomic within one filesystem, so a reader never
// observes a partial write. perm is applied to the temp file before the
// rename, so the final file never has a window at the wrong mode either.
func WriteFileAtomic(path string, body []byte, perm os.FileMode) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return fmt.Errorf("creating %s: %w", dir, err)
	}
	tmp, err := os.CreateTemp(dir, "."+filepath.Base(path)+"-*.tmp")
	if err != nil {
		return fmt.Errorf("writing %s: %w", path, err)
	}
	tmpPath := tmp.Name()
	// Best-effort cleanup: once the rename below succeeds tmpPath no longer
	// exists, so this Remove is only reached on an error path.
	defer os.Remove(tmpPath)

	if _, err := tmp.Write(body); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("writing %s: %w", path, err)
	}
	if err := tmp.Chmod(perm); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("writing %s: %w", path, err)
	}
	// fsync before rename: without it, a crash can drop the write while
	// leaving a zero-length (or short) file at tmpPath, and on some
	// filesystems the rename itself is not durable until the data is.
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("writing %s: %w", path, err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("writing %s: %w", path, err)
	}
	if err := os.Rename(tmpPath, path); err != nil {
		return fmt.Errorf("writing %s: %w", path, err)
	}
	return nil
}

// ReadFileOrEmpty reads path, returning (nil, nil) when it does not exist —
// the shape every "load state, or start from the default if this is the
// first run" caller in this program wants, without each one repeating the
// os.IsNotExist check.
func ReadFileOrEmpty(path string) ([]byte, error) {
	body, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, fmt.Errorf("reading %s: %w", path, err)
	}
	return body, nil
}
