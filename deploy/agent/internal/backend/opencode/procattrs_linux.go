//go:build linux

package opencode

import (
	"os/exec"
	"syscall"
)

// setProcAttrs puts the spawned opencode in its own process group and asks
// the kernel to SIGKILL it the instant this agent process dies for any
// reason, SIGKILL included (Pdeathsig fires on the thread group leader's
// exit, which covers a crash the deferred Stop() call never runs for).
// Setpgid is what lets Stop signal the whole group below, so a bash tool
// opencode spawned does not outlive it either.
func setProcAttrs(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{
		Setpgid:   true,
		Pdeathsig: syscall.SIGKILL,
	}
}

// signalGroup signals the process group opencode leads (negative pid),
// reaching any subprocess it spawned along with it.
func signalGroup(cmd *exec.Cmd, sig syscall.Signal) {
	if cmd.Process == nil {
		return
	}
	_ = syscall.Kill(-cmd.Process.Pid, sig)
}

func signalGroupTerm(cmd *exec.Cmd) { signalGroup(cmd, syscall.SIGTERM) }
func signalGroupKill(cmd *exec.Cmd) { signalGroup(cmd, syscall.SIGKILL) }
