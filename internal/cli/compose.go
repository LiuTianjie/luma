package cli

import (
	"context"
	"encoding/json"
	"fmt"
	"path/filepath"
	"time"

	"github.com/LiuTianjie/luma/internal/adapters/controlhttp"
	"github.com/LiuTianjie/luma/internal/adapters/manifest"
	"github.com/LiuTianjie/luma/internal/adapters/nomad"
	"github.com/LiuTianjie/luma/internal/deployment"
)

func composeCommand(ctx context.Context, args []string, o options, streams IO) error {
	if len(args) != 3 || (args[1] != "validate" && args[1] != "render" && args[1] != "deploy") {
		return fmt.Errorf("usage: compose validate|render|deploy SIDECAR")
	}
	sidecar, err := readYAML(args[2])
	if err != nil {
		return err
	}
	composePath, err := manifest.ComposePath(sidecar)
	if err != nil {
		return err
	}
	if !filepath.IsAbs(composePath) {
		composePath = filepath.Join(filepath.Dir(args[2]), composePath)
	}
	compose, err := readYAML(composePath)
	if err != nil {
		return err
	}
	config, configPath, err := loadRenderConfig(o)
	if err != nil {
		return err
	}
	if o.engine != "" {
		config.Engine = o.engine
	}
	var client *controlhttp.Client
	ctx, cancel := context.WithTimeout(ctx, time.Duration(o.timeout)*time.Second)
	defer cancel()
	online := args[1] == "deploy" && !o.dryRun
	var c deployment.Compose
	if online {
		c, err = (manifest.Decoder{}).Compose(string(sidecar), string(compose))
	} else {
		c, err = manifest.DecodeCompose(sidecar, compose)
	}
	if err != nil {
		return err
	}
	if online {
		client, err = clientFor(o, streams)
		if err != nil {
			return err
		}
		defer client.Close()
	}
	app := deployment.Application{ComposeCompiler: nomad.Compiler{}, Submitter: client, Workflows: client}
	var plan deployment.Plan
	if !online {
		plan, err = app.PreviewCompose(c, config)
		if err != nil {
			return err
		}
	}
	if args[1] == "render" {
		_, err := fmt.Fprintln(streams.Out, plan.Artifacts[0].Content)
		return err
	}
	if args[1] == "validate" || o.dryRun {
		return success(streams, o, struct {
			deployment.Plan
			DryRun bool `json:"dryRun,omitempty"`
		}{plan, o.dryRun})
	}
	workflow, err := makeWorkflow(args[2], configPath, c.Name, "compose-deploy", o)
	if err != nil {
		return err
	}
	services := make([]deployment.Service, 0, len(c.Services))
	for _, item := range c.Services {
		services = append(services, item.Service)
	}
	secrets, err := deploymentSecrets(o.deployEnvFile, deployment.RequiredSecretNames(services), string(sidecar), string(compose))
	if err != nil {
		return err
	}
	request := deployment.SubmitRequest{Manifest: string(sidecar), ComposeContent: string(compose), SourceName: args[2], SkipDNS: o.skipDNS, Workflow: workflow, EnvSecrets: secrets, AcceptWorkflowChange: o.acceptWorkflowChange}
	result, err := app.DeployCompose(ctx, c, config, request, eventWriter(streams, o))
	if err != nil {
		return err
	}
	return success(streams, o, json.RawMessage(result))
}
