package controlhttp

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/LiuTianjie/luma/internal/deployment"
)

func (c *Client) Check(ctx context.Context, w deployment.Workflow) (deployment.WorkflowCheck, error) {
	data, err := c.json(ctx, "POST", "/v1/workflows/check", w)
	if err != nil {
		return deployment.WorkflowCheck{}, err
	}
	var result struct {
		Status   string `json:"status"`
		Workflow struct {
			Revision int    `json:"revision"`
			Name     string `json:"name"`
		} `json:"workflow"`
		Differences json.RawMessage `json:"differences"`
	}
	if err := json.Unmarshal(data, &result); err != nil {
		return deployment.WorkflowCheck{}, err
	}
	return deployment.WorkflowCheck{Name: result.Workflow.Name, Status: result.Status, Revision: result.Workflow.Revision, Differences: string(result.Differences)}, nil
}
func (c *Client) Record(ctx context.Context, w deployment.Workflow, result []byte) error {
	var deployed struct {
		Service    string          `json:"service"`
		Deployment string          `json:"deployment"`
		Image      json.RawMessage `json:"image"`
		Revision   json.RawMessage `json:"revision"`
		BuildRunID string          `json:"buildRunId"`
		BuildID    string          `json:"buildId"`
		Workflow   *struct {
			Saved   bool   `json:"saved"`
			Warning string `json:"warning"`
		} `json:"workflow"`
	}
	if err := json.Unmarshal(result, &deployed); err != nil {
		return err
	}
	if deployed.Workflow != nil {
		if !deployed.Workflow.Saved {
			return fmt.Errorf("%s", deployed.Workflow.Warning)
		}
		return nil
	}
	name := deployed.Service
	if name == "" {
		name = deployed.Deployment
	}
	if name == "" {
		name = w.Selector.Name
	}
	var image any
	if len(deployed.Image) > 0 {
		if err := json.Unmarshal(deployed.Image, &image); err != nil {
			return err
		}
	}
	if value, ok := image.(map[string]any); ok {
		image = value["selected"]
		if image == nil {
			image = value["requested"]
		}
	}
	build := deployed.BuildRunID
	if build == "" {
		build = deployed.BuildID
	}
	body := struct {
		deployment.Workflow
		Name     string         `json:"name"`
		Source   string         `json:"source"`
		Evidence map[string]any `json:"evidence"`
	}{Workflow: w, Name: name, Source: "cli-success", Evidence: map[string]any{"image": image, "revision": deployed.Revision, "buildId": build}}
	data, err := c.json(ctx, "POST", "/v1/workflows", body)
	if err != nil {
		return err
	}
	var saved struct {
		Workflow json.RawMessage `json:"workflow"`
	}
	if err := json.Unmarshal(data, &saved); err != nil {
		return err
	}
	if !object(saved.Workflow) {
		return fmt.Errorf("Control did not return a saved workflow")
	}
	return nil
}
