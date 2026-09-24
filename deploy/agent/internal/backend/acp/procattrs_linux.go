//go:build linux

package acp

import (
	"os/exec"
	"syscall"
)

// setProcAttrs puts the spawned agent in its own process group and asks the
// kernel to SIGKILL it the instant this agent process dies for any reason
// (Pdeathsig fires on the thread group leader's exit, which covers a crash
// the deferred Stop() call never runs for) — identical reasoning to
// internal/backend/opencode's procattrs_linux.go.
func setProcAttrs(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{
		Setpgid:   true,
		Pdeathsig: syscall.SIGKILL,
	}
}

func signalGroup(cmd *exec.Cmd, sig syscall.Signal) {
	if cmd.Process == nil {
		return
	}
	_ = syscall.Kill(-cmd.Process.Pid, sig)
}

func signalGroupTerm(cmd *exec.Cmd) { signalGroup(cmd, syscall.SIGTERM) }
func signalGroupKill(cmd *exec.Cmd) { signalGroup(cmd, syscall.SIGKILL) }
