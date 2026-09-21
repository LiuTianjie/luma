//go:build linux || darwin

package process

import (
	"context"
	"errors"
	"io"
	"os"
	"os/exec"
	"syscall"
	"time"
)

const SupervisorArgument = "__luma_process_supervisor"

// Supervise is an internal executable mode, never exposed through task payloads.
// FD 3 is a private parent-liveness pipe. It is not forwarded to the tool, so
// killing the parent always produces EOF even if the tool forks descendants.
func Supervise(args []string) int {
	if len(args) == 0 || args[0] == "" {
		return 127
	}
	liveness := os.NewFile(3, "agent-liveness")
	if liveness == nil {
		return 127
	}
	info, err := liveness.Stat()
	if err != nil || info.Mode()&os.ModeNamedPipe == 0 {
		return 127
	}
	defer liveness.Close()
	syscall.CloseOnExec(3)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _, _ = io.Copy(io.Discard, liveness); cancel() }()
	cmd := exec.CommandContext(ctx, args[0], args[1:]...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	cmd.Cancel = func() error { return killGroup(cmd) }
	cmd.WaitDelay = time.Second
	if err := cmd.Start(); err != nil {
		return 127
	}
	err = cmd.Wait()
	_ = killGroup(cmd)
	if err == nil {
		return 0
	}
	var exited *exec.ExitError
	if errors.As(err, &exited) && exited.ExitCode() >= 0 {
		return exited.ExitCode()
	}
	return 127
}
