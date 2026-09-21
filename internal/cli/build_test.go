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
	"strings"
	"testing"

	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/deployment"
)

func TestImportWorkflowAndDurableObservation(t *testing.T) {
	for _, scenario := range []string{"success", "conflict", "observation-error", "record-error", "empty-env", "new-target"} {
		t.Run(scenario, func(t *testing.T) {
			root := t.TempDir()
			env := filepath.Join(root, "deploy.env")
			contents := "SECRET=literal-value\n"
			if scenario == "empty-env" {
				contents = ""
			}
			_ = os.WriteFile(env, []byte(contents), 0600)
			submits, reads, records := 0, 0, 0
			server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Authorization") != "Bearer trusted" {
					t.Error("wrong credentials")
				}
				switch r.URL.Path {
				case "/v1/workflows/check":
					var body deployment.Workflow
					_ = json.NewDecoder(r.Body).Decode(&body)
					if body.Recipe.Method != "remote-build" || strings.Contains(strings.Join(body.Recipe.Argv, " "), "literal-value") {
						t.Error(body)
					}
					status := "match"
					if scenario == "conflict" {
						status = "confirmation-required"
					}
					name := "app"
					if scenario == "new-target" {
						name = "sibling-app"
					}
					fmt.Fprintf(w, `{"status":%q,"workflow":{"name":%q,"revision":3},"differences":[]}`, status, name)
				case "/v1/builds":
					submits++
					var body builddomain.ControlRequest
					_ = json.NewDecoder(r.Body).Decode(&body)
					if body.RepoURL != "https://github.com/team/app" || !body.Queue || body.ComposeSidecar != "deploy/luma.compose.yml" || body.Proxy == nil || *body.Proxy != "" || body.EnvSecrets == nil {
						t.Errorf("bad request %+v", body)
					}
					fmt.Fprint(w, `{"queued":true,"buildRunId":"attempt"}`)
				case "/v1/builds/attempt":
					reads++
					if scenario == "observation-error" {
						w.WriteHeader(503)
						fmt.Fprint(w, `{"error":"offline"}`)
						return
					}
					if reads == 1 {
						if r.URL.Query().Get("after") != "-1" {
							t.Error(r.URL)
						}
						fmt.Fprint(w, `{"run":{"id":"attempt","status":"running","events":[{"status":"output","message":"compiler log"}]},"eventsPage":{"hasMore":true,"resumeAfter":100}}`)
					} else {
						if r.URL.Query().Get("after") != "100" {
							t.Error(r.URL)
						}
						fmt.Fprint(w, `{"run":{"id":"attempt","status":"succeeded","result":{"deployment":"app","image":"registry/app:abc1234","buildRunId":"attempt"},"events":[{"status":"ok","message":"deployed"}]},"eventsPage":{"hasMore":false,"resumeAfter":101}}`)
					}
				case "/v1/workflows":
					records++
					var body map[string]any
					_ = json.NewDecoder(r.Body).Decode(&body)
					wantRevision := float64(3)
					if scenario == "new-target" {
						wantRevision = 0
					}
					if body["expectedRevision"] != wantRevision || body["name"] != "app" {
						t.Error(body)
					}
					if scenario == "record-error" {
						w.WriteHeader(409)
						fmt.Fprint(w, `{"error":"changed"}`)
					} else {
						fmt.Fprint(w, `{"workflow":{"name":"app"}}`)
					}
				default:
					t.Error("unexpected route", r.URL.Path)
					w.WriteHeader(404)
				}
			}))
			defer server.Close()
			var out, errout bytes.Buffer
			code := Run(context.Background(), []string{"import", "https://github.com/team/app", "--compose-sidecar", "deploy/luma.compose.yml", "--env", env, "--proxy-mode", "direct", "--control-url", server.URL, "--token", "trusted", "--insecure", "--no-env", "--format", "ndjson"}, IO{Out: &out, Err: &errout, Getenv: func(string) string { return "" }}, "test")
			switch scenario {
			case "conflict":
				if code != 1 || submits != 0 {
					t.Fatal(code, submits)
				}
			case "observation-error":
				if code != 1 || submits != 1 || records != 0 {
					t.Fatal(code, submits, records)
				}
			default:
				if code != 0 || submits != 1 || records != 1 || !strings.Contains(out.String(), "compiler log") {
					t.Fatal(code, submits, records, out.String(), errout.String())
				}
			}
			if strings.Contains(out.String(), "literal-value") || strings.Contains(out.String(), "trusted") {
				t.Fatal("secret in output")
			}
		})
	}
}
func TestRemoteWorkflowRunPreservesURLsAndLocksConnection(t *testing.T) {
	root := t.TempDir()
	_ = os.WriteFile(filepath.Join(root, ".env"), []byte("LUMA_CONTROL_URL=https://never.invalid\nLUMA_DEPLOY_TOKEN=never-send\n"), 0600)
	_ = os.WriteFile(filepath.Join(root, "manifest.yml"), []byte("name: app\nbuild: {}\n"), 0600)
	submitted := false
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer trusted" {
			t.Error("connection changed")
		}
		switch r.URL.Path {
		case "/v1/workflows/app":
			fmt.Fprint(w, `{"workflow":{"name":"app","recipe":{"schemaVersion":1,"method":"remote-build","argv":["import","https://github.com/team/app","--manifest","manifest.yml","--region","global"]}}}`)
		case "/v1/workflows/check":
			fmt.Fprint(w, `{"status":"match","workflow":{"revision":2}}`)
		case "/v1/builds":
			var body builddomain.ControlRequest
			_ = json.NewDecoder(r.Body).Decode(&body)
			if body.RepoURL != "https://github.com/team/app" || body.Manifest != "name: app\nbuild: {}\n" || body.ExpectedName != "app" {
				t.Error(body)
			}
			submitted = true
			fmt.Fprint(w, `{"queued":true,"buildRunId":"attempt"}`)
		case "/v1/builds/attempt":
			fmt.Fprint(w, `{"run":{"id":"attempt","status":"succeeded","result":{"service":"app"}},"eventsPage":{"resumeAfter":-1}}`)
		case "/v1/workflows":
			fmt.Fprint(w, `{"workflow":{"name":"app"}}`)
		default:
			t.Error(r.URL.Path)
			w.WriteHeader(404)
		}
	}))
	defer server.Close()
	var out, errout bytes.Buffer
	code := Run(context.Background(), []string{"workflow", "run", "app", "--path", root, "--control-url", server.URL, "--token", "trusted", "--insecure", "--no-env", "--format", "json"}, IO{Out: &out, Err: &errout, Getenv: func(string) string { return "" }}, "test")
	if code != 0 || !submitted {
		t.Fatal(code, out.String(), errout.String())
	}
}
func TestImportOptionsCannotChangeUnrelatedCommands(t *testing.T) {
	for _, args := range [][]string{{"build", "cancel", "attempt", "--ref", "main"}, {"deploy", "app.yml", "--provider-id", "git"}, {"build", "list", "--after", "2"}, {"import", "team/app", "--proxy-mode", "unknown"}, {"import", "team/app", "--compose-sidecar", "stack.yml", "--manifest", "local.yml"}} {
		var out, errout bytes.Buffer
		code := Run(context.Background(), args, IO{Out: &out, Err: &errout, Getenv: func(string) string { return "" }}, "test")
		if code != 1 {
			t.Fatal(args, out.String())
		}
	}
}

