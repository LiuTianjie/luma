package build

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sync"
	"time"

	"github.com/LiuTianjie/luma/internal/credentials"
	"github.com/LiuTianjie/luma/internal/deployment"
	"github.com/LiuTianjie/luma/internal/node"
	"github.com/LiuTianjie/luma/internal/workflow"
)

type Planned struct {
	Request    ControlRequest
	Image      ImageRequest
	ProjectKey string
}
type Planner interface {
	PlanLocalBuild(context.Context, LocalPrepareRequest, deployment.RenderConfig) (Planned, error)
	PlanBuild(context.Context, ControlRequest, deployment.RenderConfig) (Planned, error)
}
type DeploymentDecoder interface {
	LocalBuiltDeployment(ControlRequest, Run, ImageResult) (deployment.ControlRequest, bool, error)
	BuildReplacement(ControlRequest) (string, error)
	BuiltDeployment(ControlRequest, ImageRequest, ImageResult) (deployment.ControlRequest, bool, error)
}
type Deployer interface {
	Deploy(context.Context, string, deployment.ControlRequest, bool, func(deployment.Event) error) (map[string]any, error)
}
type Repository interface {
	LocalRepository
	QueueRepository
	BuildTask(context.Context, string) (node.StoredTask, error)
	RebuildRequest(context.Context, string) (ControlRequest, error)
}
type Coordinator struct {
	Workflows    *workflow.Service
	Repository   Repository
	Identity     credentials.ManagementRepository
	Planner      Planner
	Decoder      DeploymentDecoder
	Deployer     Deployer
	Config       deployment.RenderConfig
	PollInterval time.Duration
}

func (c *Coordinator) interval() time.Duration {
	if c.PollInterval > 0 {
		return c.PollInterval
	}
	return 250 * time.Millisecond
}
func (c *Coordinator) Submit(ctx context.Context, token string, r ControlRequest) (Run, error) {
	return c.submit(ctx, token, r, "")
}
func (c *Coordinator) submit(ctx context.Context, token string, r ControlRequest, retryOf string) (Run, error) {
	if _, err := credentials.Authenticate(ctx, c.Identity, token); err != nil {
		return Run{}, err
	}
	plan, err := c.Planner.PlanBuild(ctx, r, c.Config)
	if err != nil {
		return Run{}, err
	}
	if err := c.prepareWorkflow(ctx, &plan.Request); err != nil {
		return Run{}, err
	}
	key, err := c.Decoder.BuildReplacement(plan.Request)
	if err != nil {
		return Run{}, fmt.Errorf("%w: %s", ErrInput, err)
	}
	var nonce [16]byte
	if _, err := rand.Read(nonce[:]); err != nil {
		return Run{}, err
	}
	return c.Repository.EnqueueBuild(ctx, token, Submission{Run: Run{RetryOf: retryOf, ID: "build-" + hex.EncodeToString(nonce[:]), Source: plan.Request.RepoURL, BuildNode: plan.Request.BuildNode, ProjectKey: plan.ProjectKey, ReplacementKey: key}, Request: plan.Request})
}

