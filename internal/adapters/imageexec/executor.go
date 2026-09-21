// Package imageexec implements typed Docker and crane image operations. Policy
// lives in registry; process lifetime/cancellation belongs to CommandRunner.
package imageexec

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/LiuTianjie/luma/internal/node"
	"github.com/LiuTianjie/luma/internal/registry"
)

type Executor struct {
	Runner                 node.CommandRunner
	Docker, Crane, Scratch string
}

func (e Executor) Capabilities() []string {
	var caps []string
	if e.Runner == nil || e.Scratch == "" {
		return caps
	}
	if e.Docker != "" {
		caps = append(caps, "docker-image")
	}
	if e.Crane != "" {
		caps = append(caps, "control-image-mirror-v1", "system-image-mirror-v1", "runtime-image-cache-v1")
	}
	return caps
}
func (e Executor) Execute(ctx context.Context, task node.Task, emit func(node.Event)) (node.Outcome, error) {
	if err := ctx.Err(); err != nil {
		return node.Outcome{}, err
	}
	if e.Runner == nil {
		return node.Outcome{}, fmt.Errorf("image process runner is unavailable")
	}
	trimmed := strings.TrimSpace(string(task.Payload))
	if len(trimmed) == 0 || len(trimmed) > 4<<20 || trimmed[0] != '{' || !json.Valid(task.Payload) {
		return node.Outcome{}, fmt.Errorf("invalid image task payload")
	}
	switch task.Action {
	case "resolve-docker-image", "diagnose-docker-pull":
		var p pullPayload
		if json.Unmarshal(task.Payload, &p) != nil {
			return node.Outcome{}, fmt.Errorf("invalid image task field types")
		}
		return e.pull(ctx, task.Action, p, emit)
	case "mirror-control-image", "mirror-system-image", "cache-runtime-image":
		var p mirrorPayload
		if json.Unmarshal(task.Payload, &p) != nil {
			return node.Outcome{}, fmt.Errorf("invalid image task field types")
		}
		return e.mirror(ctx, task.Action, p, emit)
	default:
		return node.Outcome{}, fmt.Errorf("unsupported image task action")
	}
}
func result(value any, message string) (node.Outcome, error) {
	data, err := json.Marshal(value)
	return node.Outcome{Result: data, Message: message}, err
}
func emitLine(emit func(node.Event), kind, line string) {
	if emit != nil {
		emit(node.Event{Type: kind, Line: line, Timestamp: time.Now().Unix()})
	}
}
func boundedTimeout(seconds, fallback, low, high int) time.Duration {
	if seconds == 0 {
		seconds = fallback
	}
	return time.Duration(min(high, max(low, seconds))) * time.Second
}

type pullPayload struct {
	Image        string        `json:"image"`
	RegistryAuth *registryAuth `json:"registryAuth"`
	ForcePull    bool          `json:"forcePull"`
	Platform     string        `json:"platform"`
	Timeout      int           `json:"timeout"`
}

func (e Executor) pull(ctx context.Context, action string, p pullPayload, emit func(node.Event)) (node.Outcome, error) {
	if e.Docker == "" {
		return node.Outcome{}, fmt.Errorf("Docker executable is unavailable")
	}
	image, err := registry.ParseImage(p.Image)
	if err != nil {
		return node.Outcome{}, err
	}
	p.Platform = strings.TrimSpace(p.Platform)
	if len(p.Platform) > 128 || strings.ContainsAny(p.Platform, "\x00\r\n \t") {
		return node.Outcome{}, fmt.Errorf("invalid Docker target platform")
	}
	s, err := newAuthSession(e.Scratch, p.RegistryAuth)
	if err != nil {
		return node.Outcome{}, err
	}
	defer s.close()
	env := map[string]string{"DOCKER_CONFIG": s.path}
	inspect := func() (node.CommandResult, error) {
		return e.Runner.Run(ctx, node.Command{Args: []string{e.Docker, "image", "inspect", image.Reference, "--format", "{{json .RepoDigests}}"}, Env: env, Secrets: s.secrets, Timeout: time.Minute}, nil)
	}
	resolved := func(digest string, pulled bool) (node.Outcome, error) {
		deployed, message := digest, "Target node image already present"
		if deployed == "" {
			deployed = image.Reference
		}
		if pulled {
			message = "Target node image pull ready"
		}
		return result(map[string]any{"image": image.Reference, "deployed": deployed, "digest": digest, "pulled": pulled, "platform": p.Platform, "message": message}, message)
	}
	if action == "resolve-docker-image" && !p.ForcePull && p.Platform == "" {
		local, err := inspect()
		if err != nil {
			return node.Outcome{}, err
		}
		if local.Code == 0 {
			return resolved(registry.RepoDigest(image, local.Output), false)
		}
	}
	args := []string{e.Docker, "pull"}
	if p.Platform != "" {
		args = append(args, "--platform", p.Platform)
	}
	args = append(args, image.Reference)
	timeout := 10 * time.Minute
	if action == "diagnose-docker-pull" {
		timeout = boundedTimeout(p.Timeout, 600, 30, 1800)
	}
	pulled, err := e.Runner.Run(ctx, node.Command{Args: args, Env: env, Secrets: s.secrets, Timeout: timeout}, func(line string) { emitLine(emit, "output", s.clean(line)) })
	if err != nil {
		return node.Outcome{}, err
	}
	output := s.clean(pulled.Output)
	if action == "diagnose-docker-pull" {
		lines := []string{}
		for _, line := range strings.Split(strings.ReplaceAll(output, "\r", "\n"), "\n") {
			if strings.TrimSpace(line) != "" {
				lines = append(lines, strings.TrimRight(line, " \t"))
			}
		}
		if len(lines) > 200 {
			lines = lines[len(lines)-200:]
		}
		message := "Docker pull diagnostic finished"
		if pulled.Code != 0 {
			message = "Docker pull diagnostic failed"
		}
		return result(map[string]any{"image": image.Reference, "platform": p.Platform, "ok": pulled.Code == 0, "exitCode": pulled.Code, "output": output, "lines": lines, "message": message}, message)
	}
	if pulled.Code != 0 {
		return node.Outcome{}, fmt.Errorf("target node Docker pull failed for %s: %s", image.Reference, tail(output, 1200))
	}
	local, err := inspect()
	if err != nil {
		return node.Outcome{}, err
	}
	digest := ""
	if local.Code == 0 {
		digest = registry.RepoDigest(image, local.Output)
	}
	return resolved(digest, true)
}
func tail(value string, limit int) string {
	value = strings.TrimSpace(value)
	if len(value) > limit {
		return value[len(value)-limit:]
	}
	return value
}
