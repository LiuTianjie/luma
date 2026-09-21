package cli

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/LiuTianjie/luma/internal/adapters/controlhttp"
	"github.com/LiuTianjie/luma/internal/deployment"
	"github.com/LiuTianjie/luma/internal/workflow"
)

func workflowCommand(ctx context.Context, args []string, o options, streams IO) error {
	if len(args) < 2 {
		return fmt.Errorf("usage: workflow list|show|run|record")
	}
	command := args[1]
	if command == "list" && len(args) != 2 || ((command == "show" || command == "run") && len(args) != 3) || command == "record" && len(args) < 5 {
		return fmt.Errorf("invalid workflow arguments")
	}
	if command != "list" && command != "show" && command != "run" && command != "record" {
		return fmt.Errorf("unknown workflow command: %s", command)
	}
	connection, err := connectionOptions(o, streams)
	if err != nil {
		return err
	}
	client, err := controlhttp.New(connection)
	if err != nil {
		return err
	}
	defer client.Close()
	readCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	if command == "list" {
		data, err := client.ListWorkflows(readCtx)
		if err != nil {
			return err
		}
		return success(streams, o, data)
	}
	if command == "record" {
		argv := args[3:]
		if argv[0] == "luma" || argv[0] == "luma-go" {
			argv = argv[1:]
		}
		recipeOptions, pos, method, err := parseRecipe(argv)
		if err != nil {
			return err
		}
		var w deployment.Workflow
		if method == "local-build" {
			source, e := localExecutor().InspectLocal(readCtx, pos[2], recipeOptions.localRepoURL, recipeOptions.composeSidecar, recipeOptions.buildRegion)
			if e != nil {
				return e
			}
			w, err = localWorkflow(source, pos[2], recipeOptions, args[2])
		} else if method == "remote-build" && pos[0] == "build" {
			w, err = retryWorkflow(readCtx, client, pos[2], recipeOptions, args[2])
		} else if method == "remote-build" {
			w, err = importWorkflow(pos, recipeOptions, args[2])
		} else {
			_, config, e := loadRenderConfig(recipeOptions)
			if e != nil {
				return e
			}
			w, err = makeWorkflow(pos[len(pos)-1], config, args[2], method, recipeOptions)
		}
		if err != nil {
			return err
		}
		if o.used["note"] {
			w.Note = &o.workflowNote
		}
		data, err := client.SetWorkflow(readCtx, w)
		if err != nil {
			return err
		}
		return success(streams, o, data)
	}
	data, err := client.GetWorkflow(readCtx, args[2])
	if err != nil {
		return err
	}
	if command == "show" {
		return success(streams, o, data)
	}
	var payload struct {
		Workflow struct {
			Name   string
			Recipe deployment.Recipe
		}
	}
	if err := json.Unmarshal(data, &payload); err != nil {
		return err
	}
	recipe := payload.Workflow.Recipe
	if recipe.SchemaVersion != 1 {
		return fmt.Errorf("unsupported workflow schema")
	}
	if _, _, err := workflow.ValidateRecipe(recipe); err != nil {
		return err
	}
	next, pos, method, err := parseRecipe(recipe.Argv)
	if err != nil {
		return err
	}
	if recipe.Method != method {
		return fmt.Errorf("workflow method does not match command")
	}
	root, err := filepath.Abs(o.path)
	if err != nil {
		return err
	}
	info, err := os.Stat(root)
	if err != nil {
		return err
	}
	if !info.IsDir() {
		return fmt.Errorf("workflow --path must be a checkout directory")
	}
	for dir := root; ; dir = filepath.Dir(dir) {
		if _, err := os.Stat(filepath.Join(dir, ".git")); err == nil {
			root = dir
			break
		}
		if filepath.Dir(dir) == dir {
			break
		}
	}
	resolve := func(path string) string {
		if path == "" || filepath.IsAbs(path) {
			return path
		}
		return filepath.Join(root, path)
	}
	if method != "remote-build" {
		pos[len(pos)-1] = resolve(pos[len(pos)-1])
	} else {
		resolveImportFiles(&next, root)
	}
	next.config = resolve(next.config)
	if next.config == "" {
		if _, err := os.Stat(filepath.Join(root, "luma.yaml")); err == nil {
			next.config = filepath.Join(root, "luma.yaml")
		}
	}
	// Pin even an absent default config to this checkout, never the caller cwd.
	next.configRoot = root
	next.envFile = resolve(next.envFile)
	next.deployEnvFile = resolve(next.deployEnvFile)
	next.format = o.format
	next.acceptWorkflowChange = o.acceptWorkflowChange
	next.expectedName = args[2]
	next.lockedConnection = &connection
	if o.used["workflow-note"] {
		next.workflowNote = o.workflowNote
		next.used["workflow-note"] = true
	}
	streams, err = withEnvironment(next, streams)
	if err != nil {
		return err
	}
	if method == "remote-build" && pos[0] == "build" {
		return retryCommand(ctx, pos, next, streams)
	}
	if method == "remote-build" {
		return importCommand(ctx, pos, next, streams)
	}
	if method == "local-build" {
		return localBuildCommand(ctx, pos, next, streams)
	}
	if method == "compose-deploy" {
		return composeCommand(ctx, pos, next, streams)
	}
	return serviceCommand(ctx, "deploy", pos, next, streams)
}

