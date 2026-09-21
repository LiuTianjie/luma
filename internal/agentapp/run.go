// Package agentapp composes the node worker with explicit host and Control
// adapters. It owns process configuration, not task recovery or host policy.
package agentapp

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"

	"github.com/LiuTianjie/luma/internal/adapters/agentstate"
	"github.com/LiuTianjie/luma/internal/adapters/buildexec"
	"github.com/LiuTianjie/luma/internal/adapters/controlhttp"
	"github.com/LiuTianjie/luma/internal/adapters/hostadmin"
	"github.com/LiuTianjie/luma/internal/adapters/hostexec"
	"github.com/LiuTianjie/luma/internal/adapters/hostmetrics"
	"github.com/LiuTianjie/luma/internal/adapters/imageexec"
	"github.com/LiuTianjie/luma/internal/adapters/process"
	"github.com/LiuTianjie/luma/internal/node"
)

func Run(ctx context.Context, args []string, out, errout io.Writer, version string) int {
	flags := flag.NewFlagSet("luma-agent", flag.ContinueOnError)
	flags.SetOutput(errout)
	path := flags.String("config", "/opt/luma/node-agent/agent.json", "private node agent config")
	once := flags.Bool("once", false, "lease at most one task, acknowledging its outcome before exit")
	showVersion := flags.Bool("version", false, "show agent version")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 1
	}
	if flags.NArg() != 0 {
		fmt.Fprintln(errout, "unexpected agent arguments")
		return 1
	}
	if *showVersion {
		fmt.Fprintln(out, version)
		return 0
	}
	if err := run(ctx, *path, *once, version, errout); err != nil && !errors.Is(err, context.Canceled) {
		fmt.Fprintln(errout, "luma-agent:", err)
		return 1
	}
	return 0
}
func run(ctx context.Context, path string, once bool, version string, errout io.Writer) error {
	config, err := agentstate.LoadConfig(path)
	if err != nil {
		return err
	}
	client, err := controlhttp.NewAgent(controlhttp.Options{Endpoint: config.Endpoint, Token: config.Token, Insecure: config.Insecure, ResolveIP: config.ResolveIP})
	if err != nil {
		return err
	}
	defer client.Close()
	store, unlock, err := agentstate.Open(path)
	if err != nil {
		return err
	}
	defer unlock()
	scratch, err := agentstate.Scratch(path)
	if err != nil {
		return err
	}
	executable, err := os.Executable()
	if err != nil {
		return err
	}
	runner := process.Runner{Supervisor: executable}
	handlers := []node.Handler{
		node.Handler{Actions: []string{"prepare-managed-volume-path", "remove-managed-volume-path", "remove-docker-volume"}, Executor: hostexec.Executor{Runner: runner, Docker: dockerBinary()}},
		node.Handler{Actions: []string{"resolve-docker-image", "diagnose-docker-pull", "mirror-control-image", "mirror-system-image", "cache-runtime-image"}, Executor: imageexec.Executor{Runner: runner, Docker: dockerBinary(), Crane: craneBinary(), Scratch: scratch}},
	}
	git, _ := exec.LookPath("git")
	buildState, err := filepath.Abs(path + ".go-build")
	if err != nil {
		return err
	}
	pluginDirs := buildxPluginDirs()
	builder := buildexec.Executor{Runner: runner, Git: git, Docker: dockerBinary(), Scratch: scratch, Persistent: buildState, PluginDirs: pluginDirs, Buildx: len(pluginDirs) > 0}
	if err := builder.Recover(ctx); err != nil {
		return err
	}
	if actions := builder.SupportedActions(); len(actions) > 0 {
		handlers = append(handlers, node.Handler{Actions: actions, Executor: builder})
	}
	admin := hostadmin.Executor{Runner: runner, Docker: dockerBinary(), Root: "/", Platform: runtime.GOOS, Privileged: os.Geteuid() == 0}
	if actions := admin.SupportedActions(); len(actions) > 0 {
		handlers = append(handlers, node.Handler{Actions: actions, Executor: admin})
	}
	executor, err := node.NewDispatcher(handlers...)
	if err != nil {
		return err
	}
	metrics := &hostmetrics.Sampler{Runner: runner, DiskPath: os.Getenv("LUMA_METRICS_DISK_PATH")}
	statsInterval := config.StatsIntervalSeconds
	if value := os.Getenv("LUMA_NODE_AGENT_STATS_INTERVAL_SECONDS"); value != "" {
		parsed, err := strconv.Atoi(value)
		if err != nil || parsed < 1 || parsed > 3600 {
			return fmt.Errorf("invalid LUMA_NODE_AGENT_STATS_INTERVAL_SECONDS")
		}
		statsInterval = parsed
	}
	containers := &hostmetrics.ContainerSampler{Runner: runner, Docker: dockerBinary(), Interval: time.Duration(statsInterval) * time.Second, Log: func(message string) { fmt.Fprintln(errout, "luma-agent:", message) }}
	sampling, stop := context.WithCancel(ctx)
	defer stop()
	sampleDone := make(chan struct{})
	go func() { defer close(sampleDone); metrics.Run(sampling) }()
	containersDone := make(chan struct{})
	go func() { defer close(containersDone); containers.Run(sampling) }()
	defer func() { stop(); <-containersDone }()
	defer func() { stop(); <-sampleDone }()
	options := config.WorkerOptions()
	options.Once = once
	arch := runtime.GOARCH
	if arch == "amd64" {
		arch = "x86_64"
	}
	worker := node.Worker{Control: client, Executor: executor, Receipts: store, Telemetry: metrics, Containers: containers, Authority: strings.TrimRight(config.Endpoint, "/"), Options: options, Presence: node.Presence{Identity: node.Identity{Name: config.NodeName, ID: config.NodeID}, OS: runtime.GOOS, Arch: arch, Version: version}, Log: func(message string) { fmt.Fprintln(errout, "luma-agent:", message) }}
	return worker.Run(ctx)
}
func craneBinary() string {
	if path, err := exec.LookPath("crane"); err == nil {
		return path
	}
	for _, path := range []string{"/usr/local/bin/crane", "/opt/homebrew/bin/crane", "/usr/bin/crane"} {
		if info, err := os.Stat(path); err == nil && !info.IsDir() && info.Mode()&0111 != 0 {
			return path
		}
	}
	return ""
}
func dockerBinary() string {
	if path, err := exec.LookPath("docker"); err == nil {
		return path
	}
	for _, path := range []string{"/usr/local/bin/docker", "/opt/homebrew/bin/docker", "/Applications/OrbStack.app/Contents/MacOS/xbin/docker", "/Applications/Docker.app/Contents/Resources/bin/docker"} {
		if info, err := os.Stat(path); err == nil && !info.IsDir() && info.Mode()&0111 != 0 {
			return path
		}
	}
	return ""
}

func buildxPluginDirs() []string { return buildexec.PluginDirectories() }
