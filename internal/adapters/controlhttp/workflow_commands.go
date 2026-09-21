package controlhttp

import (
	"context"
	"encoding/json"
	"github.com/LiuTianjie/luma/internal/deployment"
	"net/url"
)

func (c *Client) ListWorkflows(ctx context.Context) (json.RawMessage, error) {
	return c.json(ctx, "GET", "/v1/workflows", nil)
}
func (c *Client) GetWorkflow(ctx context.Context, name string) (json.RawMessage, error) {
	return c.json(ctx, "GET", "/v1/workflows/"+url.PathEscape(name), nil)
}
func (c *Client) SetWorkflow(ctx context.Context, w deployment.Workflow) (json.RawMessage, error) {
	body := struct {
		Recipe   deployment.Recipe   `json:"recipe"`
		Selector deployment.Selector `json:"selector"`
		Note     *string             `json:"note,omitempty"`
		Name     string              `json:"name"`
		Source   string              `json:"source"`
	}{w.Recipe, w.Selector, w.Note, w.Selector.Name, "manual"}
	return c.json(ctx, "POST", "/v1/workflows", body)
}
