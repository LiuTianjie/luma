//go:build linux || darwin

// Package process supervises bounded-output child process groups. It never
// interprets a lease payload or constructs shell commands from task input.
package process

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/LiuTianjie/luma/internal/node"
)

const outputLimit = 256 << 10

type Runner struct {
	// Supervisor is this agent executable. Its private helper watches a pipe
	// owned by the parent, so even SIGKILL of the agent cancels host children.
	Supervisor string
}

func (r Runner) Run(ctx context.Context, command node.Command, emit func(string)) (node.CommandResult, error) {
	if len(command.Args) == 0 || command.Args[0] == "" {
		return node.CommandResult{}, fmt.Errorf("process command is empty")
	}
	if command.Timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, command.Timeout)
		defer cancel()
	}
	args := command.Args
	var lifeRead, lifeWrite *os.File
	if r.Supervisor != "" {
		var err error
		lifeRead, lifeWrite, err = os.Pipe()
		if err != nil {
			return node.CommandResult{}, fmt.Errorf("create process liveness pipe: %w", err)
		}
		defer lifeRead.Close()
		defer lifeWrite.Close()
		args = append([]string{r.Supervisor, SupervisorArgument}, command.Args...)
	}
	cmd := exec.CommandContext(ctx, args[0], args[1:]...)
	if lifeRead != nil {
		cmd.ExtraFiles = []*os.File{lifeRead}
	}
	cmd.Dir = command.Dir
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if command.Env != nil || command.ClearEnv {
		env := map[string]string{}
		if !command.ClearEnv {
			for _, entry := range os.Environ() {
				key, value, _ := strings.Cut(entry, "=")
				env[key] = value
			}
		}
		cmd.Env = []string{}
		for key, value := range command.Env {
			if strings.ContainsAny(key, "=\x00") || key == "" {
				return node.CommandResult{}, fmt.Errorf("invalid command environment name")
			}
			env[key] = value
		}
		keys := make([]string, 0, len(env))
		for key := range env {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		for _, key := range keys {
			cmd.Env = append(cmd.Env, key+"="+env[key])
		}
	}
	output := &boundedOutput{emit: emit, secrets: command.Secrets}
	cmd.Stdout = output
	cmd.Stderr = output
	cmd.Cancel = func() error { return killGroup(cmd) }
	if lifeWrite != nil {
		// Closing the pipe asks the supervisor to kill and reap its tool group.
		// WaitDelay still bounds a malfunctioning helper during normal shutdown.
		cmd.Cancel = func() error { return lifeWrite.Close() }
	}
	// Descendants retaining stdout cannot hold Wait forever after their parent exits.
	cmd.WaitDelay = 2 * time.Second
	if err := cmd.Start(); err != nil {
		return node.CommandResult{}, fmt.Errorf("start host process: %w", err)
	}
	if lifeRead != nil {
		_ = lifeRead.Close()
	}
	err := cmd.Wait()
	_ = killGroup(cmd)
	output.flush()
	result := node.CommandResult{Output: output.text()}
	if ctx.Err() != nil {
		return result, ctx.Err()
	}
	var exited *exec.ExitError
	if errors.As(err, &exited) {
		result.Code = exited.ExitCode()
		return result, nil
	}
	if err != nil {
		return result, fmt.Errorf("host process supervision failed: %w", err)
	}
	return result, nil
}
func killGroup(cmd *exec.Cmd) error {
	if cmd.Process == nil {
		return os.ErrProcessDone
	}
	err := syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	if errors.Is(err, syscall.ESRCH) {
		return os.ErrProcessDone
	}
	return err
}

type boundedOutput struct {
	mu         sync.Mutex
	data, line []byte
	pending    []byte
	emit       func(string)
	secrets    []string
}

func (w *boundedOutput) Write(data []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.pending = append(w.pending, data...)
	w.drain(false)
	return len(data), nil
}

// Redact before truncation or line splitting, retaining enough lookahead for a
// secret split across process writes. Neither output tail nor progress leaks it.
func (w *boundedOutput) drain(final bool) {
	lookahead := 1
	for _, secret := range w.secrets {
		lookahead = max(lookahead, len(secret))
	}
	if len(w.secrets) == 0 {
		w.accept(w.pending)
		w.pending = nil
		return
	}
	var clean []byte
	for len(w.pending) >= lookahead || (final && len(w.pending) > 0) {
		matched := 0
		for _, secret := range w.secrets {
			if len(secret) > matched && bytes.HasPrefix(w.pending, []byte(secret)) {
				matched = len(secret)
			}
		}
		if matched > 0 {
			clean = append(clean, "[REDACTED]"...)
			w.pending = w.pending[matched:]
		} else {
			clean = append(clean, w.pending[0])
			w.pending = w.pending[1:]
		}
	}
	w.accept(clean)
	w.pending = append([]byte(nil), w.pending...)
}
func (w *boundedOutput) accept(data []byte) {
	w.data = append(w.data, data...)
	if len(w.data) > outputLimit {
		w.data = append([]byte(nil), w.data[len(w.data)-outputLimit:]...)
	}
	for _, value := range data {
		if value == '\n' || value == '\r' {
			w.flushLine()
		} else {
			w.line = append(w.line, value)
			if len(w.line) >= 4000 {
				w.flushLine()
			}
		}
	}
}
func (w *boundedOutput) flushLine() {
	line := strings.TrimSpace(string(w.line))
	w.line = w.line[:0]
	if line != "" && w.emit != nil {
		w.emit(line)
	}
}
func (w *boundedOutput) flush() { w.mu.Lock(); defer w.mu.Unlock(); w.drain(true); w.flushLine() }
func (w *boundedOutput) text() string {
	w.mu.Lock()
	defer w.mu.Unlock()
	return string(w.data)
}
