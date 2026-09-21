package cli

import (
	"fmt"
	"github.com/LiuTianjie/luma/internal/adapters/repository"
	"github.com/LiuTianjie/luma/internal/deployment"
	"os"
	"path/filepath"
	"strings"
)

func makeWorkflow(source, config, name, method string, o options) (deployment.Workflow, error) {
	if o.expectedName != "" && (deployment.Service{Name: o.expectedName}).Slug() != (deployment.Service{Name: name}).Slug() {
		return deployment.Workflow{}, fmt.Errorf("recorded workflow application does not match manifest name: %s", name)
	}
	absolute, err := filepath.Abs(source)
	if err != nil {
		return deployment.Workflow{}, err
	}
	root := filepath.Dir(absolute)
	for dir := root; ; dir = filepath.Dir(dir) {
		if _, err := os.Stat(filepath.Join(dir, ".git")); err == nil {
			root = dir
			break
		}
		if filepath.Dir(dir) == dir {
			break
		}
	}
	relative := func(path string) string {
		abs, err := filepath.Abs(path)
		if err != nil {
			return path
		}
		rel, err := filepath.Rel(root, abs)
		if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
			return abs
		}
		return filepath.ToSlash(rel)
	}
	argv := []string{}
	if config != "" {
		argv = append(argv, "--config", relative(config))
	}
	if o.noEnv {
		argv = append(argv, "--no-env")
	} else {
		argv = append(argv, "--env-file", relative(o.envFile))
	}
	if method == "compose-deploy" {
		argv = append(argv, "compose")
	}
	argv = append(argv, "deploy", relative(source), "--timeout", fmt.Sprint(o.timeout))
	if o.deployEnvFile != "" {
		argv = append(argv, "--env", relative(o.deployEnvFile))
	}
	if o.engine != "" {
		argv = append(argv, "--engine", o.engine)
	}
	if o.skipDNS {
		argv = append(argv, "--skip-dns")
	}
	w := deployment.Workflow{Recipe: deployment.Recipe{SchemaVersion: 1, Method: method, Argv: argv}, Selector: deployment.Selector{Name: name, RepoURL: repository.Origin(root)}}
	if o.used["workflow-note"] || o.used["note"] {
		w.Note = &o.workflowNote
	}
	return w, nil
}
