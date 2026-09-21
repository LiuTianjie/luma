package buildexec

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/node"
)

func TestInspectLocalUsesWorkingTreeAndMixedComposeTargets(t *testing.T) {
	root := t.TempDir()
	root, _ = filepath.EvalSymlinks(root)
	write(t, root, ".luma.yml", "name: local-app\nnode: local-node\nregion: cn\n")
	e := fixtureExecutor(t, func(ctx context.Context, c node.Command, emit func(string)) (node.CommandResult, error) {
		if c.Args[0] != "git" || c.Args[1] != "-C" || c.Args[2] != root {
			t.Fatal(c.Args)
		}
		args := strings.Join(c.Args[3:], " ")
		switch args {
		case "remote get-url origin":
			return node.CommandResult{Output: "git@git.test:team/app.git\n"}, nil
		case "rev-parse HEAD":
			return node.CommandResult{Output: strings.Repeat("a", 40)}, nil
		case "status --porcelain":
			return node.CommandResult{Output: " M Dockerfile\n?? local.txt"}, nil
		default:
			t.Fatal(args)
			return node.CommandResult{}, nil
		}
	})
	source, err := e.InspectLocal(context.Background(), root, "", "", "global")
	if err != nil || source.RepoURL != "git@git.test:team/app.git" || !strings.HasSuffix(source.Revision, "-dirty") || source.Name != "local-app" || len(source.TargetNodes) != 1 || len(source.TargetRegions) != 0 {
		t.Fatal(source, err)
	}
	write(t, root, "stack/luma.compose.yml", "name: local-stack\nregion: cn\nservices:\n  web: {node: worker}\n  extra: {region: home}\n")
	write(t, root, "stack/docker-compose.yml", "services:\n  web: {build: .}\n  extra: {image: busybox:1}\n  other: {build: .}\n")
	source, err = e.InspectLocal(context.Background(), root, "team/app", "stack/luma.compose.yml", "global")
	if err != nil || source.Kind != "compose" || source.RepoURL != "https://github.com/team/app" || strings.Join(source.TargetNodes, ",") != "worker" || strings.Join(source.TargetRegions, ",") != "global,home" {
		t.Fatal(source, err)
	}
}
func TestLocalBuildReservesRepositoryAndBorrowsDockerBuilder(t *testing.T) {
	t.Setenv("DOCKER_CONTEXT", "")
	for _, selected := range []bool{false, true} {
		t.Run(map[bool]string{false: "owned", true: "selected"}[selected], func(t *testing.T) {
			root := t.TempDir()
			root, _ = filepath.EvalSymlinks(root)
			config := t.TempDir()
			write(t, root, ".luma.yml", "name: app\nregion: global\nbuild: {repo: ignored/override}\n")
			write(t, root, "Dockerfile", "FROM scratch\nCOPY local.txt /local.txt\n")
			write(t, root, "local.txt", "uncommitted file")
			secret := "private-docker-password"
			configText := `{"currentContext":"desktop-linux","auths":{"registry.test:5000":{"auth":"` + base64.StdEncoding.EncodeToString([]byte("robot:"+secret)) + `"}},"credsStore":"desktop"}`
			write(t, config, "config.json", configText)
			write(t, config, "contexts/meta/example/meta.json", "{}")
			write(t, config, "buildx/instances/borrowed", "untouched")
			var commands []node.Command
			var scratch string
			var output []string
			e := fixtureExecutor(t, func(ctx context.Context, c node.Command, emit func(string)) (node.CommandResult, error) {
				commands = append(commands, c)
				if c.Args[0] != "docker" {
					t.Fatal("local build invoked Git", c.Args)
				}
				if c.Env["DOCKER_CONTEXT"] != "desktop-linux" {
					t.Fatal(c.Env)
				}
				if c.Args[1] == "buildx" && c.Args[2] == "build" {
					scratch = c.Env["DOCKER_CONFIG"]
					if scratch == config {
						t.Fatal("using user config as writable scratch")
					}
					raw, err := os.ReadFile(filepath.Join(scratch, "config.json"))
					if err != nil {
						t.Fatal(err)
					}
					var copied map[string]any
					if json.Unmarshal(raw, &copied) != nil || copied["credsStore"] != "desktop" {
						t.Fatal(string(raw))
					}
					if _, err := os.Stat(filepath.Join(scratch, "contexts/meta/example/meta.json")); err != nil {
						t.Fatal(err)
					}
					if c.Args[len(c.Args)-1] != root || !strings.Contains(strings.Join(c.Args, " "), "registry.test:5000/team/app:local-0123456789abcdef") {
						t.Fatal(c.Args)
					}
					if strings.Contains(strings.Join(c.Args, " "), "ignored/override") {
						t.Fatal(c.Args)
					}
					data, _ := os.ReadFile(filepath.Join(c.Args[len(c.Args)-1], "local.txt"))
					if string(data) != "uncommitted file" {
						t.Fatal(string(data))
					}
					if selected && (c.Env["BUILDX_CONFIG"] != filepath.Join(config, "buildx") || c.Args[4] != "borrowed") {
						t.Fatal(c.Args, c.Env)
					}
					emit("tool output " + secret)
				}
				return node.CommandResult{}, nil
			})
			request := builddomain.LocalImageRequest{ImageRequest: builddomain.ImageRequest{RepoURL: "https://git.test/team/app", RegistryHost: "registry.test:5000", Repo: "team/app", Platform: "linux/arm64"}, Path: root, Tag: "local-0123456789abcdef", DockerConfig: config}
			if selected {
				request.Builder = "borrowed"
			}
			result, err := e.BuildLocal(context.Background(), request, func(event node.Event) { output = append(output, event.Line) })
			if err != nil || result.Image != "registry.test:5000/team/app:local-0123456789abcdef" {
				t.Fatal(result, err)
			}
			if strings.Contains(strings.Join(output, " "), secret) {
				t.Fatal("Docker credential in progress")
			}
			for _, c := range commands {
				if selected && (c.Args[1] != "buildx" || c.Args[2] != "build") {
					t.Fatal("mutated selected builder", c.Args)
				}
				if !selected && strings.Contains(strings.Join(c.Args, " "), "--keep-state") {
					t.Fatal("leaked ephemeral build cache", c.Args)
				}
			}
			if data, _ := os.ReadFile(filepath.Join(config, "config.json")); string(data) != configText {
				t.Fatal("user Docker config changed")
			}
			if data, _ := os.ReadFile(filepath.Join(config, "buildx/instances/borrowed")); string(data) != "untouched" {
				t.Fatal("selected builder changed")
			}
			if _, err := os.Stat(scratch); !errors.Is(err, os.ErrNotExist) {
				t.Fatal("scratch retained", err)
			}
		})
	}
}
func TestLocalComposeForcesReservedRepositoriesAndSharedAliases(t *testing.T) {
	root := t.TempDir()
	root, _ = filepath.EvalSymlinks(root)
	config := t.TempDir()
	write(t, root, "luma.compose.yml", "name: stack\nregion: global\n")
	write(t, root, "docker-compose.yml", "services:\n  web: {image: shared:dev, build: {context: web, repo: foreign/web}}\n  worker: {image: shared:dev}\n  other: {build: {context: other, x-luma-repo: foreign/other}}\n")
	write(t, root, "web/Dockerfile", "FROM scratch\n")
	write(t, root, "other/Dockerfile", "FROM scratch\n")
	e := fixtureExecutor(t, func(ctx context.Context, c node.Command, emit func(string)) (node.CommandResult, error) {
		return node.CommandResult{}, nil
	})
	result, err := e.BuildLocal(context.Background(), builddomain.LocalImageRequest{ImageRequest: builddomain.ImageRequest{RepoURL: "https://git.test/team/app", RegistryHost: "registry.test:5000", Repo: "team/app", Platform: "linux/arm64"}, Path: root, Tag: "local-0123456789abcdef", DockerConfig: config}, nil)
	if err != nil || len(result.Images) != 2 || result.Images["web"] != "registry.test:5000/team/app/web:local-0123456789abcdef" || strings.Count(result.ComposeContent, result.Images["web"]) != 2 || strings.Contains(result.ComposeContent, "build:") {
		t.Fatal(result, err)
	}
}
func TestLocalBuildFailureCleansOwnedResourcesAndLeavesSource(t *testing.T) {
	root := t.TempDir()
	root, _ = filepath.EvalSymlinks(root)
	config := t.TempDir()
	write(t, root, ".luma.yml", "name: app\nregion: global\n")
	write(t, root, "Dockerfile", "FROM scratch\n")
	removed := false
	e := fixtureExecutor(t, func(ctx context.Context, c node.Command, emit func(string)) (node.CommandResult, error) {
		if c.Args[1] == "buildx" && c.Args[2] == "build" {
			return node.CommandResult{Code: 1, Output: "compiler failed"}, nil
		}
		if c.Args[1] == "buildx" && c.Args[2] == "rm" {
			removed = true
		}
		return node.CommandResult{}, nil
	})
	_, err := e.BuildLocal(context.Background(), builddomain.LocalImageRequest{ImageRequest: builddomain.ImageRequest{RepoURL: "https://git.test/team/app", RegistryHost: "registry.test:5000", Repo: "team/app"}, Path: root, Tag: "local-0123456789abcdef", DockerConfig: config}, nil)
	if err == nil || !removed {
		t.Fatal(err, removed)
	}
	if _, err := os.Stat(filepath.Join(root, "Dockerfile")); err != nil {
		t.Fatal("source removed", err)
	}
	entries, err := os.ReadDir(e.Scratch)
	if err != nil || len(entries) != 0 {
		t.Fatal(entries, err)
	}
}
