package imageexec

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/LiuTianjie/luma/internal/node"
)

type runnerFunc func(context.Context, node.Command, func(string)) (node.CommandResult, error)

func (f runnerFunc) Run(ctx context.Context, cmd node.Command, emit func(string)) (node.CommandResult, error) {
	return f(ctx, cmd, emit)
}
func imageTask(action string, payload any) node.Task {
	data, _ := json.Marshal(payload)
	return node.Task{ID: "task", Action: action, Payload: data}
}
func decoded(t *testing.T, outcome node.Outcome) map[string]any {
	t.Helper()
	var r map[string]any
	if err := json.Unmarshal(outcome.Result, &r); err != nil {
		t.Fatal(err)
	}
	return r
}

func TestResolveExistingAndPlatformPull(t *testing.T) {
	for _, platform := range []string{"", "linux/arm64"} {
		t.Run(platform, func(t *testing.T) {
			root := t.TempDir()
			calls := []node.Command{}
			var authPath string
			digest := "registry.test/app@sha256:" + strings.Repeat("a", 64)
			runner := runnerFunc(func(_ context.Context, c node.Command, _ func(string)) (node.CommandResult, error) {
				calls = append(calls, c)
				authPath = c.Env["DOCKER_CONFIG"]
				for _, path := range []string{authPath, filepath.Join(authPath, "config.json")} {
					info, err := os.Stat(path)
					if err != nil || info.Mode().Perm()&0077 != 0 {
						t.Fatalf("nonprivate credentials %s %v", path, err)
					}
				}
				data, err := os.ReadFile(filepath.Join(authPath, "config.json"))
				if err != nil || !strings.Contains(string(data), base64.StdEncoding.EncodeToString([]byte("user:secret"))) {
					t.Fatalf("missing scoped auth %v", err)
				}
				if strings.Contains(strings.Join(c.Args, " "), "secret") {
					t.Fatal("credential in argv")
				}
				if c.Args[1] == "pull" {
					return node.CommandResult{}, nil
				}
				return node.CommandResult{Output: `["` + digest + `"]`}, nil
			})
			e := Executor{Runner: runner, Docker: "docker", Scratch: root}
			out, err := e.Execute(context.Background(), imageTask("resolve-docker-image", pullPayload{Image: "registry.test/app:v1", Platform: platform, RegistryAuth: &registryAuth{Username: "user", Password: "secret", ServerAddress: "registry.test"}}), nil)
			if err != nil {
				t.Fatal(err)
			}
			got := decoded(t, out)
			if got["digest"] != digest || got["deployed"] != digest || got["pulled"] != (platform != "") {
				t.Fatal(got)
			}
			if platform == "" && len(calls) != 1 {
				t.Fatalf("local image unexpectedly pulled: %v", calls)
			}
			if platform != "" && (len(calls) != 2 || !reflect.DeepEqual(calls[0].Args, []string{"docker", "pull", "--platform", platform, "registry.test/app:v1"})) {
				t.Fatal(calls)
			}
			if _, err := os.Stat(authPath); !os.IsNotExist(err) {
				t.Fatal("credential directory survived task")
			}
		})
	}
}

func TestDiagnosticFailureIsAResultAndRedactsCredentials(t *testing.T) {
	root := t.TempDir()
	var events []node.Event
	encoded := base64.StdEncoding.EncodeToString([]byte("user:top-secret"))
	runner := runnerFunc(func(_ context.Context, c node.Command, emit func(string)) (node.CommandResult, error) {
		if c.Timeout != 30*time.Second {
			t.Fatalf("timeout not clamped: %s", c.Timeout)
		}
		if len(c.Secrets) < 3 {
			t.Fatal("runner missing redaction secrets")
		}
		emit("error top-secret " + encoded)
		return node.CommandResult{Code: 1, Output: "denied top-secret\r\n" + encoded + "\n"}, nil
	})
	e := Executor{Runner: runner, Docker: "docker", Scratch: root}
	out, err := e.Execute(context.Background(), imageTask("diagnose-docker-pull", pullPayload{Image: "registry.test/app:v1", Timeout: 1, RegistryAuth: &registryAuth{Username: "user", Password: "top-secret", ServerAddress: "registry.test"}}), func(event node.Event) { events = append(events, event) })
	if err != nil {
		t.Fatal(err)
	}
	got := decoded(t, out)
	if got["ok"] != false || got["exitCode"] != float64(1) {
		t.Fatal(got)
	}
	for _, secret := range []string{"top-secret", encoded} {
		if strings.Contains(string(out.Result), secret) || strings.Contains(events[0].Line, secret) {
			t.Fatal("secret leaked")
		}
	}
	entries, _ := os.ReadDir(root)
	if len(entries) != 0 {
		t.Fatal("credential files retained")
	}
}

