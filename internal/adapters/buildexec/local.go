package buildexec

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"time"

	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/node"
)

func localRoot(path string) (string, error) {
	root, err := filepath.Abs(path)
	if err != nil {
		return "", err
	}
	root, err = filepath.EvalSymlinks(root)
	if err != nil {
		return "", err
	}
	info, err := os.Stat(root)
	if err != nil || !info.IsDir() {
		return "", fmt.Errorf("local build path must be a directory")
	}
	return root, nil
}
func (e Executor) InspectLocal(ctx context.Context, path, repo, sidecar, region string) (builddomain.LocalSource, error) {
	source := builddomain.LocalSource{TargetNodes: []string{}, TargetRegions: []string{}}
	root, err := localRoot(path)
	if err != nil {
		return source, err
	}
	source.Path = root
	git := func(args ...string) (string, error) {
		if e.Git == "" || e.Runner == nil {
			return "", nil
		}
		result, err := e.Runner.Run(ctx, node.Command{Args: append([]string{e.Git, "-C", root}, args...), Timeout: 15 * time.Second}, nil)
		if err != nil {
			return "", err
		}
		if result.Code != 0 {
			return "", nil
		}
		return strings.TrimSpace(result.Output), nil
	}
	if repo == "" {
		repo, err = git("remote", "get-url", "origin")
		if err != nil {
			return source, err
		}
	}
	if repo == "" {
		return source, fmt.Errorf("cannot infer repository; pass --repo-url or configure git remote origin")
	}
	if !strings.Contains(repo, ":") && len(strings.Split(repo, "/")) == 2 {
		repo = "https://github.com/" + repo
	}
	source.RepoURL, err = builddomain.NormalizeRepository(repo)
	if err != nil {
		return source, err
	}
	source.Revision, err = git("rev-parse", "HEAD")
	if err != nil {
		return source, err
	}
	if source.Revision != "" {
		dirty, err := git("status", "--porcelain")
		if err != nil {
			return source, err
		}
		if dirty != "" {
			source.Revision += "-dirty"
		}
	}
	kind, path, err := discover(root, sidecar)
	if err != nil {
		return source, err
	}
	if path == "" {
		return source, fmt.Errorf("local build requires a Luma deployment manifest")
	}
	manifest, _, err := readYAML(path)
	if err != nil {
		return source, err
	}
	source.Name, source.Kind = text(manifest, "name"), kind
	if source.Name == "" {
		return source, fmt.Errorf("local deployment manifest requires a name")
	}
	target := func(n, r string) {
		if n != "" {
			if !slices.Contains(source.TargetNodes, n) {
				source.TargetNodes = append(source.TargetNodes, n)
			}
		} else if r != "" && !slices.Contains(source.TargetRegions, r) {
			source.TargetRegions = append(source.TargetRegions, r)
		}
	}
	rootNode, rootRegion := text(manifest, "node"), first(region, text(manifest, "region"))
	if kind != "compose" {
		target(rootNode, rootRegion)
	} else {
		composeFile, err := repoPath(root, filepath.Dir(path), first(text(manifest, "compose"), "docker-compose.yml"))
		if err != nil {
			return source, err
		}
		compose, _, err := readYAML(composeFile)
		if err != nil {
			return source, err
		}
		services := object(compose, "services")
		if len(services) == 0 {
			return source, fmt.Errorf("local Compose requires nonempty services")
		}
		for name := range services {
			override := object(object(manifest, "services"), name)
			target(first(text(override, "node"), rootNode), first(text(override, "region"), rootRegion))
		}
	}
	slices.Sort(source.TargetNodes)
	slices.Sort(source.TargetRegions)
	return source, nil
}

