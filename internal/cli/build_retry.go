package cli

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"strconv"
	"time"

	"github.com/LiuTianjie/luma/internal/adapters/controlhttp"
	"github.com/LiuTianjie/luma/internal/adapters/envfile"
	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/deployment"
	"github.com/LiuTianjie/luma/internal/workflow"
)

func retryWorkflow(ctx context.Context, client *controlhttp.Client, id string, o options, name string) (deployment.Workflow, error) {
	raw, err := client.GetBuild(ctx, id, nil)
	if err != nil {
		return deployment.Workflow{}, err
	}
	var reply struct {
		Run builddomain.Run `json:"run"`
	}
	if json.Unmarshal(raw, &reply) != nil || reply.Run.ID != id {
		return deployment.Workflow{}, fmt.Errorf("invalid recorded build request")
	}
	prior := reply.Run
	if !builddomain.Terminal(prior.Status) || prior.Mode == "local" {
		return deployment.Workflow{}, fmt.Errorf("only a completed repository build can be retried")
	}
	actual, _ := prior.Result["service"].(string)
	if actual == "" {
		actual, _ = prior.Result["deployment"].(string)
	}
	if actual == "" {
		actual = prior.Request.ExpectedName
	}
	if name != "" && actual != "" && workflow.Slug(name) != workflow.Slug(actual) {
		return deployment.Workflow{}, fmt.Errorf("recorded retry targets a different application")
	}
	if name == "" {
		name = actual
	}
	r := prior.Request
	if r.RepoURL == "" && r.ProviderID == "" {
		r.RepoURL = prior.Source
	}
	argv := []string{"build", "retry", id, "--timeout", strconv.Itoa(o.timeout)}
	if o.noEnv {
		argv = append(argv, "--no-env")
	} else {
		argv = append(argv, "--env-file", o.envFile)
	}
	parameters := map[string]any{"method": "remote-build"}
	for key, value := range map[string]string{"repo": r.RepoURL, "provider_id": r.ProviderID, "repository": r.Repository, "ref": r.Ref, "build_node": r.BuildNode, "region": r.Region, "exposure": r.Exposure, "domain": r.Domain, "compose_sidecar": r.ComposeSidecar, "platform": r.Platform, "build_context": r.Context, "dockerfile": r.Dockerfile, "registry_host": r.RegistryHost, "proxy_mode": r.ProxyMode} {
		if value != "" {
			parameters[key] = value
		}
	}
	if r.Port != 0 {
		parameters["port"] = r.Port
	}
	if r.Manifest != "" {
		parameters["manifest"] = fmt.Sprintf("sha256:%x", sha256.Sum256([]byte(r.Manifest)))
	}
	if o.deployEnvFile != "" {
		argv = append(argv, "--env", o.deployEnvFile)
		parameters["deploy_env_file"] = o.deployEnvFile
	}
	recipe, _, err := workflow.ValidateRecipe(deployment.Recipe{SchemaVersion: 1, Method: "remote-build", Argv: argv, RetryParameters: parameters})
	if err != nil {
		return deployment.Workflow{}, err
	}
	w := deployment.Workflow{Recipe: recipe, Selector: deployment.Selector{Name: name, RepoURL: r.RepoURL}}
	if o.used["workflow-note"] || o.used["note"] {
		w.Note = &o.workflowNote
	}
	return w, nil
}

func retryCommand(ctx context.Context, args []string, o options, streams IO) error {
	client, err := clientFor(o, streams)
	if err != nil {
		return err
	}
	defer client.Close()
	ctx, cancel := context.WithTimeout(ctx, time.Duration(o.timeout)*time.Second)
	defer cancel()
	w, err := retryWorkflow(ctx, client, args[2], o, o.expectedName)
	if err != nil {
		return err
	}
	r := builddomain.RetryRequest{Queue: true}
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
	emit := eventWriter(streams, o)
	result, err := deployment.ExecuteWorkflow(ctx, client, w, o.acceptWorkflowChange, emit, func(checked deployment.Workflow) ([]byte, error) {
		r.Workflow = &builddomain.QueuedWorkflow{Workflow: checked, AcceptChange: o.acceptWorkflowChange}
		return client.RetryBuild(ctx, args[2], r, emit)
	})
	if err != nil {
		return err
	}
	return success(streams, o, json.RawMessage(result))
}