// Recipes are typed CLI input, never shell commands. Permit only implemented
// deployment options, and never let server content choose a credential target.
func parseRecipe(argv []string) (options, []string, string, error) {
	if len(argv) == 0 || len(argv) > 128 {
		return options{}, nil, "", fmt.Errorf("workflow argv must have 1 to 128 arguments")
	}
	for _, arg := range argv {
		if len(arg) > 4096 || strings.ContainsRune(arg, 0) {
			return options{}, nil, "", fmt.Errorf("invalid workflow argument")
		}
	}
	o, pos, err := parse(argv, io.Discard)
	if err != nil {
		return o, nil, "", fmt.Errorf("unsupported workflow recipe: %w", err)
	}
	allowed := map[string]bool{"config": true, "no-env": true, "env-file": true, "env": true, "secrets-env-file": true, "engine": true, "timeout": true, "skip-dns": true}
	if len(pos) > 0 && pos[0] == "import" {
		for _, name := range strings.Fields("provider-id repository build-node ref region exposure domain port manifest compose-sidecar platform context dockerfile registry-host proxy-mode") {
			allowed[name] = true
		}
	}
	if len(pos) > 1 && pos[0] == "build" && pos[1] == "local" {
		for _, name := range strings.Fields("repo-url builder proxy compose-sidecar region exposure domain port platform context dockerfile") {
			allowed[name] = true
		}
	}
	for flag := range o.used {
		if !allowed[flag] {
			return o, nil, "", fmt.Errorf("workflow recipe cannot include --%s", flag)
		}
	}
	method := ""
	if len(pos) == 2 && pos[0] == "deploy" {
		method = "image-deploy"
	}
	if len(pos) == 3 && pos[0] == "compose" && pos[1] == "deploy" {
		method = "compose-deploy"
	}
	if len(pos) >= 1 && len(pos) <= 2 && pos[0] == "import" {
		method = "remote-build"
		if _, err := importRequest(pos, o); err != nil {
			return o, nil, "", err
		}
	}
	if len(pos) >= 2 && len(pos) <= 3 && pos[0] == "build" && pos[1] == "local" {
		method = "local-build"
		if len(pos) == 2 {
			pos = append(pos, ".")
		}
	}
	var retryParameters map[string]any
	if len(pos) == 3 && pos[0] == "build" && pos[1] == "retry" {
		method = "remote-build"
		retryParameters = map[string]any{"method": method}
	}
	if method == "" {
		return o, nil, "", fmt.Errorf("workflow command has not been migrated")
	}
	if err := validateOptions(pos, o); err != nil {
		return o, nil, "", err
	}
	if _, _, err := workflow.ValidateRecipe(deployment.Recipe{SchemaVersion: 1, Method: method, Argv: argv, RetryParameters: retryParameters}); err != nil {
		return o, nil, "", err
	}
	return o, pos, method, nil
}
