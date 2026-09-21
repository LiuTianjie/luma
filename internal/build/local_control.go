package build

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"strings"

	"github.com/LiuTianjie/luma/internal/credentials"
	"github.com/LiuTianjie/luma/internal/deployment"
	"github.com/LiuTianjie/luma/internal/registry"
)

type LocalRepository interface {
	ReserveLocalBuild(context.Context, string, Submission) (Run, error)
	CompleteLocalBuild(context.Context, string, string, LocalCompleteRequest) (Run, error)
	FailLocalBuild(context.Context, string, string, string) (Run, bool, error)
}

func LocalBuildTag(id string) string { return "local-" + strings.TrimPrefix(id, "build-") }
func ValidateLocalResult(run Run, built ImageResult) error {
	if run.Mode != "local" || run.Source != run.Request.RepoURL || built.SHA != LocalBuildTag(run.ID) || (built.Kind != "service" && built.Kind != "compose") {
		return fmt.Errorf("%w: local build result does not match its reservation", ErrInput)
	}
	if built.Image == "" && len(built.Images) == 0 {
		return fmt.Errorf("%w: local build result requires an image", ErrInput)
	}
	values := []string{}
	if built.Image != "" {
		values = append(values, built.Image)
	}
	for _, image := range built.Images {
		values = append(values, image)
	}
	for _, image := range values {
		parsed, err := registry.ParseImage(image)
		if err != nil || parsed.Digest != "" || parsed.Registry != run.Request.RegistryHost || parsed.Tag != LocalBuildTag(run.ID) || (parsed.Path != run.ProjectKey && !strings.HasPrefix(parsed.Path, run.ProjectKey+"/")) {
			return fmt.Errorf("%w: image must use the reserved local project and tag", ErrInput)
		}
	}
	for _, alias := range built.ImageAliases {
		found := false
		for _, image := range built.Images {
			found = found || image == alias
		}
		if !found {
			return fmt.Errorf("%w: image alias does not refer to a built image", ErrInput)
		}
	}
	if built.Manifest == "" || built.Kind == "compose" && built.ComposeContent == "" {
		return fmt.Errorf("%w: local result requires its deployment manifest", ErrInput)
	}
	return nil
}
func (c *Coordinator) PrepareLocal(ctx context.Context, token string, r LocalPrepareRequest) (LocalReservation, error) {
	if _, err := credentials.Authenticate(ctx, c.Identity, token); err != nil {
		return LocalReservation{}, err
	}
	plan, err := c.Planner.PlanLocalBuild(ctx, r, c.Config)
	if err != nil {
		return LocalReservation{}, err
	}
	if err := c.prepareWorkflowMethod(ctx, &plan.Request, "local-build"); err != nil {
		return LocalReservation{}, err
	}
	key, err := c.Decoder.BuildReplacement(plan.Request)
	if err != nil {
		return LocalReservation{}, err
	}
	var nonce [16]byte
	if _, err := rand.Read(nonce[:]); err != nil {
		return LocalReservation{}, err
	}
	run, err := c.Repository.ReserveLocalBuild(ctx, token, Submission{Run: Run{ID: "build-" + hex.EncodeToString(nonce[:]), Source: plan.Request.RepoURL, ProjectKey: plan.ProjectKey, BuildNode: "local", ReplacementKey: key}, Request: plan.Request})
	if err != nil {
		return LocalReservation{}, err
	}
	tag := LocalBuildTag(run.ID)
	return LocalReservation{Run: run, Upload: LocalUpload{RegistryHost: run.Request.RegistryHost, Repository: run.ProjectKey, Tag: tag, Image: run.Request.RegistryHost + "/" + run.ProjectKey + ":" + tag, Platform: run.Request.Platform}}, nil
}
func (c *Coordinator) CompleteLocal(ctx context.Context, token, id string, r LocalCompleteRequest) (Run, error) {
	if _, err := credentials.Authenticate(ctx, c.Identity, token); err != nil {
		return Run{}, err
	}
	run, err := c.Repository.ReadBuild(ctx, id)
	if err != nil {
		return Run{}, err
	}
	if err := ValidateLocalResult(run, r.BuildResult); err != nil {
		return Run{}, err
	}
	if _, _, err := c.Decoder.LocalBuiltDeployment(run.Request, run, r.BuildResult); err != nil {
		return Run{}, fmt.Errorf("%w: %s", ErrInput, err)
	}
	return c.Repository.CompleteLocalBuild(ctx, token, id, r)
}
func (c *Coordinator) runLocal(ctx context.Context, work Work) (map[string]any, error) {
	if work.LocalResult == nil {
		return nil, ErrState
	}
	if err := ValidateLocalResult(work.Run, *work.LocalResult); err != nil {
		return nil, err
	}
	ctx, stop := c.watchBuild(ctx, work.Run.ID)
	defer stop()
	request, compose, err := c.Decoder.LocalBuiltDeployment(work.Request, work.Run, *work.LocalResult)
	if err != nil {
		return nil, err
	}
	request.GitSource = &deployment.GitSource{RepoURL: work.Request.RepoURL, BuildNode: work.Request.BuildNode, Context: work.Request.Context, Dockerfile: work.Request.Dockerfile, Platform: work.Request.Platform, ComposeSidecar: work.Request.ComposeSidecar, BuildRunID: work.Run.ID, Revision: work.Request.SourceRevision, Region: work.Request.Region, Exposure: work.Request.Exposure, Domain: work.Request.Domain, Port: work.Request.Port}
	if err := c.Repository.BeginBuildDeployment(ctx, work.Run.ID); err != nil {
		return nil, err
	}
	identity, err := c.Identity.ManagementIdentity(ctx)
	if err != nil {
		return nil, err
	}
	result, err := c.Deployer.Deploy(ctx, identity.Token, request, compose, func(event deployment.Event) error {
		return c.Repository.BuildEvent(ctx, work.Run.ID, Event{Name: "Deploy", Status: event.Status, Message: event.Message})
	})
	if err != nil {
		return nil, err
	}
	if result == nil {
		result = map[string]any{}
	}
	result["buildRunId"], result["image"], result["revision"] = work.Run.ID, work.LocalResult.Image, work.Request.SourceRevision
	if len(work.LocalResult.Images) > 0 {
		result["images"] = work.LocalResult.Images
	}
	c.recordWorkflow(ctx, identity.Token, work.Request.Workflow, result)
	return result, nil
}
