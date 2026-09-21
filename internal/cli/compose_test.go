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
)

func TestComposeSubmissionAndScopedEnv(t *testing.T) {
	for _, scenario := range []string{"success", "dry-run", "interrupted", "workflow-change", "accepted-change", "invalid-env"} {
		t.Run(scenario, func(t *testing.T) {
			root := t.TempDir()
			source := filepath.Join(root, "luma.compose.yml")
			sidecar := "name: app\nregion: cn\nservices:\n  web:\n    node: appnode\n"
			compose := "services:\n  web:\n    image: nginx\n    environment:\n      TOKEN: '${TOKEN}'\n"
			for name, content := range map[string]string{"luma.compose.yml": sidecar, "docker-compose.yml": compose, "config.yml": "{}", "prod.env": "TOKEN='scoped value'\nUNRELATED=must-not-send\n"} {
				if err := os.WriteFile(filepath.Join(root, name), []byte(content), 0600); err != nil {
					t.Fatal(err)
				}
			}
			if scenario == "invalid-env" {
				_ = os.WriteFile(filepath.Join(root, "prod.env"), []byte("TOKEN='do-not-leak"), 0600)
			}
			var calls []string
			server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls = append(calls, r.URL.Path)
				if r.Header.Get("Authorization") != "Bearer connection-token" {
					t.Error("incorrect connection credentials")
				}
				var body map[string]any
				if r.Method == "POST" {
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Error(err)
					}
				}
				switch r.URL.Path {
				case "/v1/workflows/check":
					recipe := body["recipe"].(map[string]any)
					if recipe["method"] != "compose-deploy" {
						t.Error(recipe)
					}
					argv := recipe["argv"].([]any)
					found := false
					for i, v := range argv {
						if v == "--env" && i+1 < len(argv) && argv[i+1] == "prod.env" {
							found = true
						}
					}
					if !found {
						t.Error("scoped env path missing from portable recipe")
					}
					status := "match"
					if strings.Contains(scenario, "change") {
						status = "confirmation-required"
					}
					fmt.Fprintf(w, `{"status":%q,"workflow":{"revision":7}}`, status)
				case "/v1/compose-deployments/stream":
					if body["manifest"] != sidecar || body["composeContent"] != compose {
						t.Error("original sources not preserved")
					}
					if !reflect.DeepEqual(body["envSecrets"], map[string]any{"TOKEN": "scoped value"}) {
						t.Error("env secrets not scoped")
					}
					fmt.Fprintln(w, `{"status":"ok","message":"started"}`)
					if scenario != "interrupted" {
						fmt.Fprintln(w, `{"status":"done","result":{"deployment":"app","revision":2}}`)
					}
				case "/v1/workflows":
					if body["expectedRevision"] != float64(7) {
						t.Error("lost workflow revision")
					}
					fmt.Fprint(w, `{"workflow":{"name":"app"}}`)
				default:
					t.Errorf("unexpected request %s", r.URL.Path)
					w.WriteHeader(404)
				}
			}))
			defer server.Close()
			connection := filepath.Join(root, "connection.env")
			_ = os.WriteFile(connection, []byte("LUMA_CONTROL_URL="+server.URL+"\nLUMA_DEPLOY_TOKEN=connection-token\nLUMA_INSECURE=true\n"), 0600)
			args := []string{"compose", "deploy", source, "--config", filepath.Join(root, "config.yml"), "--env", filepath.Join(root, "prod.env"), "--env-file", connection, "--format", "ndjson"}
			if scenario == "dry-run" {
				args = append(args, "--dry-run")
			}
			if scenario == "accepted-change" {
				args = append(args, "--accept-workflow-change")
			}
			var out, errout bytes.Buffer
			code := Run(context.Background(), args, IO{Out: &out, Err: &errout, Getenv: func(key string) string {
				if key == "LUMA_CONFIG_HOME" {
					return filepath.Join(root, "contexts")
				}
				return ""
			}}, "test")
			wantCode := 0
			if scenario == "interrupted" || scenario == "workflow-change" || scenario == "invalid-env" {
				wantCode = 1
			}
			if code != wantCode {
				t.Fatalf("exit=%d out=%s err=%s", code, &out, &errout)
			}
			var expected []string
			if scenario != "dry-run" && scenario != "invalid-env" {
				expected = append(expected, "/v1/workflows/check")
			}
			if scenario == "success" || scenario == "interrupted" || scenario == "accepted-change" {
				expected = append(expected, "/v1/compose-deployments/stream")
			}
			if scenario == "success" || scenario == "accepted-change" {
				expected = append(expected, "/v1/workflows")
			}
			if !reflect.DeepEqual(calls, expected) {
				t.Fatalf("calls=%v expected=%v", calls, expected)
			}
			for _, secret := range []string{"connection-token", "scoped value", "must-not-send", "do-not-leak"} {
				if strings.Contains(out.String()+errout.String(), secret) {
					t.Error("secret leaked in output")
				}
			}
		})
	}
}

func TestRejectsOptionsForWrongCommand(t *testing.T) {
	for _, args := range [][]string{{"status", "--dry-run"}, {"validate", "app.yml", "--env", "prod.env"}, {"deploy", "app.yml", "--local"}, {"compose", "render", "app.yml", "--skip-dns"}} {
		o, pos, err := parse(args, &bytes.Buffer{})
		if err == nil {
			err = validateOptions(pos, o)
		}
		if err == nil {
			t.Fatalf("silently ignored options: %v", args)
		}
	}
}
