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

	"github.com/LiuTianjie/luma/internal/deployment"
)

func TestWorkflowRunUsesCheckoutAndCurrentConnection(t *testing.T) {
	for _, scenario := range []string{"native", "compose", "wrong-app", "unsafe-recipe", "build-recipe"} {
		t.Run(scenario, func(t *testing.T) {
			root := t.TempDir()
			for name, content := range map[string]string{"app.yml": "name: app\nimage: nginx\nregion: cn\n", "luma.compose.yml": "name: app\nregion: cn\n", "docker-compose.yml": "services:\n  web:\n    image: nginx\n", "luma.yaml": "{}\n", ".env": "LUMA_CONTROL_URL=https://never-contact.invalid\nLUMA_DEPLOY_TOKEN=never-send\n"} {
				_ = os.WriteFile(filepath.Join(root, name), []byte(content), 0600)
			}
			recipe := deployment.Recipe{SchemaVersion: 1, Method: "image-deploy", Argv: []string{"--env-file", ".env", "deploy", "app.yml"}}
			if scenario == "compose" {
				recipe.Method = "compose-deploy"
				recipe.Argv = []string{"--env-file", ".env", "compose", "deploy", "luma.compose.yml"}
			}
			if scenario == "unsafe-recipe" {
				recipe.Argv = append(recipe.Argv, "--control-url", "https://never-contact.invalid")
			}
			if scenario == "build-recipe" {
				recipe.Method = "local-build"
				recipe.Argv = []string{"build", "local", "."}
			}
			var calls []string
			server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls = append(calls, r.URL.Path)
				if r.Header.Get("Authorization") != "Bearer trusted" {
					t.Error("recipe replaced current credentials")
				}
				switch r.URL.Path {
				case "/v1/workflows/app", "/v1/workflows/other":
					_ = json.NewEncoder(w).Encode(map[string]any{"workflow": map[string]any{"name": "app", "recipe": recipe}})
				case "/v1/workflows/check":
					var body deployment.Workflow
					_ = json.NewDecoder(r.Body).Decode(&body)
					for _, value := range body.Recipe.Argv {
						if strings.Contains(value, "trusted") || strings.Contains(value, "never-send") {
							t.Error("credentials in recipe")
						}
					}
					fmt.Fprint(w, `{"status":"match","workflow":{"revision":2}}`)
				case "/v1/deployments/stream", "/v1/compose-deployments/stream":
					fmt.Fprintln(w, `{"status":"done","result":{"service":"app"}}`)
				case "/v1/workflows":
					fmt.Fprint(w, `{"workflow":{"name":"app"}}`)
				default:
					t.Errorf("unexpected request %s", r.URL.Path)
					w.WriteHeader(404)
				}
			}))
			defer server.Close()
			name := "app"
			if scenario == "wrong-app" {
				name = "other"
			}
			var out, errout bytes.Buffer
			code := Run(context.Background(), []string{"workflow", "run", name, "--path", root, "--control-url", server.URL, "--token", "trusted", "--insecure", "--no-env", "--format", "json"}, IO{Out: &out, Err: &errout, Getenv: func(key string) string {
				if key == "LUMA_CONFIG_HOME" {
					return filepath.Join(root, "contexts")
				}
				if key == "HOME" {
					return root
				}
				return ""
			}}, "test")
			if scenario == "native" || scenario == "compose" {
				if code != 0 {
					t.Fatalf("out=%s err=%s", &out, &errout)
				}
				expected := []string{"/v1/workflows/app"}
				endpoint := "/v1/deployments/stream"
				if scenario == "compose" {
					endpoint = "/v1/compose-deployments/stream"
				}
				expected = append(expected, "/v1/workflows/check", endpoint, "/v1/workflows")
				if !reflect.DeepEqual(calls, expected) {
					t.Fatal(calls)
				}
			} else if code != 1 || len(calls) != 1 {
				t.Fatalf("code=%d calls=%v out=%s err=%s", code, calls, &out, &errout)
			}
		})
	}
}

func TestWorkflowRecordOmitsRevisionAndNeverDeploys(t *testing.T) {
	root := t.TempDir()
	var calls []string
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls = append(calls, r.URL.Path)
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		if _, exists := body["expectedRevision"]; exists {
			t.Error("manual record must not impose revision zero")
		}
		if body["source"] != "manual" || body["note"] != "reviewed" {
			t.Error(body)
		}
		fmt.Fprint(w, `{"workflow":{"name":"app"}}`)
	}))
	defer server.Close()
	var out, errout bytes.Buffer
	code := Run(context.Background(), []string{"--control-url", server.URL, "--token", "token", "--insecure", "--no-env", "workflow", "record", "app", "--note", "reviewed", "--", "--no-env", "deploy", filepath.Join(root, "app.yml"), "--env", filepath.Join(root, "prod.env")}, IO{Out: &out, Err: &errout, Getenv: func(string) string { return "" }}, "test")
	if code != 0 || !reflect.DeepEqual(calls, []string{"/v1/workflows"}) {
		t.Fatalf("code=%d calls=%v out=%s err=%s", code, calls, &out, &errout)
	}
}
