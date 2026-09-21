package cli

import (
	"os"
	"path/filepath"
	"testing"
)

func TestConnectionEnvironmentPrecedenceAndIsolation(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "connection.env")
	_ = os.WriteFile(path, []byte("LUMA_DEPLOY_TOKEN=file-value\nFROM_FILE=literal-${TOKEN}\n"), 0600)
	_ = os.WriteFile(filepath.Join(root, ".luma.config.json"), []byte(`{"env":{"LUMA_DEPLOY_TOKEN":"user-value","FROM_USER":true}}`), 0600)
	streams := IO{Getenv: func(key string) string {
		if key == "HOME" {
			return root
		}
		if key == "LUMA_DEPLOY_TOKEN" {
			return "process-value"
		}
		return ""
	}}
	loaded, err := withEnvironment(options{envFile: path}, streams)
	if err != nil {
		t.Fatal(err)
	}
	if loaded.Getenv("LUMA_DEPLOY_TOKEN") != "process-value" || loaded.Getenv("FROM_FILE") != "literal-${TOKEN}" || loaded.Getenv("FROM_USER") != "True" {
		t.Fatal("environment precedence or literal semantics changed")
	}
	if streams.Getenv("FROM_USER") != "" {
		t.Fatal("caller environment was mutated")
	}
	ignored, err := withEnvironment(options{envFile: path, noEnv: true}, streams)
	if err != nil || ignored.Getenv("FROM_FILE") != "" {
		t.Fatal("--no-env still loaded files")
	}
	loaded, err = withEnvironment(options{envFile: filepath.Join(root, "missing")}, streams)
	if err != nil || loaded.Getenv("FROM_USER") != "True" {
		t.Fatal("missing optional .env blocked user config")
	}
}

func TestEmptyProcessValueDoesNotBorrowDotEnv(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, ".env")
	_ = os.WriteFile(path, []byte("LUMA_DEPLOY_TOKEN=do-not-use\n"), 0600)
	streams := IO{Getenv: func(key string) string {
		if key == "HOME" {
			return root
		}
		return ""
	}, LookupEnv: func(key string) (string, bool) { return "", key == "LUMA_DEPLOY_TOKEN" }}
	loaded, err := withEnvironment(options{envFile: path}, streams)
	if err != nil {
		t.Fatal(err)
	}
	if loaded.Getenv("LUMA_DEPLOY_TOKEN") != "" {
		t.Fatal("an explicit empty process token was replaced with dotenv credentials")
	}
}
