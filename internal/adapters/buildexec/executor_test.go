package buildexec

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/node"
	"go.yaml.in/yaml/v3"
)

type runnerFunc func(context.Context, node.Command, func(string)) (node.CommandResult, error)

func (f runnerFunc) Run(ctx context.Context, c node.Command, emit func(string)) (node.CommandResult, error) {
	return f(ctx, c, emit)
}
func write(t *testing.T, root, path, value string) {
	t.Helper()
	path = filepath.Join(root, path)
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(value), 0600); err != nil {
		t.Fatal(err)
	}
}
func fixtureExecutor(t *testing.T, f runnerFunc) Executor {
	t.Helper()
	root := t.TempDir()
	return Executor{Runner: f, Git: "git", Docker: "docker", Scratch: root, Persistent: filepath.Join(root, "builder"), Buildx: true}
}
func TestBuildNativeUsesManifestPlatformAndCleansCredentialsAndBuilder(t *testing.T) {
	var commands []node.Command
	var scratch string
	e := fixtureExecutor(t, func(ctx context.Context, c node.Command, emit func(string)) (node.CommandResult, error) {
		commands = append(commands, c)
		for _, arg := range c.Args {
			if strings.Contains(arg, "git-secret") || strings.Contains(arg, "registry-secret") {
				t.Fatal("credential in argv")
			}
		}
		if c.Args[0] == "git" && c.Args[1] == "clone" {
			root := c.Args[len(c.Args)-1]
			scratch = filepath.Dir(root)
			write(t, root, ".luma.yml", "name: app\nnode: arm\nregion: cn\nbuild:\n  context: app\n  dockerfile: app/Dockerfile\n  repo: custom/app\n")
			write(t, root, "app/Dockerfile", "FROM scratch\n")
			if data, err := os.ReadFile(c.Env["LUMA_GIT_PASSWORD_FILE"]); err != nil || string(data) != "git-secret" {
				t.Fatal(string(data), err)
			}
		}
		if c.Args[0] == "git" && c.Args[3] == "rev-parse" {
			return node.CommandResult{Output: "1234567\n"}, nil
		}
		if c.Args[0] == "docker" && c.Args[1] == "buildx" && c.Args[2] == "build" {
			if _, err := os.Stat(filepath.Join(scratch, "git-password")); !errors.Is(err, os.ErrNotExist) {
				t.Fatal("Git credential survived checkout")
			}
			args := strings.Join(c.Args, " ")
			if !strings.Contains(args, "--platform linux/arm64") || !strings.Contains(args, "-t localhost:5000/custom/app:1234567") {
				t.Fatal(args)
			}
			if c.Env["HTTP_PROXY"] != "" {
				t.Fatal("direct build inherited a proxy")
			}
		}
		return node.CommandResult{}, nil
	})
	r := builddomain.ImageRequest{RepoURL: "https://git.test/app", Repo: "default/app", RegistryHost: "registry.test:5000", PushHost: "localhost:5000", GitToken: "git-secret", RegistryAuth: &builddomain.RegistryAuth{Username: "robot", Password: "registry-secret", ServerAddress: "registry.test:5000"}, TargetPlatformsByNode: map[string]string{"arm": "linux/arm64"}}
	raw, _ := json.Marshal(r)
	out, err := e.Execute(context.Background(), node.Task{Action: "build-image", Payload: raw}, nil)
	if err != nil {
		t.Fatal(err)
	}
	var result builddomain.ImageResult
	if json.Unmarshal(out.Result, &result) != nil || result.Image != "registry.test:5000/custom/app:1234567" {
		t.Fatal(string(out.Result))
	}
	if _, err := os.Stat(scratch); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("task scratch survived", err)
	}
	if _, err := os.Stat(filepath.Join(e.Persistent, "active")); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("active build marker survived", err)
	}
	if !strings.Contains(strings.Join(commands[len(commands)-2].Args, " "), "buildx rm --force --keep-state") {
		t.Fatal(commands)
	}
}
func TestComposeBuildPlansAndSharedImageAliases(t *testing.T) {
	root := t.TempDir()
	write(t, root, "stack/luma.compose.yml", "name: stack\nregion: cn\n")
	write(t, root, "stack/docker-compose.yml", "services:\n  web:\n    image: project:dev\n    build:\n      context: ../app\n      args: {COUNT: 3, ENABLED: true}\n  worker:\n    image: project:dev\n  external:\n    image: redis:7\n")
	write(t, root, "app/Dockerfile", "FROM scratch\n")
	s := session{request: builddomain.ImageRequest{Repo: "acme/app", RegistryHost: "registry.test:5000", TargetPlatformsByRegion: map[string][]string{"cn": {"linux/arm64"}}}}
	kind, path, err := discover(root, "")
	if err != nil {
		t.Fatal(err)
	}
	p, err := s.plan(root, "1234567", kind, path)
	if err != nil || len(p.images) != 1 || p.images[0].platform != "linux/arm64" || p.images[0].args["COUNT"] != "3" {
		t.Fatal(p, err)
	}
	result, err := p.result("1234567")
	if err != nil {
		t.Fatal(err)
	}
	var composed map[string]any
	if yaml.Unmarshal([]byte(result.ComposeContent), &composed) != nil {
		t.Fatal("bad result")
	}
	services := object(composed, "services")
	if text(object(services, "worker"), "image") != result.Image || text(object(services, "external"), "image") != "redis:7" || object(services, "web")["build"] != nil {
		t.Fatal(result.ComposeContent)
	}
	write(t, root, ".luma.yml", "name: root\nregion: cn\n")
	if kind, _, err := discover(root, ""); err != nil || kind != "service" {
		t.Fatal(kind, err)
	}
	if kind, _, err := discover(root, "stack/luma.compose.yml"); err != nil || kind != "compose" {
		t.Fatal(kind, err)
	}
}
func TestRepositoryPathsAndBuildArgsDoNotEscapeOrInherit(t *testing.T) {
	root := t.TempDir()
	other := t.TempDir()
	write(t, other, "Dockerfile", "FROM scratch")
	if err := os.Symlink(other, filepath.Join(root, "outside")); err != nil {
		t.Fatal(err)
	}
	if _, err := repoPath(root, root, "outside/Dockerfile"); err == nil {
		t.Fatal("accepted escaping symlink")
	}
	for _, raw := range []any{[]any{"TOKEN"}, map[string]any{"TOKEN": nil}, map[string]any{"TOKEN": []any{"a"}}} {
		if _, err := buildArgs(raw); err == nil {
			t.Fatal("accepted inherited/nonliteral build arg")
		}
	}
}
func TestCanceledBuildStillStopsOwnedBuildKit(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	cleaned := false
	e := fixtureExecutor(t, func(c context.Context, cmd node.Command, _ func(string)) (node.CommandResult, error) {
		if cmd.Args[0] == "git" && cmd.Args[1] == "clone" {
			write(t, cmd.Args[len(cmd.Args)-1], "Dockerfile", "FROM scratch")
		}
		if cmd.Args[0] == "git" && cmd.Args[3] == "rev-parse" {
			return node.CommandResult{Output: "1234567"}, nil
		}
		if cmd.Args[0] == "docker" && cmd.Args[1] == "buildx" && cmd.Args[2] == "build" {
			cancel()
			return node.CommandResult{}, context.Canceled
		}
		if cmd.Args[0] == "docker" && cmd.Args[1] == "buildx" && cmd.Args[2] == "rm" {
			if c.Err() != nil {
				t.Fatal("cleanup inherited cancellation")
			}
			cleaned = true
		}
		return node.CommandResult{}, nil
	})
	payload := json.RawMessage(`{"repoUrl":"https://git.test/app","repo":"acme/app","registryHost":"registry.test:5000"}`)
	_, err := e.Execute(ctx, node.Task{Action: "build-image", Payload: payload}, nil)
	if !errors.Is(err, context.Canceled) || !cleaned {
		t.Fatal(err, cleaned)
	}
}
func TestRecoverOnlyThisAgentsOwnedBuilder(t *testing.T) {
	calls := []string{}
	e := fixtureExecutor(t, func(_ context.Context, c node.Command, _ func(string)) (node.CommandResult, error) {
		calls = append(calls, strings.Join(c.Args, " "))
		return node.CommandResult{}, nil
	})
	if err := e.ensurePersistent(); err != nil {
		t.Fatal(err)
	}
	write(t, e.Persistent, "active", e.builderName())
	if err := e.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(calls) != 2 || !strings.HasSuffix(calls[0], e.builderName()) {
		t.Fatal(calls)
	}
	calls = nil
	if err := e.Recover(context.Background()); err != nil || len(calls) > 0 {
		t.Fatal(err, calls)
	}
}

