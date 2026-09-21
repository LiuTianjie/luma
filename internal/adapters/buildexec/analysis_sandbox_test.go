package buildexec

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/node"
)

type sandboxRunner struct {
	mu            sync.Mutex
	commands      [][]string
	cleanupSticks bool
}

func (r *sandboxRunner) Run(_ context.Context, command node.Command, _ func(string)) (node.CommandResult, error) {
	r.mu.Lock()
	r.commands = append(r.commands, append([]string(nil), command.Args...))
	r.mu.Unlock()
	if command.ClearEnv {
		if command.Env["DOCKER_CONFIG"] == "" || command.Env["HOME"] == "" {
			return node.CommandResult{}, errors.New("missing isolated environment")
		}
	}
	args := command.Args
	joined := strings.Join(args, " ")
	switch {
	case strings.Contains(joined, " info "):
		return node.CommandResult{Output: `["name=rootless","name=seccomp"]`}, nil
	case strings.Contains(joined, " image inspect "):
		image := args[len(args)-3]
		return node.CommandResult{Output: `["` + image + `"]`}, nil
	case strings.Contains(joined, " container inspect "):
		if r.cleanupSticks {
			return node.CommandResult{Code: 0, Output: "still-running"}, nil
		}
		return node.CommandResult{Code: 1, Output: "Error: No such object"}, nil
	default:
		return node.CommandResult{}, nil
	}
}

func sandboxImage() string { return "registry.test/analyzer@sha256:" + strings.Repeat("a", 64) }
func preparedSandbox(t *testing.T) (*PreparedAnalysis, string, analysisPeer) {
	t.Helper()
	work := t.TempDir()
	p := &PreparedAnalysis{Work: work, Workspace: filepath.Join(work, "workspace"), Input: filepath.Join(work, "input"), Output: filepath.Join(work, "output"), Deadline: time.Now().Add(time.Minute), Metadata: builddomain.AnalysisMetadata{BuilderTaskID: "task", AgentImageDigest: sandboxImage()}}
	for _, dir := range []string{p.Workspace, p.Input, p.Output} {
		if err := os.Mkdir(dir, 0700); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(p.Workspace, "source.txt"), []byte("source"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(p.Input, "metadata.json"), []byte("{}"), 0600); err != nil {
		t.Fatal(err)
	}
	stat, err := os.Stat(p.Work)
	if err != nil {
		t.Fatal(err)
	}
	st := stat.Sys().(*syscall.Stat_t)
	return p, work, analysisPeer{UID: int(st.Uid), GID: int(st.Gid)}
}

func TestAnalysisSandboxBindsLimitsAndCleansByName(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("rootless Docker peer contract is Linux-specific")
	}
	p, work, peer := preparedSandbox(t)
	runner := &sandboxRunner{}
	sandbox := &AnalysisSandbox{Runner: runner, Docker: "/usr/bin/docker", Host: "unix:///run/user/1000/docker.sock", Image: sandboxImage(), MaxEntries: 100}
	sandbox.peer = func(context.Context, string) (analysisPeer, error) { return peer, nil }
	limits := builddomain.AnalysisLimits{CPU: 1, MemoryMiB: 256, DiskMiB: 256, TimeoutSeconds: 30}
	if err := sandbox.Run(context.Background(), p, limits); err != nil {
		t.Fatal(err)
	}
	if mode, _ := os.Stat(p.Workspace); mode.Mode().Perm() != 0500 {
		t.Fatalf("workspace mode %o", mode.Mode().Perm())
	}
	if mode, _ := os.Stat(p.Input); mode.Mode().Perm() != 0500 {
		t.Fatalf("input mode %o", mode.Mode().Perm())
	}
	if mode, _ := os.Stat(p.Output); mode.Mode().Perm() != 0700 {
		t.Fatalf("output mode %o", mode.Mode().Perm())
	}
	if err := p.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(work); !os.IsNotExist(err) {
		t.Fatal("workspace retained")
	}
	runner.mu.Lock()
	defer runner.mu.Unlock()
	if len(runner.commands) < 4 {
		t.Fatalf("sandbox did not probe/run/clean: %v", runner.commands)
	}
	for _, command := range runner.commands {
		for _, arg := range command {
			if strings.Contains(arg, "private") || strings.Contains(arg, "token") {
				t.Fatal("secret entered Docker argv")
			}
		}
	}
	last := runner.commands[2]
	if !strings.Contains(strings.Join(last, " "), "--read-only") || !strings.Contains(strings.Join(last, " "), "--network none") || !strings.Contains(strings.Join(last, " "), "--cap-drop ALL") {
		t.Fatalf("unsafe runner flags: %v", last)
	}
}

func TestAnalysisSandboxFailsClosedOnCleanupAndDoesNotRemoveWorkspace(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("rootless Docker peer contract is Linux-specific")
	}
	p, work, peer := preparedSandbox(t)
	runner := &sandboxRunner{cleanupSticks: true}
	sandbox := &AnalysisSandbox{Runner: runner, Docker: "/usr/bin/docker", Host: "unix:///run/user/1000/docker.sock", Image: sandboxImage()}
	sandbox.peer = func(context.Context, string) (analysisPeer, error) { return peer, nil }
	err := sandbox.Run(context.Background(), p, builddomain.AnalysisLimits{CPU: 1, MemoryMiB: 256, DiskMiB: 256, TimeoutSeconds: 30})
	if !errors.Is(err, ErrAnalyzerCleanup) {
		t.Fatalf("cleanup failure not surfaced: %v", err)
	}
	if err := p.Close(); err == nil {
		t.Fatal("cleanup failure allowed workspace deletion")
	}
	if _, err := os.Stat(work); err != nil {
		t.Fatalf("workspace was lost on cleanup failure: %v", err)
	}
	if err := sandbox.Run(context.Background(), p, builddomain.AnalysisLimits{CPU: 1, MemoryMiB: 256, DiskMiB: 256, TimeoutSeconds: 30}); err == nil {
		t.Fatal("unhealthy sandbox was reused")
	}
}

func TestAnalysisSandboxValidationFailsClosed(t *testing.T) {
	cases := []AnalysisSandbox{{Docker: "/usr/bin/docker", Host: "unix:///var/run/docker.sock", Image: sandboxImage()}, {Docker: "/usr/bin/docker", Host: "unix:///run/user/1000/docker.sock", Image: "registry.test/analyzer:latest"}, {Docker: "/usr/bin/docker", Host: "unix:///run/user/1000/docker.sock", Image: sandboxImage(), ControllerURL: "https://controller.test"}}
	for i, sandbox := range cases {
		sandbox.Runner = &sandboxRunner{}
		if err := sandbox.validate(); err == nil {
			t.Fatalf("case %d accepted unsafe config", i)
		}
	}
}
