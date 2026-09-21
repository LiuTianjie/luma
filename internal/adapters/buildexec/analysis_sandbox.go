package buildexec

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/node"
)

var ErrAnalyzerCleanup = errors.New("analyzer container cleanup could not be verified")

// AnalysisSandbox executes only a preinstalled, digest-pinned runner on the
// explicitly configured rootless Docker endpoint. Configuration is Agent-owned;
// a task cannot choose a daemon, runner credential, mount path or Docker context.
type AnalysisSandbox struct {
	Runner                         node.CommandRunner
	Docker, Host, Image            string
	ControllerURL, ControllerToken string
	AIRequired                     bool
	PIDsLimit, MaxEntries          int
	unhealthy                      atomic.Bool
	// Unit tests exercise the command and filesystem paths without a daemon.
	peer func(context.Context, string) (analysisPeer, error)
}

type analysisPeer struct{ UID, GID int }

func (s *AnalysisSandbox) validate() error {
	if s == nil || s.Runner == nil || !filepath.IsAbs(s.Docker) || s.unhealthy.Load() {
		return fmt.Errorf("analysis sandbox is unavailable")
	}
	image, digest, ok := strings.Cut(s.Image, "@")
	if !ok || image == "" || strings.ContainsAny(image, "\x00\r\n\t @") || !builddomain.ValidContentDigest(digest) {
		return fmt.Errorf("analysis sandbox image must be pinned by digest")
	}
	if !rootlessEndpoint.MatchString(s.Host) {
		return fmt.Errorf("analysis sandbox requires an explicit rootless Unix socket")
	}
	if s.PIDsLimit != 0 && (s.PIDsLimit < 32 || s.PIDsLimit > 4096) {
		return fmt.Errorf("invalid analysis PID limit")
	}
	if s.MaxEntries < 0 || s.MaxEntries > 2000000 {
		return fmt.Errorf("invalid analysis file limit")
	}
	if (s.ControllerURL == "") != (s.ControllerToken == "") {
		return fmt.Errorf("analysis controller URL and token must be configured together")
	}
	if s.ControllerURL != "" {
		u, err := url.Parse(s.ControllerURL)
		if err != nil || (u.Scheme != "https" && u.Scheme != "http") || u.Hostname() == "" || u.User != nil || u.Fragment != "" || strings.ContainsAny(s.ControllerURL+s.ControllerToken, "\x00\r\n") || len(s.ControllerToken) > 8192 {
			return fmt.Errorf("invalid analysis controller configuration")
		}
	}
	return nil
}

func (s *AnalysisSandbox) verifyPeer(ctx context.Context) (analysisPeer, error) {
	if s.peer != nil {
		return s.peer(ctx, s.Host)
	}
	return rootlessPeer(ctx, s.Host)
}

func (s *AnalysisSandbox) environment(home string) map[string]string {
	return map[string]string{"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": home, "DOCKER_CONFIG": home, "LANG": "C.UTF-8"}
}

func (s *AnalysisSandbox) command(ctx context.Context, env map[string]string, args ...string) (node.CommandResult, error) {
	argv := append([]string{s.Docker, "--host", s.Host}, args...)
	return s.Runner.Run(ctx, node.Command{Args: argv, Env: env, ClearEnv: true, Secrets: []string{s.ControllerToken}, Timeout: time.Until(deadlineOr(ctx, time.Now().Add(15*time.Second)))}, nil)
}

func (s *AnalysisSandbox) probe(ctx context.Context, env map[string]string) (analysisPeer, error) {
	identity, err := s.verifyPeer(ctx)
	if err != nil {
		return identity, err
	}
	probeCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	result, err := s.command(probeCtx, env, "info", "--format", "{{json .SecurityOptions}}")
	var options []string
	if err != nil || result.Code != 0 || json.Unmarshal([]byte(result.Output), &options) != nil {
		return identity, fmt.Errorf("rootless Docker security probe failed")
	}
	if !slices.Contains(options, "name=rootless") && !slices.Contains(options, "rootless") {
		return identity, fmt.Errorf("Docker daemon did not prove rootless mode")
	}
	result, err = s.command(probeCtx, env, "image", "inspect", s.Image, "--format", "{{json .RepoDigests}}")
	var digests []string
	if err != nil || result.Code != 0 || json.Unmarshal([]byte(result.Output), &digests) != nil || !slices.Contains(digests, s.Image) {
		return identity, fmt.Errorf("preinstalled analyzer image does not match its configured digest")
	}
	return identity, nil
}

