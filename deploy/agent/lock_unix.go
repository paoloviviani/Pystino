//go:build unix

package main

import (
	"fmt"
	"os"
	"syscall"
)

// lockCredsFile takes an exclusive advisory lock on path+".lock" (not the
// credentials file itself, so a lock attempt never races the atomic
// rename in writeFileAtomic) and blocks until it is held. The returned
// func releases it. This is R7's cross-process half: two processes on the
// same machine (serve and run, or two run instances) that both refresh the
// credential must not both present the same refresh token to the IdP — one
// would get invalid_grant on a token the other had already rotated away.
func lockCredsFile(path string) (unlock func(), err error) {
	lockPath := path + ".lock"
	f, err := os.OpenFile(lockPath, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, fmt.Errorf("opening lock file %s: %w", lockPath, err)
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX); err != nil {
		_ = f.Close()
		return nil, fmt.Errorf("locking %s: %w", lockPath, err)
	}
	return func() {
		_ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
		_ = f.Close()
	}, nil
}
