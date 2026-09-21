package cli

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"path/filepath"
	"strconv"
	"time"

	"github.com/LiuTianjie/luma/internal/adapters/envfile"
	"github.com/LiuTianjie/luma/internal/adapters/manifest"
	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/deployment"
	"github.com/LiuTianjie/luma/internal/history"
	"github.com/LiuTianjie/luma/internal/workflow"
)

func importRequest(args []string, o options) (builddomain.ControlRequest, error) {
	r := builddomain.ControlRequest{ProviderID: o.providerID, Repository: o.repository, BuildNode: o.buildNode, Ref: o.buildRef, Region: o.buildRegion, Exposure: o.buildExposure, Domain: o.buildDomain, Port: o.buildPort, ComposeSidecar: o.composeSidecar, Platform: o.buildPlatform, Context: o.buildContext, Dockerfile: o.dockerfile, RegistryHost: o.registryHost, Queue: true, Origin: "cli", ExpectedName: o.expectedName}
	if len(args) > 2 {
		return r, fmt.Errorf("usage: import [REPOSITORY] [--provider-id ID --repository OWNER/NAME]")
	}
	if len(args) == 2 {
		r.RepoURL = args[1]
	}
	if r.RepoURL == "" && (r.ProviderID == "" || r.Repository == "") {
		return r, fmt.Errorf("repository URL or provider-id and repository is required")
	}
	if r.RepoURL != "" && r.Repository != "" {
		return r, fmt.Errorf("use a repository URL or provider-id and repository, not both")
	}
	if o.proxyMode != "auto" && o.proxyMode != "direct" {
		return r, fmt.Errorf("proxy-mode must be auto or direct")
	}
	if o.proxyMode == "direct" {
		direct := ""
		r.Proxy = &direct
		r.ProxyMode = "direct"
	}
	if o.buildPort < 0 || o.buildPort > 65535 {
		return r, fmt.Errorf("port must be between 1 and 65535")
	}
	if o.composeSidecar != "" && o.buildManifest != "" {
		return r, fmt.Errorf("compose-sidecar cannot be combined with manifest")
	}
	return r, nil
}
func importWorkflow(args []string, o options, name string) (deployment.Workflow, error) {
	r, err := importRequest(args, o)
	if err != nil {
		return deployment.Workflow{}, err
	}
	argv := []string{"import"}
	if r.RepoURL != "" {
		argv = append(argv, r.RepoURL)
	}
	if o.noEnv {
		argv = append(argv, "--no-env")
	} else {
		argv = append(argv, "--env-file", o.envFile)
	}
	argv = append(argv, "--timeout", strconv.Itoa(o.timeout))
	for _, pair := range [][2]string{{"provider-id", o.providerID}, {"repository", o.repository}, {"build-node", o.buildNode}, {"ref", o.buildRef}, {"region", o.buildRegion}, {"exposure", o.buildExposure}, {"domain", o.buildDomain}, {"manifest", o.buildManifest}, {"compose-sidecar", o.composeSidecar}, {"platform", o.buildPlatform}, {"context", o.buildContext}, {"dockerfile", o.dockerfile}, {"registry-host", o.registryHost}, {"env", o.deployEnvFile}} {
		if pair[1] != "" {
			argv = append(argv, "--"+pair[0], pair[1])
		}
	}
	if o.buildPort != 0 {
		argv = append(argv, "--port", strconv.Itoa(o.buildPort))
	}
	if o.proxyMode == "direct" {
		argv = append(argv, "--proxy-mode", "direct")
	}
	recipe, _, err := workflow.ValidateRecipe(deployment.Recipe{SchemaVersion: 1, Method: "remote-build", Argv: argv})
	if err != nil {
		return deployment.Workflow{}, err
	}
	w := deployment.Workflow{Recipe: recipe, Selector: deployment.Selector{Name: name, RepoURL: r.RepoURL}}
	if o.used["workflow-note"] || o.used["note"] {
		w.Note = &o.workflowNote
	}
	return w, nil
}
func importCommand(ctx context.Context, args []string, o options, streams IO) error {
	r, err := importRequest(args, o)
	if err != nil {
		return err
	}
	name := o.expectedName
	if o.buildManifest != "" {
		data, err := readYAML(o.buildManifest)
		if err != nil {
			return err
		}
		r.Manifest = string(data)
		manifestName, err := (manifest.Decoder{}).BuildName(r.Manifest)
		if err != nil {
			return err
		}
		if name != "" && workflow.Slug(name) != workflow.Slug(manifestName) {
			return fmt.Errorf("recorded workflow application does not match manifest")
		}
		name = manifestName
	}
	if o.deployEnvFile != "" {
		data, err := readYAML(o.deployEnvFile)
		if err != nil {
			return err
		}
		r.EnvSecrets, err = envfile.Parse(data)
		if err != nil {
			return err
		}
	}
	w, err := importWorkflow(args, o, name)
	if err != nil {
		return err
	}
	client, err := clientFor(o, streams)
	if err != nil {
		return err
	}
	defer client.Close()
	ctx, cancel := context.WithTimeout(ctx, time.Duration(o.timeout)*time.Second)
	defer cancel()
	emit := eventWriter(streams, o)
	result, err := deployment.ExecuteWorkflow(ctx, client, w, o.acceptWorkflowChange, emit, func(checked deployment.Workflow) ([]byte, error) {
		r.Workflow = &builddomain.QueuedWorkflow{Workflow: checked, AcceptChange: o.acceptWorkflowChange}
		return client.Import(ctx, r, emit)
	})
	if err != nil {
		return err
	}
	return success(streams, o, json.RawMessage(result))
}
func buildCommand(ctx context.Context, args []string, o options, streams IO) error {
	if len(args) < 2 {
		return fmt.Errorf("usage: build list|logs|cancel|retry|config|local")
	}
	command := args[1]
	if command == "local" {
		return localBuildCommand(ctx, args, o, streams)
	}
	if command == "config" {
		return buildConfigCommand(ctx, args, o, streams)
	}
	if command == "list" && len(args) != 2 || command != "list" && len(args) != 3 {
		return fmt.Errorf("invalid build command arguments")
	}
	if command == "retry" {
		return retryCommand(ctx, args, o, streams)
	}
	q := url.Values{"limit": {strconv.Itoa(o.historyLimit)}}
	for key, value := range map[string]string{"cursor": o.historyCursor, "after": o.historyAfter, "app": o.historyApp, "status": o.historyStatus, "source": o.historySource, "since": o.historySince, "until": o.historyUntil} {
		if value != "" {
			q.Set(key, value)
		}
	}
	values := map[string]string{}
	for key := range q {
		values[key] = q.Get(key)
	}
	switch command {
	case "list":
		if _, err := history.ParseList(values, "build", false); err != nil {
			return err
		}
	case "logs":
		if _, err := history.ParseDetail("build", args[2], values); err != nil {
			return err
		}
	case "cancel":
	default:
		return fmt.Errorf("build %s has not been migrated", command)
	}
	client, err := clientFor(o, streams)
	if err != nil {
		return err
	}
	defer client.Close()
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	var result json.RawMessage
	switch command {
	case "list":
		result, err = client.ListBuilds(ctx, q)
	case "logs":
		result, err = client.GetBuild(ctx, args[2], q)
	case "cancel":
		result, err = client.CancelBuild(ctx, args[2])
	}
	if err != nil {
		return err
	}
	return success(streams, o, result)
}

// Only local files are resolved relative to workflow --path. Git URLs, context
// paths inside the remote repository and the sidecar selector stay untouched.
func resolveImportFiles(o *options, root string) {
	resolve := func(value string) string {
		if value == "" || filepath.IsAbs(value) {
			return value
		}
		return filepath.Join(root, value)
	}
	o.buildManifest = resolve(o.buildManifest)
}
