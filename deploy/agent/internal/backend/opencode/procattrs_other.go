//go:build !linux

package opencode

import (
	"os/exec"
	"syscall"
)

// setProcAttrs has no non-Linux implementation yet: this agent currently
// only ships for Linux. A no-op keeps a future build on another OS
// compiling rather than failing outright; it just loses the
// Pdeathsig/process-group guarantee procattrs_linux.go provides.
func setProcAttrs(cmd *exec.Cmd) {}

// signalGroup falls back to signalling the process itself.
func signalGroup(cmd *exec.Cmd, sig syscall.Signal) {
	if cmd.Process == nil {
		return
	}
	_ = cmd.Process.Signal(sig)
}

func signalGroupTerm(cmd *exec.Cmd) { signalGroup(cmd, syscall.SIGTERM) }
func signalGroupKill(cmd *exec.Cmd) { signalGroup(cmd, syscall.SIGKILL) }
