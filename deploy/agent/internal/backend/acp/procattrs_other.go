//go:build !linux

package acp

import (
	"os/exec"
	"syscall"
)

// setProcAttrs has no non-Linux implementation yet, matching
// internal/backend/opencode's procattrs_other.go: a no-op keeps a future
// build on another OS compiling, just without the Pdeathsig/process-group
// guarantee.
func setProcAttrs(cmd *exec.Cmd) {}

func signalGroup(cmd *exec.Cmd, sig syscall.Signal) {
	if cmd.Process == nil {
		return
	}
	_ = cmd.Process.Signal(sig)
}

func signalGroupTerm(cmd *exec.Cmd) { signalGroup(cmd, syscall.SIGTERM) }
func signalGroupKill(cmd *exec.Cmd) { signalGroup(cmd, syscall.SIGKILL) }
