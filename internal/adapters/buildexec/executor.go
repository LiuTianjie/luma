// Package buildexec owns repository checkout and Docker Buildx I/O. It receives
// ephemeral credentials with a task and never writes them into build results.
package buildexec

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/node"
)

type Executor struct {
	Runner                           node.CommandRunner
	Git, Docker, Scratch, Persistent string
	PluginDirs                       []string
	Buildx                           bool
}

func (e Executor) Capabilities() []string {
	if e.Runner != nil && e.Git != "" && e.Docker != "" && e.Scratch != "" && e.Persistent != "" && e.Buildx {
		return []string{"docker-build"}
	}
	return nil
}
func (e Executor) SupportedActions() []string {
	if len(e.Capabilities()) > 0 {
		return []string{"build-image"}
	}
	return nil
}
func (e Executor) builderName() string {
	sum := sha256.Sum256([]byte(e.Persistent))
	return "luma-go-" + hex.EncodeToString(sum[:8])
}
func (e Executor) ensurePersistent() error {
	if !filepath.IsAbs(e.Persistent) {
		return fmt.Errorf("builder state path must be absolute")
	}
	if err := os.MkdirAll(e.Persistent, 0700); err != nil {
		return err
	}
	info, err := os.Lstat(e.Persistent)
	if err != nil {
		return err
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm()&0077 != 0 {
		return fmt.Errorf("builder state must be a private directory")
	}
	return nil
}

type session struct {
	selectedBuilder                      string
	localEnv                             map[string]string
	reservedRepository, ephemeralBuilder bool
	executor                             Executor
	path                                 string
	request                              builddomain.ImageRequest
	secrets                              []string
	emit                                 func(node.Event)
}

func (e Executor) newSession(r builddomain.ImageRequest, emit func(node.Event)) (*session, error) {
	if err := e.ensurePersistent(); err != nil {
		return nil, err
	}
	path, err := os.MkdirTemp(e.Scratch, "build-")
	if err != nil {
		return nil, err
	}
	s := &session{executor: e, path: path, request: r, emit: emit, secrets: []string{r.GitToken}}
	if r.Proxy != "" {
		s.secrets = append(s.secrets, r.Proxy)
	}
	auths := map[string]map[string]string{}
	if a := r.RegistryAuth; a != nil {
		if a.Username == "" || a.Password == "" || a.ServerAddress == "" || a.ServerAddress != r.PushHost && a.ServerAddress != r.RegistryHost {
			os.RemoveAll(path)
			return nil, fmt.Errorf("build registry credentials must match the push host")
		}
		encoded := base64.StdEncoding.EncodeToString([]byte(a.Username + ":" + a.Password))
		auths[r.PushHost] = map[string]string{"auth": encoded}
		auths[r.RegistryHost] = map[string]string{"auth": encoded}
		s.secrets = append(s.secrets, a.Password, a.Username+":"+a.Password, encoded)
	}
	raw, _ := json.Marshal(map[string]any{"auths": auths, "cliPluginsExtraDirs": e.PluginDirs})
	if err := os.WriteFile(filepath.Join(path, "config.json"), raw, 0600); err != nil {
		os.RemoveAll(path)
		return nil, err
	}
	return s, nil
}
func (s *session) env() map[string]string {
	env := map[string]string{"DOCKER_CONFIG": s.path, "BUILDX_CONFIG": filepath.Join(s.executor.Persistent, "buildx"), "DOCKER_CONTEXT": "", "BUILDX_BUILDER": "", "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "", "NO_PROXY": "", "http_proxy": "", "https_proxy": "", "all_proxy": "", "no_proxy": ""}
	if s.request.Proxy != "" {
		for _, key := range []string{"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"} {
			env[key] = s.request.Proxy
		}
		env["NO_PROXY"] = "localhost,127.0.0.1,::1," + s.request.PushHost + "," + s.request.RegistryHost
	}
	for key, value := range s.localEnv {
		env[key] = value
	}
	return env
}
func (s *session) clean(value string) string {
	for _, secret := range s.secrets {
		if secret != "" {
			value = strings.ReplaceAll(value, secret, "[REDACTED]")
		}
	}
	return value
}
func (s *session) command(ctx context.Context, c node.Command) (string, error) {
	if c.Env == nil {
		c.Env = s.env()
	}
	c.Secrets = s.secrets
	out, err := s.executor.Runner.Run(ctx, c, func(line string) {
		if s.emit != nil {
			s.emit(node.Event{Type: "output", Line: s.clean(line), Timestamp: time.Now().Unix()})
		}
	})
	if err != nil {
		if ctx.Err() != nil {
			return "", ctx.Err()
		}
		return "", fmt.Errorf("build tool failed: %s", s.clean(err.Error()))
	}
	if out.Code != 0 {
		message := s.clean(out.Output)
		if len(message) > 4000 {
			message = message[len(message)-4000:]
		}
		return "", fmt.Errorf("build tool exited %d: %s", out.Code, message)
	}
	return out.Output, nil
}
func (e Executor) cleanup(ctx context.Context, s *session) error {
	name := e.builderName()
	// Buildx owns only this agent's deterministic builder. Keep its cache volume,
	// but terminate its container so cancellation cannot leave a build running.
	args := []string{e.Docker, "buildx", "rm", "--force"}
	if !s.ephemeralBuilder {
		args = append(args, "--keep-state")
	}
	args = append(args, name)
	_, rmErr := s.command(ctx, node.Command{Args: args, Timeout: time.Minute})
	existing, err := s.command(ctx, node.Command{Args: []string{e.Docker, "ps", "-aq", "--filter", "name=^/buildx_buildkit_" + name + "0$"}, Timeout: 15 * time.Second})
	if err != nil {
		return err
	}
	if strings.TrimSpace(existing) != "" {
		if _, err := s.command(ctx, node.Command{Args: []string{e.Docker, "rm", "-f", "buildx_buildkit_" + name + "0"}, Timeout: time.Minute}); err != nil {
			return err
		}
	}
	if rmErr != nil && !strings.Contains(strings.ToLower(rmErr.Error()), "no builder") && !strings.Contains(strings.ToLower(rmErr.Error()), "not found") {
		return rmErr
	}
	return os.Remove(filepath.Join(e.Persistent, "active"))
}
func (e Executor) Recover(ctx context.Context) error {
	if err := e.ensurePersistent(); err != nil {
		return err
	}
	raw, err := os.ReadFile(filepath.Join(e.Persistent, "active"))
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	if string(raw) != e.builderName() {
		return fmt.Errorf("invalid owned builder marker")
	}
	if e.Docker == "" || e.Runner == nil {
		return fmt.Errorf("Docker is needed to stop the interrupted owned build")
	}
	s, err := e.newSession(builddomain.ImageRequest{}, nil)
	if err != nil {
		return err
	}
	defer os.RemoveAll(s.path)
	return e.cleanup(ctx, s)
}
func (e Executor) Execute(ctx context.Context, task node.Task, emit func(node.Event)) (out node.Outcome, err error) {
	if task.Action != "build-image" || len(e.Capabilities()) == 0 {
		return out, fmt.Errorf("repository image build is unavailable")
	}
	if len(task.Payload) > 4<<20 {
		return out, fmt.Errorf("build request exceeds limit")
	}
	var r builddomain.ImageRequest
	if json.Unmarshal(task.Payload, &r) != nil {
		return out, fmt.Errorf("invalid image build payload")
	}
	r, err = r.Validate()
	if err != nil {
		return out, err
	}
	if err := e.Recover(ctx); err != nil {
		return out, err
	}
	s, err := e.newSession(r, emit)
	if err != nil {
		return out, err
	}
	defer os.RemoveAll(s.path)
	runCtx, cancel := context.WithTimeout(ctx, time.Duration(r.BuildTimeout)*time.Second)
	defer cancel()
	root, sha, err := s.checkout(runCtx, r)
	if err != nil {
		return out, err
	}
	kind, manifest, err := discover(root, r.ComposeSidecar)
	if err != nil {
		return out, err
	}
	plan, err := s.plan(root, sha, kind, manifest)
	if err != nil {
		return out, err
	}
	if len(plan.images) > 0 {
		if err = os.WriteFile(filepath.Join(e.Persistent, "active"), []byte(e.builderName()), 0600); err != nil {
			return out, err
		}
		defer func() {
			cleanCtx, stop := context.WithTimeout(context.WithoutCancel(ctx), 90*time.Second)
			defer stop()
			if cleanupErr := e.cleanup(cleanCtx, s); cleanupErr != nil {
				err = errors.Join(err, fmt.Errorf("owned builder cleanup failed: %w", cleanupErr))
				out = node.Outcome{}
			}
		}()
		if err = s.createBuilder(runCtx); err != nil {
			return out, err
		}
		for _, image := range plan.images {
			if err = s.build(runCtx, image, sha); err != nil {
				return out, err
			}
		}
	}
	value, err := plan.result(sha)
	if err != nil {
		return out, err
	}
	raw, err := json.Marshal(value)
	return node.Outcome{Result: raw, Message: value.Message}, err
}
func (s *session) createBuilder(ctx context.Context) error {
	r := s.request
	config := ""
	for _, host := range []string{r.RegistryHost, r.PushHost} {
		config += fmt.Sprintf("[registry.%q]\n  http = true\n  insecure = true\n", host)
		if r.RegistryHost == r.PushHost {
			break
		}
	}
	path := filepath.Join(s.path, "buildkitd.toml")
	if err := os.WriteFile(path, []byte(config), 0600); err != nil {
		return err
	}
	args := []string{s.executor.Docker, "buildx", "create", "--name", s.executor.builderName(), "--driver", "docker-container", "--driver-opt", "network=host", "--buildkitd-config", path}
	if r.Proxy != "" {
		for _, key := range []string{"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"} {
			value := "env." + key + "=" + s.env()[key]
			if strings.ContainsAny(value, ",\"") {
				value = "\"" + strings.ReplaceAll(value, "\"", "\"\"") + "\""
			}
			args = append(args, "--driver-opt", value)
		}
	}
	args = append(args, "--bootstrap")
	_, err := s.command(ctx, node.Command{Args: args, Timeout: 3 * time.Minute})
	return err
}
func (s *session) build(ctx context.Context, p imagePlan, sha string) error {
	builder := s.selectedBuilder
	if builder == "" {
		builder = s.executor.builderName()
	}
	args := []string{s.executor.Docker, "buildx", "build", "--builder", builder, "--platform", p.platform, "--progress", "plain", "--output", "type=image,push=true,registry.insecure=true", "-t", s.request.PushHost + "/" + p.repo + ":" + sha, "-t", s.request.PushHost + "/" + p.repo + ":latest"}
	for _, key := range sortedKeys(p.args) {
		args = append(args, "--build-arg", key+"="+p.args[key])
	}
	env := s.env()
	if s.request.Proxy != "" {
		for _, key := range []string{"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"} {
			args = append(args, "--build-arg", key)
		}
	}
	args = append(args, "-f", p.dockerfile, p.context)
	_, err := s.command(ctx, node.Command{Args: args, Env: env, Timeout: time.Duration(s.request.BuildTimeout) * time.Second})
	return err
}