func TestCleanupFailureDoesNotReportBuildSuccessAndRetainsOwnership(t *testing.T) {
	e := fixtureExecutor(t, func(ctx context.Context, c node.Command, _ func(string)) (node.CommandResult, error) {
		if c.Args[0] == "git" && c.Args[1] == "clone" {
			write(t, c.Args[len(c.Args)-1], "Dockerfile", "FROM scratch")
		}
		if c.Args[0] == "git" && c.Args[3] == "rev-parse" {
			return node.CommandResult{Output: "1234567"}, nil
		}
		if c.Args[0] == "docker" && c.Args[1] == "buildx" && c.Args[2] == "rm" {
			return node.CommandResult{Code: 1, Output: "Docker unavailable"}, nil
		}
		if c.Args[0] == "docker" && c.Args[1] == "ps" {
			return node.CommandResult{Code: 1, Output: "Docker unavailable"}, nil
		}
		return node.CommandResult{}, nil
	})
	payload := json.RawMessage(`{"repoUrl":"https://git.test/app","repo":"acme/app","registryHost":"registry.test:5000"}`)
	out, err := e.Execute(context.Background(), node.Task{Action: "build-image", Payload: payload}, nil)
	if err == nil || len(out.Result) > 0 {
		t.Fatal("reported success before cleanup", out, err)
	}
	if raw, err := os.ReadFile(filepath.Join(e.Persistent, "active")); err != nil || string(raw) != e.builderName() {
		t.Fatal(string(raw), err)
	}
}

func TestAmbiguousSourceImageDoesNotRewriteUnrelatedConsumer(t *testing.T) {
	root := t.TempDir()
	write(t, root, "luma.compose.yml", "name: stack\nregion: cn\n")
	write(t, root, "docker-compose.yml", "services:\n  one:\n    image: shared:dev\n    build: .\n  two:\n    image: shared:dev\n    build: .\n  worker:\n    image: shared:dev\n")
	write(t, root, "Dockerfile", "FROM scratch\n")
	s := session{request: builddomain.ImageRequest{Repo: "acme/app", RegistryHost: "registry.test:5000"}}
	p, err := s.plan(root, "1234567", "compose", filepath.Join(root, "luma.compose.yml"))
	if err != nil {
		t.Fatal(err)
	}
	r, err := p.result("1234567")
	if err != nil {
		t.Fatal(err)
	}
	if len(r.ImageAliases) > 0 || r.Images["one"] == r.Images["two"] {
		t.Fatal(r)
	}
	var doc map[string]any
	if yaml.Unmarshal([]byte(r.ComposeContent), &doc) != nil || text(object(object(doc, "services"), "worker"), "image") != "shared:dev" {
		t.Fatal(r.ComposeContent)
	}
}
