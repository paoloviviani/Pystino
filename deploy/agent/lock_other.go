//go:build !unix

package main

// lockCredsFile has no non-unix implementation yet: this agent currently
// only ships for unix targets. A no-op keeps a future Windows build
// compiling rather than failing outright; it just loses the cross-process
// guarantee lock_unix.go provides.
func lockCredsFile(path string) (unlock func(), err error) {
	return func() {}, nil
}