var localTag = regexp.MustCompile(`^local-[a-f0-9]{16,32}$`)
var localBuilder = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`)

// BuildLocal uses the caller's actual files and Docker runtime. Only builders
// created by this invocation are removed; an explicit existing builder is borrowed.
func (e Executor) BuildLocal(ctx context.Context, r builddomain.LocalImageRequest, emit func(node.Event)) (out builddomain.ImageResult, err error) {
	if e.Runner == nil || e.Docker == "" || !e.Buildx {
		return out, fmt.Errorf("local Docker Buildx is unavailable")
	}
	if !localTag.MatchString(r.Tag) {
		return out, fmt.Errorf("invalid reserved local build tag")
	}
	if r.Builder != "" && !localBuilder.MatchString(r.Builder) {
		return out, fmt.Errorf("invalid local Buildx builder name")
	}
	root, err := localRoot(r.Path)
	if err != nil {
		return out, err
	}
	r.ImageRequest.PushHost = r.RegistryHost // local client uploads to the externally reachable registry.
	r.ImageRequest, err = r.ImageRequest.Validate()
	if err != nil {
		return out, err
	}
	state, err := os.MkdirTemp(e.Scratch, "local-builder-")
	if err != nil {
		return out, err
	}
	defer os.RemoveAll(state)
	e.Persistent = state
	s, err := e.newSession(r.ImageRequest, emit)
	if err != nil {
		return out, err
	}
	defer os.RemoveAll(s.path)
	s.reservedRepository = true
	s.ephemeralBuilder = true
	s.selectedBuilder = r.Builder
	if err := s.localDockerConfig(r.DockerConfig, r.DockerContext); err != nil {
		return out, err
	}
	runCtx, cancel := context.WithTimeout(ctx, time.Duration(r.BuildTimeout)*time.Second)
	defer cancel()
	kind, path, err := discover(root, r.ComposeSidecar)
	if err != nil {
		return out, err
	}
	if path == "" {
		return out, fmt.Errorf("local build requires a Luma deployment manifest")
	}
	plan, err := s.plan(root, r.Tag, kind, path)
	if err != nil {
		return out, err
	}
	if len(plan.images) == 0 {
		return out, fmt.Errorf("local build requires at least one image build")
	}
	if r.Builder == "" {
		if err := os.WriteFile(filepath.Join(state, "active"), []byte(e.builderName()), 0600); err != nil {
			return out, err
		}
		defer func() {
			cleanCtx, stop := context.WithTimeout(context.WithoutCancel(ctx), 90*time.Second)
			defer stop()
			if cleanErr := e.cleanup(cleanCtx, s); cleanErr != nil {
				err = errors.Join(err, cleanErr)
				out = builddomain.ImageResult{}
			}
		}()
		if err := s.createBuilder(runCtx); err != nil {
			return out, err
		}
	}
	for _, image := range plan.images {
		if err := s.build(runCtx, image, r.Tag); err != nil {
			return out, err
		}
	}
	return plan.result(r.Tag)
}

func (s *session) localDockerConfig(root, selectedContext string) error {
	if root == "" {
		root = os.Getenv("DOCKER_CONFIG")
		if root == "" {
			home, err := os.UserHomeDir()
			if err != nil {
				return err
			}
			root = filepath.Join(home, ".docker")
		}
	}
	if selectedContext == "" {
		selectedContext = os.Getenv("DOCKER_CONTEXT")
	}
	s.localEnv = map[string]string{}
	config := map[string]any{}
	if root != "" {
		absolute, err := filepath.Abs(root)
		if err != nil {
			return err
		}
		root = absolute
		path := filepath.Join(root, "config.json")
		info, err := os.Stat(path)
		if err == nil {
			if !info.Mode().IsRegular() || info.Size() > 4<<20 {
				return fmt.Errorf("invalid local Docker configuration")
			}
			raw, err := os.ReadFile(path)
			if err != nil {
				return err
			}
			if json.Unmarshal(raw, &config) != nil || config == nil {
				return fmt.Errorf("invalid local Docker configuration")
			}
		} else if !errors.Is(err, os.ErrNotExist) {
			return err
		}
		// Docker contexts and explicit builders are user-owned metadata; borrow them
		// without copying their TLS keys into results or mutating their configuration.
		contexts := filepath.Join(root, "contexts")
		if info, err := os.Stat(contexts); err == nil && info.IsDir() {
			if err := os.Symlink(contexts, filepath.Join(s.path, "contexts")); err != nil {
				return err
			}
		}
		if s.selectedBuilder != "" {
			s.localEnv["BUILDX_CONFIG"] = filepath.Join(root, "buildx")
		}
	}
	if selectedContext == "" {
		selectedContext, _ = config["currentContext"].(string)
	}
	s.localEnv["DOCKER_CONTEXT"] = selectedContext
	for _, value := range object(config, "auths") {
		a, _ := value.(map[string]any)
		for _, key := range []string{"auth", "identitytoken", "registrytoken"} {
			if value, ok := a[key].(string); ok && value != "" {
				s.secrets = append(s.secrets, value)
				if key == "auth" {
					if decoded, err := base64.StdEncoding.DecodeString(value); err == nil {
						s.secrets = append(s.secrets, string(decoded))
						if _, password, ok := strings.Cut(string(decoded), ":"); ok {
							s.secrets = append(s.secrets, password)
						}
					}
				}
			}
		}
	}
	if auth := s.request.RegistryAuth; auth != nil {
		auths := object(config, "auths")
		if auths == nil {
			auths = map[string]any{}
		}
		auths[s.request.RegistryHost] = map[string]any{"auth": base64.StdEncoding.EncodeToString([]byte(auth.Username + ":" + auth.Password))}
		config["auths"] = auths
	}
	dirs := []string{}
	if values, ok := config["cliPluginsExtraDirs"].([]any); ok {
		for _, v := range values {
			if dir, ok := v.(string); ok {
				dirs = append(dirs, dir)
			}
		}
	}
	if root != "" {
		dirs = append(dirs, filepath.Join(root, "cli-plugins"))
	}
	dirs = append(dirs, s.executor.PluginDirs...)
	config["cliPluginsExtraDirs"] = dirs
	raw, err := json.Marshal(config)
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(s.path, "config.json"), raw, 0600)
}
