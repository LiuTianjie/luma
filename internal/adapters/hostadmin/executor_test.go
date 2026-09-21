package hostadmin

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/LiuTianjie/luma/internal/node"
)

type runFunc func(context.Context, node.Command, func(string)) (node.CommandResult, error)

func (f runFunc) Run(ctx context.Context, c node.Command, emit func(string)) (node.CommandResult, error) {
	return f(ctx, c, emit)
}
func task(action string, payload any) node.Task {
	data, _ := json.Marshal(payload)
	return node.Task{Action: action, Payload: data}
}
func writeFixture(t *testing.T, root, path, text string) {
	t.Helper()
	path = filepath.Join(root, path)
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(text), 0600); err != nil {
		t.Fatal(err)
	}
}
func readFixture(t *testing.T, root, path string) string {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(root, path))
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}
func testExecutor(t *testing.T, platform string, f runFunc) Executor {
	t.Helper()
	return Executor{Platform: platform, Root: t.TempDir(), Privileged: true, Docker: "docker", Lookup: func(name string) string { return name }, Runner: f}
}

func TestDockerRegistryRestartAndIdempotency(t *testing.T) {
	var calls []string
	e := testExecutor(t, "linux", func(_ context.Context, c node.Command, _ func(string)) (node.CommandResult, error) {
		calls = append(calls, strings.Join(c.Args, " "))
		if c.Args[0] == "docker" {
			return node.CommandResult{Output: "alloc-b\nalloc-a\nalloc-a\n<no value>\n"}, nil
		}
		return node.CommandResult{}, nil
	})
	path := "etc/docker/daemon.json"
	writeFixture(t, e.Root, path, `{"data-root":"/srv/docker","insecure-registries":["old.test:5000"]}`)
	request := task("configure-insecure-registry", map[string]string{"registry": "new.test:5000"})
	out, err := e.Execute(context.Background(), request, nil)
	if err != nil {
		t.Fatal(err)
	}
	var got struct {
		Changed, DockerRestarted bool
		AffectedAllocationIds    []string
	}
	if json.Unmarshal(out.Result, &got) != nil || !got.Changed || !got.DockerRestarted || !reflect.DeepEqual(got.AffectedAllocationIds, []string{"alloc-a", "alloc-b"}) {
		t.Fatal(string(out.Result))
	}
	if len(calls) != 3 || calls[1] != "systemctl restart docker" || calls[2] != "systemctl is-active --quiet docker" {
		t.Fatal(calls)
	}
	if !strings.Contains(readFixture(t, e.Root, path), `"data-root": "/srv/docker"`) {
		t.Fatal("unrelated config removed")
	}
	calls = nil
	out, err = e.Execute(context.Background(), request, nil)
	if err != nil || len(calls) != 0 {
		t.Fatalf("unchanged config restarted Docker: %v %v", calls, err)
	}
	if json.Unmarshal(out.Result, &got) != nil || got.Changed || got.DockerRestarted || len(got.AffectedAllocationIds) != 0 {
		t.Fatal(string(out.Result))
	}
}

func TestDockerProxyPrivateEscapedAndRedacted(t *testing.T) {
	e := testExecutor(t, "linux", func(_ context.Context, c node.Command, _ func(string)) (node.CommandResult, error) {
		if strings.Contains(strings.Join(c.Args, " "), "secret") {
			t.Fatal("proxy password in argv")
		}
		return node.CommandResult{}, nil
	})
	p := map[string]string{"proxy": "http://user:p%25secret@proxy.test:7890", "noProxy": "localhost,10.0.0.0/8"}
	out, err := e.Execute(context.Background(), task("configure-docker-egress-proxy", p), nil)
	if err != nil {
		t.Fatal(err)
	}
	path := "etc/systemd/system/docker.service.d/http-proxy.conf"
	config := readFixture(t, e.Root, path)
	if !strings.Contains(config, "p%%25secret") || strings.Contains(string(out.Result), "secret") {
		t.Fatalf("escaping or result redaction failed: %s", out.Result)
	}
	info, _ := os.Stat(filepath.Join(e.Root, path))
	if info.Mode().Perm() != 0600 {
		t.Fatal("proxy config is not private")
	}
}