func TestMirrorUsesBothAuthsThenVerifiesDigest(t *testing.T) {
	root := t.TempDir()
	calls := []node.Command{}
	digest := "sha256:" + strings.Repeat("b", 64)
	runner := runnerFunc(func(_ context.Context, c node.Command, _ func(string)) (node.CommandResult, error) {
		calls = append(calls, c)
		data, err := os.ReadFile(filepath.Join(c.Env["DOCKER_CONFIG"], "config.json"))
		if err != nil {
			t.Fatal(err)
		}
		var config struct{ Auths map[string]any }
		if json.Unmarshal(data, &config) != nil || len(config.Auths) != 2 {
			t.Fatalf("scoped source+destination auth missing: %s", data)
		}
		if c.Env["HTTPS_PROXY"] != "http://proxy.test:7890" || c.Env["NO_PROXY"] != "localhost,127.0.0.1,::1,127.0.0.1:5000,registry.test" {
			t.Fatal(c.Env)
		}
		if c.Args[1] == "digest" {
			return node.CommandResult{Output: digest + "\n"}, nil
		}
		return node.CommandResult{}, nil
	})
	e := Executor{Runner: runner, Crane: "crane", Scratch: root}
	out, err := e.Execute(context.Background(), imageTask("cache-runtime-image", mirrorPayload{Source: "private.test/app:v1", Push: "127.0.0.1:5000/luma-cache/app:v1", Destination: "registry.test/luma-cache/app:v1", Platform: "linux/arm64", Insecure: true, Proxy: "http://proxy.test:7890", SourceAuth: &registryAuth{Username: "source", Password: "source-secret", ServerAddress: "private.test"}, DestinationAuth: &registryAuth{Username: "destination", Password: "dest-secret", LegacyAddress: "127.0.0.1:5000"}}), nil)
	if err != nil {
		t.Fatal(err)
	}
	if decoded(t, out)["digest"] != digest {
		t.Fatal(string(out.Result))
	}
	if len(calls) != 2 || !reflect.DeepEqual(calls[0].Args, []string{"crane", "copy", "private.test/app:v1", "127.0.0.1:5000/luma-cache/app:v1", "--platform", "linux/arm64", "--insecure"}) || calls[1].Args[1] != "digest" {
		t.Fatal(calls)
	}
	entries, _ := os.ReadDir(root)
	if len(entries) != 0 {
		t.Fatal("credentials retained")
	}
}

func TestMirrorFailureCancellationAndCleanup(t *testing.T) {
	p := mirrorPayload{Source: "nginx:alpine", Push: "127.0.0.1:5000/luma-control:v1", Destination: "registry.test/luma-control:v1"}
	for _, mode := range []string{"cancel-retry", "bad-digest", "unauthorized", "process-error"} {
		t.Run(mode, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			root := t.TempDir()
			calls := 0
			runner := runnerFunc(func(_ context.Context, c node.Command, _ func(string)) (node.CommandResult, error) {
				calls++
				switch mode {
				case "cancel-retry":
					cancel()
					return node.CommandResult{Code: 1, Output: "EOF"}, nil
				case "unauthorized":
					return node.CommandResult{Code: 1, Output: "unauthorized"}, nil
				case "process-error":
					return node.CommandResult{}, context.DeadlineExceeded
				default:
					if c.Args[1] == "digest" {
						return node.CommandResult{Output: "invalid"}, nil
					}
					return node.CommandResult{}, nil
				}
			})
			e := Executor{Runner: runner, Crane: "crane", Scratch: root}
			start := time.Now()
			_, err := e.Execute(ctx, imageTask("mirror-control-image", p), nil)
			if err == nil {
				t.Fatal("failure reported as success")
			}
			if mode == "cancel-retry" && (!errors.Is(err, context.Canceled) || time.Since(start) > time.Second) {
				t.Fatal(err)
			}
			if mode != "bad-digest" && calls != 1 {
				t.Fatal("nontransient or canceled operation retried")
			}
			entries, _ := os.ReadDir(root)
			if len(entries) != 0 {
				t.Fatal("credentials retained after failure")
			}
		})
	}
}

func TestConflictingAuthFailsBeforeAnyProcess(t *testing.T) {
	root := t.TempDir()
	_, err := newAuthSession(root, &registryAuth{Username: "a", Password: "one", ServerAddress: "same.test"}, &registryAuth{Username: "a", Password: "two", ServerAddress: "same.test"})
	if err == nil {
		t.Fatal("ambiguous credential silently overwritten")
	}
	entries, _ := os.ReadDir(root)
	if len(entries) != 0 {
		t.Fatal("credentials written despite validation failure")
	}
}
