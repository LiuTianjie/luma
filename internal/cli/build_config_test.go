package cli

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestBuildConfigFlagsReadAndExplicitClear(t *testing.T) {
	calls := 0
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.URL.Path != "/v1/builds/config" || r.Header.Get("Authorization") != "Bearer trusted" {
			t.Error(r.URL)
		}
		switch calls {
		case 1:
			if r.Method != "GET" {
				t.Error(r.Method)
			}
		case 2:
			var body map[string]any
			_ = json.NewDecoder(r.Body).Decode(&body)
			if r.Method != "POST" || len(body["nodes"].([]any)) != 2 || body["pushHost"] != "" || len(body["directEgressNodes"].([]any)) != 0 {
				t.Error(body)
			}
			if _, exists := body["registryHost"]; exists {
				t.Fatal("unspecified registry host was cleared")
			}
		default:
			t.Error("unexpected mutation")
		}
		fmt.Fprint(w, `{"build":{"defaultNode":"one","nodes":[],"directEgressNodes":[]}}`)
	}))
	defer server.Close()
	for _, args := range [][]string{{"build", "config"}, {"build", "config", "--node", "one", "--node", "two", "--clear-direct-egress", "--push-host="}} {
		var out, errout bytes.Buffer
		args = append(args, "--control-url", server.URL, "--token", "trusted", "--insecure", "--no-env")
		if code := Run(context.Background(), args, IO{Out: &out, Err: &errout, Getenv: func(string) string { return "" }}, "test"); code != 0 {
			t.Fatal(errout.String())
		}
	}
	for _, args := range [][]string{{"build", "config", "--direct-egress-node", "one", "--clear-direct-egress"}, {"build", "list", "--node", "one"}, {"build", "config", "--registry-host", "https://user:secret@registry.test"}} {
		var out, errout bytes.Buffer
		if code := Run(context.Background(), args, IO{Out: &out, Err: &errout, Getenv: func(string) string { return "" }}, "test"); code != 1 {
			t.Fatal(args, errout.String())
		}
	}
	if calls != 2 {
		t.Fatal(calls)
	}
}