func TestDockerRestartFailureRestoresOriginalAndRecovers(t *testing.T) {
	restarts := 0
	e := testExecutor(t, "linux", func(_ context.Context, c node.Command, _ func(string)) (node.CommandResult, error) {
		if len(c.Args) > 1 && c.Args[1] == "restart" {
			restarts++
			if restarts == 1 {
				return node.CommandResult{Code: 1}, nil
			}
		}
		return node.CommandResult{}, nil
	})
	path := "etc/docker/daemon.json"
	original := `{"debug":true}`
	writeFixture(t, e.Root, path, original)
	_, err := e.Execute(context.Background(), task("configure-insecure-registry", map[string]string{"registry": "new.test"}), nil)
	if err == nil || !strings.Contains(err.Error(), "original configuration restored") || restarts != 2 {
		t.Fatalf("%v restarts=%d", err, restarts)
	}
	if got := readFixture(t, e.Root, path); got != original {
		t.Fatal(got)
	}
	if _, err := os.Stat(filepath.Join(e.Root, path+".luma-go-apply")); !os.IsNotExist(err) {
		t.Fatal("successful rollback left pending activation")
	}
}

func TestUnfinishedActivationRetriesDespiteIdenticalBytes(t *testing.T) {
	restarts := 0
	e := testExecutor(t, "linux", func(_ context.Context, c node.Command, _ func(string)) (node.CommandResult, error) {
		if len(c.Args) > 1 && c.Args[1] == "restart" {
			restarts++
		}
		return node.CommandResult{}, nil
	})
	path := "etc/docker/daemon.json"
	writeFixture(t, e.Root, path, `{"insecure-registries":["new.test"]}`)
	writeFixture(t, e.Root, path+".luma-go-apply", "pending")
	out, err := e.Execute(context.Background(), task("configure-insecure-registry", map[string]string{"registry": "new.test"}), nil)
	if err != nil || restarts != 1 || !strings.Contains(string(out.Result), `"dockerRestarted":true`) {
		t.Fatalf("%s %v", out.Result, err)
	}
}