func TestRetryWorkflowRecordRunAndTargetGuard(t *testing.T) {
	for _, mode := range []string{"retry", "record", "run", "active", "wrong-target"} {
		t.Run(mode, func(t *testing.T) {
			root := t.TempDir()
			env := filepath.Join(root, "empty.env")
			_ = os.WriteFile(env, nil, 0600)
			submits, records := 0, 0
			server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch r.URL.Path {
				case "/v1/workflows/app":
					fmt.Fprint(w, `{"workflow":{"name":"app","recipe":{"schemaVersion":1,"method":"remote-build","argv":["build","retry","old","--env","empty.env","--no-env"],"retryParameters":{"method":"remote-build","repo":"https://github.com/team/app"}}}}`)
				case "/v1/builds/old":
					status, name := "succeeded", "app"
					if mode == "active" {
						status = "running"
					}
					if mode == "wrong-target" {
						name = "different"
					}
					fmt.Fprintf(w, `{"run":{"id":"old","status":%q,"mode":"remote","request":{"repoUrl":"https://github.com/team/app","manifest":"name: app\nbuild: {}\n"},"result":{"service":%q}}}`, status, name)
				case "/v1/workflows/check":
					fmt.Fprint(w, `{"status":"match","workflow":{"name":"app","revision":4}}`)
				case "/v1/builds/old/retry":
					submits++
					var body builddomain.RetryRequest
					_ = json.NewDecoder(r.Body).Decode(&body)
					if !body.Queue || body.EnvSecrets == nil || len(body.EnvSecrets) != 0 || body.Workflow == nil || body.Workflow.ExpectedRevision != 4 || body.Workflow.Selector.Name != "app" {
						t.Errorf("invalid retry %+v", body)
					}
					if body.Workflow != nil && !strings.HasPrefix(fmt.Sprint(body.Workflow.Recipe.RetryParameters["manifest"]), "sha256:") {
						t.Error("manifest is not fingerprinted")
					}
					fmt.Fprint(w, `{"queued":true,"buildRunId":"new"}`)
				case "/v1/builds/new":
					fmt.Fprint(w, `{"run":{"id":"new","status":"succeeded","result":{"service":"app","workflow":{"saved":true,"name":"app"}}},"eventsPage":{"resumeAfter":-1}}`)
				case "/v1/workflows":
					records++
					var body struct {
						Recipe deployment.Recipe
						Name   string
					}
					_ = json.NewDecoder(r.Body).Decode(&body)
					if body.Name != "app" || body.Recipe.RetryParameters["repo"] != "https://github.com/team/app" {
						t.Error(body)
					}
					fmt.Fprint(w, `{"workflow":{"name":"app"}}`)
				default:
					t.Error(r.URL.Path)
					w.WriteHeader(404)
				}
			}))
			defer server.Close()
			args := []string{"--control-url", server.URL, "--token", "trusted", "--insecure", "--no-env", "--format", "json"}
			switch mode {
			case "record":
				args = append(args, "workflow", "record", "app", "--", "build", "retry", "old", "--no-env", "--env", env)
			case "run", "wrong-target":
				args = append(args, "workflow", "run", "app", "--path", root)
			default:
				args = append(args, "build", "retry", "old", "--env", env)
			}
			var out, errout bytes.Buffer
			code := Run(context.Background(), args, IO{Out: &out, Err: &errout, Getenv: func(string) string { return "" }}, "test")
			if mode == "active" || mode == "wrong-target" {
				if code != 1 || submits != 0 || records != 0 {
					t.Fatal(code, submits, records, errout.String())
				}
			} else if mode == "record" {
				if code != 0 || submits != 0 || records != 1 {
					t.Fatal(code, submits, records, errout.String())
				}
			} else if code != 0 || submits != 1 || records != 0 {
				t.Fatal(code, submits, records, errout.String())
			}
		})
	}
}
