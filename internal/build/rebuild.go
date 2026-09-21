package build

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/LiuTianjie/luma/internal/credentials"
	"github.com/LiuTianjie/luma/internal/workflow"
)

func (c *Coordinator) Retry(ctx context.Context, token, id string, request RetryRequest) (Run, error) {
	if _, err := credentials.Authenticate(ctx, c.Identity, token); err != nil {
		return Run{}, err
	}
	prior, err := c.Repository.ReadBuild(ctx, id)
	if err != nil {
		return Run{}, err
	}
	if !Terminal(prior.Status) || prior.Mode == "local" {
		return Run{}, fmt.Errorf("%w: only a completed repository build can be retried", ErrState)
	}
	r := prior.Request
	if name, _ := prior.Result["service"].(string); name != "" {
		r.ExpectedName = name
	} else if name, _ := prior.Result["deployment"].(string); name != "" {
		r.ExpectedName = name
	}
	if r.RepoURL == "" && r.ProviderID == "" {
		r.RepoURL = prior.Source
	}
	r.Queue = request.Queue
	r.EnvSecrets = request.EnvSecrets
	r.Workflow = request.Workflow
	return c.submit(ctx, token, r, id)
}
func (c *Coordinator) Update(ctx context.Context, token string, request UpdateRequest) (Run, error) {
	if _, err := credentials.Authenticate(ctx, c.Identity, token); err != nil {
		return Run{}, err
	}
	name := request.Name
	if name == "" {
		name = request.Stack
	}
	if name == "" {
		return Run{}, fmt.Errorf("%w: application name is required", ErrInput)
	}
	r, err := c.Repository.RebuildRequest(ctx, name)
	if err != nil {
		return Run{}, err
	}
	r.Queue = request.Queue
	r.EnvSecrets = request.EnvSecrets
	r.Workflow = request.Workflow
	return c.Submit(ctx, token, r)
}
func (c *Coordinator) prepareWorkflow(ctx context.Context, r *ControlRequest) error {
	return c.prepareWorkflowMethod(ctx, r, "remote-build")
}
func (c *Coordinator) prepareWorkflowMethod(ctx context.Context, r *ControlRequest, method string) error {
	if r.Workflow == nil {
		return nil
	}
	if c.Workflows == nil {
		return fmt.Errorf("%w: queued workflow storage is unavailable", ErrInput)
	}
	w := *r.Workflow
	if w.Recipe.Method != method {
		return fmt.Errorf("%w: build workflow method does not match execution", ErrInput)
	}
	recipe, _, err := workflow.ValidateRecipe(w.Recipe)
	if err != nil {
		return err
	}
	w.Recipe = recipe
	checked, err := c.Workflows.Check(ctx, workflow.CheckRequest{Recipe: w.Recipe, Selector: w.Selector})
	if err != nil {
		return err
	}
	revision := int64(0)
	w.CheckedName = ""
	if checked.Workflow != nil {
		revision = checked.Workflow.Revision
		w.CheckedName = checked.Workflow.Name
	}
	if int64(w.ExpectedRevision) != revision {
		return workflow.ErrConflict
	}
	if checked.Status == "confirmation-required" && !w.AcceptChange {
		return fmt.Errorf("%w: workflow differs; review and accept the workflow change", ErrState)
	}
	key, err := workflow.RepoKey(r.RepoURL)
	if err != nil {
		return err
	}
	if checked.Selector.RepoKey != "" && checked.Selector.RepoKey != key {
		return fmt.Errorf("%w: workflow and build repository do not match", ErrInput)
	}
	if w.Selector.Name != "" {
		if r.ExpectedName != "" && workflow.Slug(r.ExpectedName) != workflow.Slug(w.Selector.Name) {
			return ErrInput
		}
		r.ExpectedName = w.Selector.Name
	}
	r.Workflow = &w
	return nil
}
func (c *Coordinator) recordWorkflow(ctx context.Context, token string, w *QueuedWorkflow, result map[string]any) {
	if w == nil {
		return
	}
	name, _ := result["service"].(string)
	if name == "" {
		name, _ = result["deployment"].(string)
	}
	revision := int64(w.ExpectedRevision)
	if w.Selector.Name == "" && w.CheckedName != "" && workflow.Slug(name) != workflow.Slug(w.CheckedName) {
		revision = 0
	}
	evidence := map[string]json.RawMessage{}
	for target, source := range map[string]string{"image": "image", "revision": "revision", "buildId": "buildRunId"} {
		evidence[target], _ = json.Marshal(result[source])
	}
	_, err := c.Workflows.Record(ctx, token, workflow.RecordRequest{CheckRequest: workflow.CheckRequest{Recipe: w.Recipe, Selector: w.Selector}, Name: name, Source: "cli-success", Note: w.Note, ExpectedRevision: &revision, Evidence: evidence})
	outcome := map[string]any{"saved": err == nil, "name": name}
	if err != nil {
		outcome["warning"] = err.Error()
	}
	result["workflow"] = outcome
}
