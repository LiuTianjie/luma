package cli

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/LiuTianjie/luma/internal/adapters/manifest"
	"github.com/LiuTianjie/luma/internal/adapters/nomad"
	"github.com/LiuTianjie/luma/internal/deployment"
	"io"
	"os"
	"time"
)

func readYAML(path string) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, manifest.MaxBytes+1))
	if err != nil {
		return nil, err
	}
	if len(data) > manifest.MaxBytes {
		return nil, fmt.Errorf("YAML exceeds size limit")
	}
	return data, nil
}
func serviceCommand(ctx context.Context, command string, args []string, o options, streams IO) error {
	if len(args) != 2 {
		return fmt.Errorf("usage: %s MANIFEST", command)
	}
	data, err := readYAML(args[1])
	if err != nil {
		return err
	}
	service, err := manifest.DecodeService(data)
	if err != nil {
		return err
	}
	config, configPath, err := loadRenderConfig(o)
	if err != nil {
		return err
	}
	if o.engine != "" {
		if o.engine != "nomad" {
			return fmt.Errorf("engine must be nomad")
		}
		service.Engine = o.engine
	}
	app := deployment.Application{Compiler: nomad.Compiler{}}
	plan, err := app.Preview(service, config)
	if err != nil {
		return err
	}
	if command == "render" {
		_, err := fmt.Fprintln(streams.Out, plan.Artifacts[0].Content)
		return err
	}
	if command == "validate" || o.dryRun {
		return success(streams, o, struct {
			deployment.Plan
			DryRun bool `json:"dryRun,omitempty"`
		}{plan, o.dryRun})
	}
	client, err := clientFor(o, streams)
	if err != nil {
		return err
	}
	defer client.Close()
	app.Submitter = client
	app.Workflows = client
	workflow, err := makeWorkflow(args[1], configPath, service.Name, "image-deploy", o)
	if err != nil {
		return err
	}
	secrets, err := deploymentSecrets(o.deployEnvFile, deployment.RequiredSecretNames([]deployment.Service{service}), string(data))
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(ctx, time.Duration(o.timeout)*time.Second)
	defer cancel()
	result, err := app.Deploy(ctx, service, config, deployment.SubmitRequest{Manifest: string(data), SourceName: args[1], SkipDNS: o.skipDNS, EnvSecrets: secrets, Workflow: workflow, AcceptWorkflowChange: o.acceptWorkflowChange}, eventWriter(streams, o))
	if err != nil {
		return err
	}
	return success(streams, o, json.RawMessage(result))
}
