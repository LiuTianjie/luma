package workflow

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"
)

func TestRecipeSanitizesURLValuesAndCanonicalizesEquivalentPaths(t *testing.T) {
	r := Recipe{SchemaVersion: 1, Method: "local-build", Argv: []string{"build", "local", "./directory//../app", "--repo-url=https://user:secret@git.test/acme/repo.git?token=hidden#fragment", "--proxy", "http://user:secret@proxy.test:8080/?hidden", "--context", ".", "--dockerfile", "Dockerfile"}}
	validated, fields, err := ValidateRecipe(r)
	if err != nil {
		t.Fatal(err)
	}
	encoded, _ := json.Marshal(validated)
	for _, secret := range []string{"user:secret", "hidden", "fragment"} {
		if strings.Contains(string(encoded), secret) {
			t.Fatal(string(encoded))
		}
	}
	if validated.Argv[3] != "--repo-url=https://git.test/acme/repo.git" {
		t.Fatal(validated)
	}
	if fields["path"] != "directory/../app" || fields["build_context"] != nil || fields["dockerfile"] != nil {
		t.Fatal(fields)
	}
	if r.Argv[3] == validated.Argv[3] {
		t.Fatal("modified caller recipe")
	}
	for _, url := range []string{"git@git.test:acme/repo.git", "https://git.test/acme/repo.git", "ssh://git@git.test/acme/repo"} {
		key, err := RepoKey(url)
		if err != nil || key != "git.test/acme/repo" {
			t.Fatal(key, err)
		}
	}
}

func TestRetryParametersStayIntactButDefaultsCompareCanonically(t *testing.T) {
	parameters := map[string]any{"method": "remote-build", "repo": "https://user:secret@git.test/repo?token=hidden", "port": json.Number("8080"), "build_context": ".", "dockerfile": "Dockerfile", "proxy_mode": "auto"}
	r := Recipe{SchemaVersion: 1, Method: "remote-build", Argv: []string{"build", "retry", "run-1"}, RetryParameters: parameters}
	validated, fields, err := ValidateRecipe(r)
	if err != nil {
		t.Fatal(err)
	}
	if validated.RetryParameters["build_context"] != "." || fields["build_context"] != nil {
		t.Fatal(validated, fields)
	}
	if validated.RetryParameters["repo"] != "https://git.test/repo" {
		t.Fatal(validated)
	}
	if parameters["repo"] == validated.RetryParameters["repo"] {
		t.Fatal("modified input")
	}
	_, again, err := ValidateRecipe(validated)
	if err != nil || !reflect.DeepEqual(again, fields) {
		t.Fatal(again, fields, err)
	}
}

func TestRecipeRejectsUnsafeMalformedAndCrossCommandOptions(t *testing.T) {
	for _, argv := range [][]string{
		{"deploy", "app.yml", "--token=secret"}, {"deploy", "app.yml", "--control-url=https://other.test"},
		{"deploy", "app.yml", "--resolve-ip", "127.0.0.1"}, {"deploy", "app.yml", "--insecure"},
		{"deploy", "app.yml", "--engine", "swarm"}, {"deploy", "app.yml", "--timeout", "oops"},
		{"deploy", "app.yml", "--port", "8080"}, {"deploy", "app.yml", "--env"},
		{"deploy", "app.yml", "--no-env=true"}, {"deploy", "app.yml", "--env", "bad\x00path"},
		{"deploy", "app.yml", "--env-file", "https://user:secret@host:badport/env"},
		{"deploy", "app.yml", "--skip-orchestrator"}, {"deploy", "app.yml", "--workflow-note", "nested"},
		{"deploy", "app.yml", "--help"}, {"workflow", "run", "app"},
	} {
		if _, _, err := ValidateRecipe(Recipe{SchemaVersion: 1, Method: "image-deploy", Argv: argv}); err == nil {
			t.Fatalf("accepted %v", argv)
		}
	}
	for _, params := range []map[string]any{{"method": "remote-build", "token": "secret"}, {"method": "remote-build", "port": 1.5}, {"method": "remote-build", "port": true}, nil} {
		if _, _, err := ValidateRecipe(Recipe{SchemaVersion: 1, Method: "remote-build", Argv: []string{"build", "retry", "id"}, RetryParameters: params}); err == nil {
			t.Fatal(params)
		}
	}
}