// Probe may be used by capability negotiation once the complete analysis action
// is composed. Each Run repeats the peer, daemon and image checks.
func (s *AnalysisSandbox) Probe(ctx context.Context) error {
	if err := s.validate(); err != nil {
		return err
	}
	home, err := os.MkdirTemp("", "luma-analyzer-probe-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(home)
	_, err = s.probe(ctx, s.environment(home))
	return err
}

func (s *AnalysisSandbox) Run(ctx context.Context, p *PreparedAnalysis, limits builddomain.AnalysisLimits) (err error) {
	if err := s.validate(); err != nil {
		return err
	}
	if err := limits.Validate(); err != nil {
		return err
	}
	if p == nil || p.Metadata.AgentImageDigest != s.Image || p.Metadata.BuilderTaskID == "" || p.Deadline.IsZero() {
		return fmt.Errorf("analysis workspace is not bound to the configured runner")
	}
	ctx, deadlineCancel := context.WithDeadline(ctx, p.Deadline)
	defer deadlineCancel()
	if err := ctx.Err(); err != nil {
		return err
	}
	home, err := os.MkdirTemp("", "luma-analyzer-docker-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(home)
	env := s.environment(home)
	identity, err := s.probe(ctx, env)
	if err != nil {
		return analysisCause(ctx, err)
	}
	maxEntries := s.MaxEntries
	if maxEntries == 0 {
		maxEntries = 200000
	}
	if err := prepareAnalysisBinds(p, identity, maxEntries); err != nil {
		return err
	}
	ctx, cancel := context.WithCancelCause(ctx)
	defer cancel(nil)
	stop := watchAnalysisBudget(ctx, cancel, p.Work, builddomain.SourceLimits{MaxBytes: limits.DiskMiB << 20, MaxEntries: maxEntries})
	defer stop()
	if err := analysisBudget(ctx, p.Work, builddomain.SourceLimits{MaxBytes: limits.DiskMiB << 20, MaxEntries: maxEntries}); err != nil {
		return err
	}
	taskHash := fmt.Sprintf("%x", sha256.Sum256([]byte(p.Metadata.BuilderTaskID)))
	name := "luma-lae-analyze-" + taskHash[:16]
	pidLimit := s.PIDsLimit
	if pidLimit == 0 {
		pidLimit = 256
	}
	network := "none"
	if s.ControllerURL != "" {
		network = "bridge"
		env["LAE_AGENT_CONTROLLER_URL"] = s.ControllerURL
		env["LAE_AGENT_CONTROLLER_TOKEN"] = s.ControllerToken
	}
	aiRequired := "0"
	if s.AIRequired {
		aiRequired = "1"
	}
	args := []string{"run", "--rm", "--pull", "never", "--name", name, "--label", "io.luma.component=analyzer", "--label", "io.luma.task-hash=" + taskHash, "--network", network, "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--user", "0:0", "--pids-limit", strconv.Itoa(pidLimit), "--memory", strconv.Itoa(limits.MemoryMiB) + "m", "--cpus", strconv.FormatFloat(limits.CPU, 'g', -1, 64), "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m", "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "LAE_AGENT_AI_REQUIRED=" + aiRequired}
	if network == "bridge" {
		args = append(args, "--env", "LAE_AGENT_CONTROLLER_URL", "--env", "LAE_AGENT_CONTROLLER_TOKEN")
	}
	args = append(args, "--mount", "type=bind,source="+p.Workspace+",target=/workspace,readonly", "--mount", "type=bind,source="+p.Input+",target=/input,readonly", "--mount", "type=bind,source="+p.Output+",target=/output", "--entrypoint", "lae-agent-runner", s.Image, "analyze", "--source", "/workspace", "--metadata", "/input/metadata.json", "--output-dir", "/output")
	result, runErr := s.command(ctx, env, args...)
	// The client process may be gone while Docker still runs the container.
	// Cleanup has its own bounded context and addresses only this task's name.
	if !s.removeContainer(env, name) {
		s.unhealthy.Store(true)
		p.cleanupBlocked.Store(true)
		return ErrAnalyzerCleanup
	}
	if runErr != nil || result.Code != 0 {
		return analysisCause(ctx, fmt.Errorf("analysis runner failed"))
	}
	if err := ctx.Err(); err != nil {
		return analysisCause(ctx, err)
	}
	if err := hardenAnalysisOutput(p.Output, identity, maxEntries); err != nil {
		return err
	}
	return analysisBudget(ctx, p.Work, builddomain.SourceLimits{MaxBytes: limits.DiskMiB << 20, MaxEntries: maxEntries})
}

func (s *AnalysisSandbox) removeContainer(env map[string]string, name string) bool {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	for attempt := 0; attempt < 2; attempt++ {
		step, done := context.WithTimeout(ctx, 7*time.Second)
		_, _ = s.command(step, env, "rm", "-f", name)
		done()
		step, done = context.WithTimeout(ctx, 7*time.Second)
		result, err := s.command(step, env, "container", "inspect", name, "--format", "{{.Id}}")
		done()
		if err == nil && result.Code != 0 && (strings.Contains(strings.ToLower(result.Output), "no such object") || strings.Contains(strings.ToLower(result.Output), "no such container")) {
			return true
		}
		if ctx.Err() != nil {
			return false
		}
	}
	return false
}