func TestInvalidDockerConfigNeverMutatesOrRestarts(t *testing.T) {
	e := testExecutor(t, "linux", func(context.Context, node.Command, func(string)) (node.CommandResult, error) {
		t.Fatal("process ran despite invalid config")
		return node.CommandResult{}, nil
	})
	path := "etc/docker/daemon.json"
	writeFixture(t, e.Root, path, "{broken")
	if _, err := e.Execute(context.Background(), task("configure-insecure-registry", map[string]string{"registry": "new.test"}), nil); err == nil {
		t.Fatal("accepted malformed daemon JSON")
	}
	if readFixture(t, e.Root, path) != "{broken" {
		t.Fatal("clobbered existing configuration")
	}
	if err := os.Remove(filepath.Join(e.Root, path)); err != nil {
		t.Fatal(err)
	}
	outside := filepath.Join(t.TempDir(), "keep")
	if err := os.WriteFile(outside, []byte("{}"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(e.Root, path)); err != nil {
		t.Fatal(err)
	}
	if _, err := e.Execute(context.Background(), task("configure-insecure-registry", map[string]string{"registry": "new.test"}), nil); err == nil {
		t.Fatal("followed config symlink")
	}
}

func TestActivationFailureKeepsMarkerWhenRecoveryFails(t *testing.T) {
	e := testExecutor(t, "linux", func(_ context.Context, c node.Command, _ func(string)) (node.CommandResult, error) {
		if len(c.Args) > 1 && c.Args[1] == "restart" {
			return node.CommandResult{Code: 1}, nil
		}
		return node.CommandResult{}, nil
	})
	path := "etc/docker/daemon.json"
	writeFixture(t, e.Root, path, `{"debug":true}`)
	_, err := e.Execute(context.Background(), task("configure-insecure-registry", map[string]string{"registry": "new.test"}), nil)
	if err == nil || !strings.Contains(err.Error(), "service recovery failed") {
		t.Fatal(err)
	}
	if got := readFixture(t, e.Root, path); got != `{"debug":true}` {
		t.Fatal(got)
	}
	if _, err := os.Stat(filepath.Join(e.Root, path+".luma-go-apply")); err != nil {
		t.Fatal("lost unfinished activation marker")
	}
}

func TestRollbackDoesNotOverwriteConcurrentAdministratorEdit(t *testing.T) {
	var root string
	e := testExecutor(t, "linux", func(_ context.Context, c node.Command, _ func(string)) (node.CommandResult, error) {
		if len(c.Args) > 1 && c.Args[1] == "restart" {
			writeFixture(t, root, "etc/docker/daemon.json", `{"data-root":"/admin-change"}`)
			return node.CommandResult{Code: 1}, nil
		}
		return node.CommandResult{}, nil
	})
	root = e.Root
	writeFixture(t, root, "etc/docker/daemon.json", `{"debug":true}`)
	_, err := e.Execute(context.Background(), task("configure-insecure-registry", map[string]string{"registry": "new.test"}), nil)
	if err == nil || !strings.Contains(err.Error(), "preserving the newer file") {
		t.Fatal(err)
	}
	if got := readFixture(t, root, "etc/docker/daemon.json"); got != `{"data-root":"/admin-change"}` {
		t.Fatal("overwrote administrator configuration")
	}
}

func TestCanceledActivationStillRestoresUsingRecoveryContext(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	attempts := 0
	e := testExecutor(t, "linux", func(ctx context.Context, c node.Command, _ func(string)) (node.CommandResult, error) {
		if len(c.Args) > 1 && c.Args[1] == "restart" {
			attempts++
			if attempts == 1 {
				cancel()
				return node.CommandResult{}, ctx.Err()
			}
			if ctx.Err() != nil {
				t.Fatal("recovery inherited cancellation")
			}
		}
		return node.CommandResult{}, nil
	})
	writeFixture(t, e.Root, "etc/docker/daemon.json", `{"debug":true}`)
	_, err := e.Execute(ctx, task("configure-insecure-registry", map[string]string{"registry": "new.test"}), nil)
	if err == nil || attempts != 2 {
		t.Fatalf("%v restarts %d", err, attempts)
	}
	if got := readFixture(t, e.Root, "etc/docker/daemon.json"); got != `{"debug":true}` {
		t.Fatal(got)
	}
}

func TestHostActionsRequirePrivilegeAndPlatformSupport(t *testing.T) {
	e := testExecutor(t, "darwin", func(context.Context, node.Command, func(string)) (node.CommandResult, error) {
		t.Fatal("unexpected host tool")
		return node.CommandResult{}, nil
	})
	if actions := e.SupportedActions(); len(actions) != 0 {
		t.Fatal(actions)
	}
	e.Privileged = false
	if len(e.Capabilities()) != 0 || len(e.SupportedActions()) != 0 {
		t.Fatal("unprivileged agent advertises host administration")
	}
	if _, err := e.Execute(context.Background(), task("configure-insecure-registry", map[string]string{"registry": "local.test"}), nil); err == nil {
		t.Fatal("unprivileged mutation allowed")
	}
	entries, _ := os.ReadDir(e.Root)
	if len(entries) != 0 {
		t.Fatal("unprivileged task touched filesystem")
	}
}

func TestNFSIsRetiredOnEveryPlatform(t *testing.T) {
	for _, platform := range []string{"linux", "darwin"} {
		t.Run(platform, func(t *testing.T) {
			e := testExecutor(t, platform, func(context.Context, node.Command, func(string)) (node.CommandResult, error) {
				t.Fatal("retired NFS action invoked a host tool")
				return node.CommandResult{}, nil
			})
			for _, capability := range e.Capabilities() {
				if strings.Contains(capability, "nfs") {
					t.Fatal("NFS capability remains advertised")
				}
			}
			for _, action := range e.SupportedActions() {
				if strings.Contains(action, "nfs") {
					t.Fatal("NFS action remains registered")
				}
			}
			for _, action := range []string{"prepare-managed-nfs-host", "remove-managed-nfs-export"} {
				if _, err := e.Execute(context.Background(), task(action, map[string]string{"name": "data", "path": "/srv/data"}), nil); err == nil {
					t.Fatal("retired NFS action succeeded")
				}
			}
			entries, _ := os.ReadDir(e.Root)
			if len(entries) != 0 {
				t.Fatal("retired NFS action modified host files")
			}
		})
	}
}