// Serve owns accepted work independently of HTTP connection lifetimes. It is
// started once by Control, after restart recovery, and joined before store close.
func (c *Coordinator) Serve(ctx context.Context, workers int) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	var wg sync.WaitGroup
	errs := make(chan error, 1)
	for range max(1, min(8, workers)) {
		wg.Go(func() {
			for ctx.Err() == nil {
				work, err := c.Repository.ClaimBuild(ctx)
				if err == nil && work != nil {
					err = c.execute(ctx, *work)
				}
				if err != nil && ctx.Err() == nil {
					select {
					case errs <- err:
					default:
					}
					cancel()
					return
				}
				if err := wait(ctx, c.interval()); err != nil {
					return
				}
			}
		})
	}
	wg.Wait()
	select {
	case err := <-errs:
		return err
	default:
		return ctx.Err()
	}
}
func wait(ctx context.Context, d time.Duration) error {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}
func (c *Coordinator) execute(parent context.Context, work Work) error {
	result, runErr := c.run(parent, work)
	status, message := "succeeded", "Build and deployment completed"
	if runErr != nil {
		status = "failed"
		message = runErr.Error()
	}
	cleanup, cancel := context.WithTimeout(context.WithoutCancel(parent), 5*time.Second)
	defer cancel()
	return c.Repository.FinishBuild(cleanup, work.Run.ID, status, result, message)
}
func (c *Coordinator) run(parent context.Context, work Work) (map[string]any, error) {
	if work.Run.Mode == "local" {
		return c.runLocal(parent, work)
	}
	plan, err := c.Planner.PlanBuild(parent, work.Request, c.Config)
	if err != nil {
		return nil, err
	}
	// Accepted work retains its source and builder even if defaults change while queued.
	if plan.Request.RepoURL != work.Run.Source || plan.Request.BuildNode != work.Run.BuildNode {
		return nil, ErrState
	}
	ctx, stopWatching := c.watchBuild(parent, work.Run.ID)
	defer stopWatching()
	payload, err := json.Marshal(plan.Image)
	if err != nil {
		return nil, err
	}
	if _, err := c.Repository.AttachBuildTask(ctx, work.Run.ID, payload); err != nil {
		return nil, err
	}
	if err := c.Repository.BuildEvent(ctx, work.Run.ID, Event{Name: "Build image", Status: "start", Message: "Building repository on " + work.Run.BuildNode}); err != nil {
		return nil, err
	}
	buildCtx, stop := context.WithTimeout(ctx, time.Duration(plan.Image.BuildTimeout+120)*time.Second)
	defer stop()
	var built ImageResult
	for {
		task, err := c.Repository.BuildTask(buildCtx, work.Run.ID)
		if err != nil {
			return nil, err
		}
		if node.Terminal(task.Status) {
			if task.Status != "succeeded" {
				return nil, fmt.Errorf("build task %s %s; inspect its progress", task.ID, task.Status)
			}
			if json.Unmarshal(task.Result, &built) != nil {
				return nil, fmt.Errorf("invalid build result")
			}
			break
		}
		if err := wait(buildCtx, c.interval()); err != nil {
			return nil, err
		}
	}
	request, compose, err := c.Decoder.BuiltDeployment(plan.Request, plan.Image, built)
	if err != nil {
		return nil, err
	}
	request.GitSource = &deployment.GitSource{RepoURL: plan.Request.RepoURL, ProviderID: plan.Request.ProviderID, Repository: plan.Request.Repository, Ref: plan.Request.Ref, BuildNode: plan.Request.BuildNode, Context: plan.Request.Context, Dockerfile: plan.Request.Dockerfile, Platform: plan.Request.Platform, RegistryHost: plan.Request.RegistryHost, PushHost: plan.Request.PushHost, Manifest: plan.Request.Manifest, ComposeContent: plan.Request.ComposeContent, ComposeSidecar: plan.Request.ComposeSidecar, BuildRunID: work.Run.ID, Revision: built.SHA, Region: plan.Request.Region, Exposure: plan.Request.Exposure, Domain: plan.Request.Domain, Port: plan.Request.Port}
	if plan.Request.Proxy != nil && *plan.Request.Proxy == "" {
		request.GitSource.ProxyMode = "direct"
	}
	if err := c.Repository.BeginBuildDeployment(ctx, work.Run.ID); err != nil {
		return nil, err
	}
	identity, err := c.Identity.ManagementIdentity(ctx)
	if err != nil {
		return nil, err
	}
	result, err := c.Deployer.Deploy(ctx, identity.Token, request, compose, func(e deployment.Event) error {
		return c.Repository.BuildEvent(ctx, work.Run.ID, Event{Name: "Deploy", Status: e.Status, Message: e.Message})
	})
	if err != nil {
		return nil, err
	}
	if result == nil {
		result = map[string]any{}
	}
	result["buildRunId"] = work.Run.ID
	result["image"] = built.Image
	result["revision"] = built.SHA
	if len(built.Images) > 0 {
		result["images"] = built.Images
	}
	if built.ComposeSidecar != "" {
		result["composeSidecar"] = built.ComposeSidecar
	}
	c.recordWorkflow(ctx, identity.Token, work.Request.Workflow, result)
	return result, nil
}

func (c *Coordinator) watchBuild(parent context.Context, id string) (context.Context, func()) {
	ctx, cancel := context.WithCancel(parent)
	watched := make(chan struct{})
	go func() {
		defer close(watched)
		for {
			r, err := c.Repository.ReadBuild(ctx, id)
			if err != nil || r.CancelRequestedAt > 0 || Terminal(r.Status) {
				cancel()
				return
			}
			if wait(ctx, c.interval()) != nil {
				return
			}
		}
	}()
	return ctx, func() { cancel(); <-watched }
}
