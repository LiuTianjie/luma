package cli

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"time"

	"github.com/LiuTianjie/luma/internal/adapters/buildexec"
	"github.com/LiuTianjie/luma/internal/adapters/envfile"
	"github.com/LiuTianjie/luma/internal/adapters/process"
	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/deployment"
	"github.com/LiuTianjie/luma/internal/node"
	"github.com/LiuTianjie/luma/internal/workflow"
)

func localExecutor() buildexec.Executor {
	git, _ := exec.LookPath("git")
	docker, _ := exec.LookPath("docker")
	return buildexec.Executor{Git: git, Docker: docker, Runner: process.Runner{}, Buildx: docker != "", PluginDirs: buildexec.PluginDirectories()}
}
func localWorkflow(source builddomain.LocalSource, path string, o options, name string) (deployment.Workflow, error) {
	if name != "" && workflow.Slug(name) != workflow.Slug(source.Name) {
		return deployment.Workflow{}, fmt.Errorf("local manifest does not match recorded application")
	}
	argv := []string{"build", "local", path, "--timeout", strconv.Itoa(o.timeout)}
	if o.noEnv {
		argv = append(argv, "--no-env")
	} else {
		argv = append(argv, "--env-file", o.envFile)
	}
	for _, pair := range [][2]string{{"repo-url", o.localRepoURL}, {"builder", o.localBuilder}, {"proxy", o.localProxy}, {"compose-sidecar", o.composeSidecar}, {"region", o.buildRegion}, {"exposure", o.buildExposure}, {"domain", o.buildDomain}, {"platform", o.buildPlatform}, {"context", o.buildContext}, {"dockerfile", o.dockerfile}, {"env", o.deployEnvFile}} {
		if pair[1] != "" {
			argv = append(argv, "--"+pair[0], pair[1])
		}
	}
	if o.buildPort != 0 {
		argv = append(argv, "--port", strconv.Itoa(o.buildPort))
	}
	recipe, _, err := workflow.ValidateRecipe(deployment.Recipe{SchemaVersion: 1, Method: "local-build", Argv: argv})
	if err != nil {
		return deployment.Workflow{}, err
	}
	w := deployment.Workflow{Recipe: recipe, Selector: deployment.Selector{Name: source.Name, RepoURL: source.RepoURL}}
	if o.used["workflow-note"] || o.used["note"] {
		w.Note = &o.workflowNote
	}
	return w, nil
}
func localBuildCommand(ctx context.Context, args []string, o options, streams IO) error {
	if len(args) < 2 || len(args) > 3 {
		return fmt.Errorf("usage: build local [PATH]")
	}
	path := "."
	if len(args) == 3 {
		path = args[2]
	}
	ctx, cancel := context.WithTimeout(ctx, time.Duration(o.timeout)*time.Second)
	defer cancel()
	executor := localExecutor()
	source, err := executor.InspectLocal(ctx, path, o.localRepoURL, o.composeSidecar, o.buildRegion)
	if err != nil {
		return err
	}
	w, err := localWorkflow(source, path, o, o.expectedName)
	if err != nil {
		return err
	}
	client, err := clientFor(o, streams)
	if err != nil {
		return err
	}
	defer client.Close()
	var secrets map[string]string
	if o.deployEnvFile != "" {
		data, err := readYAML(o.deployEnvFile)
		if err != nil {
			return err
		}
		secrets, err = envfile.Parse(data)
		if err != nil {
			return err
		}
	}
	scratch, err := os.MkdirTemp("", "luma-local-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(scratch)
	executor.Scratch = scratch
	emit := eventWriter(streams, o)
	result, err := deployment.ExecuteWorkflow(ctx, client, w, o.acceptWorkflowChange, emit, func(checked deployment.Workflow) ([]byte, error) {
		prepared, err := client.PrepareLocalBuild(ctx, builddomain.LocalPrepareRequest{ControlRequest: builddomain.ControlRequest{RepoURL: source.RepoURL, SourceRevision: source.Revision, ExpectedName: source.Name, Region: o.buildRegion, Exposure: o.buildExposure, Domain: o.buildDomain, Port: o.buildPort, Platform: o.buildPlatform, Context: o.buildContext, Dockerfile: o.dockerfile, ComposeSidecar: o.composeSidecar, BuildTimeout: o.timeout, Workflow: &builddomain.QueuedWorkflow{Workflow: checked, AcceptChange: o.acceptWorkflowChange}}, TargetNodes: source.TargetNodes, TargetRegions: source.TargetRegions})
		if err != nil {
			return nil, err
		}
		upload := prepared.Upload
		_ = emit(deployment.Event{Status: "start", Message: "Local build reserved: " + prepared.Run.ID})
		dockerConfig := streams.Getenv("DOCKER_CONFIG")
		if dockerConfig == "" {
			home, err := os.UserHomeDir()
			if err != nil {
				return nil, err
			}
			dockerConfig = filepath.Join(home, ".docker")
		}
		built, buildErr := executor.BuildLocal(ctx, builddomain.LocalImageRequest{ImageRequest: builddomain.ImageRequest{RepoURL: source.RepoURL, RegistryHost: upload.RegistryHost, Repo: upload.Repository, Platform: upload.Platform, Region: o.buildRegion, Context: o.buildContext, Dockerfile: o.dockerfile, ComposeSidecar: o.composeSidecar, BuildTimeout: o.timeout, Proxy: o.localProxy}, Path: source.Path, Tag: upload.Tag, Builder: o.localBuilder, DockerConfig: dockerConfig, DockerContext: streams.Getenv("DOCKER_CONTEXT")}, func(event node.Event) { _ = emit(deployment.Event{Status: "output", Message: event.Line}) })
		if buildErr != nil {
			failCtx, stop := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
			defer stop()
			_ = client.FailLocalBuild(failCtx, prepared.Run.ID, buildErr.Error())
			return nil, buildErr
		}
		// A lost complete response can already mean accepted work. Do not submit again
		// or mark its deployment failed merely because the CLI observer stopped.
		return client.CompleteLocalBuild(ctx, prepared.Run.ID, builddomain.LocalCompleteRequest{BuildResult: built, EnvSecrets: secrets}, emit)
	})
	if err != nil {
		return err
	}
	return success(streams, o, json.RawMessage(result))
}
