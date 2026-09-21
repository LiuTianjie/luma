package cli

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/LiuTianjie/luma/internal/adapters/contexts"
)

func TestDeployWorkflowAndSubmission(t *testing.T) {
	root := t.TempDir()
	source := filepath.Join(root, "app.yaml")
	manifest := "name: app\nimage: app:1\nregion: cn\nenv:\n  TOKEN: '${TOKEN}'\n"
	if err := os.WriteFile(source, []byte(manifest), 0600); err != nil {
		t.Fatal(err)
	}
	config := filepath.Join(root, "config.yaml")
	if err := os.WriteFile(config, []byte("{}\n"), 0600); err != nil {
		t.Fatal(err)
	}
	var calls []string
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls = append(calls, r.URL.Path)
		if r.Header.Get("Authorization") != "Bearer token" {
			t.Error("missing credentials")
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		switch r.URL.Path {
		case "/v1/workflows/check":
			recipe := body["recipe"].(map[string]any)
			if recipe["method"] != "image-deploy" {
				t.Error("invalid recipe")
			}
			fmt.Fprint(w, `{"status":"match","workflow":{"revision":4}}`)
		case "/v1/deployments/stream":
			if body["manifest"] != manifest {
				t.Error("manifest modified or replaced with config")
			}
			fmt.Fprintln(w, `{"status":"start","message":"deploying"}`)
			fmt.Fprintln(w, `{"status":"done","result":{"service":"app","revision":3}}`)
		case "/v1/workflows":
			if body["expectedRevision"] != float64(4) {
				t.Error("revision not preserved")
			}
			fmt.Fprint(w, `{"workflow":{"name":"app"}}`)
		default:
			t.Errorf("unexpected route %s", r.URL.Path)
			w.WriteHeader(404)
		}
	}))
	defer server.Close()
	var out, errout bytes.Buffer
	code := Run(context.Background(), []string{"--config", config, "deploy", source, "--control-url", server.URL, "--token", "token", "--insecure", "--format", "ndjson"}, IO{In: strings.NewReader(""), Out: &out, Err: &errout, Getenv: func(key string) string {
		if key == "LUMA_CONFIG_HOME" {
			return filepath.Join(root, "contexts")
		}
		return ""
	}}, "test")
	if code != 0 {
		t.Fatalf("exit=%d stdout=%s stderr=%s", code, out.String(), errout.String())
	}
	if !reflect.DeepEqual(calls, []string{"/v1/workflows/check", "/v1/deployments/stream", "/v1/workflows"}) {
		t.Fatalf("calls=%v", calls)
	}
	if strings.Contains(out.String(), "Bearer token") {
		t.Fatal("credentials leaked")
	}
	lines := strings.Split(strings.TrimSpace(out.String()), "\n")
	if len(lines) != 3 {
		t.Fatalf("output=%s", out.String())
	}
}
func TestEndpointOverrideCannotBorrowCredentials(t *testing.T) {
	root := t.TempDir()
	store, _ := contexts.New(root)
	if err := store.Save(contexts.Context{Endpoint: "https://old.invalid", Token: "secret", ClusterID: "old", Insecure: true}); err != nil {
		t.Fatal(err)
	}
	_, err := clientFor(options{endpoint: "https://new.invalid"}, IO{Getenv: func(key string) string {
		if key == "LUMA_CONFIG_HOME" {
			return root
		}
		return ""
	}})
	if err == nil || !strings.Contains(err.Error(), "token is required") {
		t.Fatalf("error=%v", err)
	}
}
func TestDryRunNeverNeedsControlOrCredentials(t *testing.T) {
	root := t.TempDir()
	source := filepath.Join(root, "app.yml")
	config := filepath.Join(root, "config.yml")
	_ = os.WriteFile(source, []byte("name: app\nimage: nginx\nregion: cn\n"), 0600)
	_ = os.WriteFile(config, []byte("{}"), 0600)
	var out, errout bytes.Buffer
	code := Run(context.Background(), []string{"deploy", source, "--dry-run", "--config", config, "--format", "json"}, IO{Out: &out, Err: &errout, Getenv: func(string) string { panic("offline preview read connection environment") }}, "test")
	if code != 0 || !strings.Contains(out.String(), `"dryRun":true`) {
		t.Fatalf("exit=%d stdout=%s stderr=%s", code, out.String(), errout.String())
	}
}
func TestUnknownOptionsCannotSilentlyChangeDeploy(t *testing.T) {
	for _, args := range [][]string{{"deploy", "app.yml", "--unknown"}, {"deploy", "app.yml", "--skip-orchestrator"}, {"deploy", "app.yml", "--timeout", "-1"}} {
		var errout bytes.Buffer
		if _, _, err := parse(args, &errout); err == nil {
			t.Fatalf("accepted %v", args)
		}
	}
}
