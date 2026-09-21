package controlhttp

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/LiuTianjie/luma/internal/node"
)

// AgentClient exposes only node-authenticated operations. It cannot use saved
// management contexts or issue management mutations through its public API.
type AgentClient struct{ client *Client }

func NewAgent(options Options) (*AgentClient, error) {
	client, err := New(options)
	if err != nil {
		return nil, err
	}
	return &AgentClient{client: client}, nil
}
func (c *AgentClient) Close() { c.client.Close() }
func (c *AgentClient) Lease(ctx context.Context, p node.Presence, wait time.Duration) (*node.Task, error) {
	body := struct {
		node.Presence
		WaitSeconds int `json:"waitSeconds"`
	}{p, min(30, max(1, int(wait/time.Second)))}
	data, err := c.client.json(ctx, "POST", "/v1/node-agent/lease", body)
	if err != nil {
		return nil, err
	}
	var response struct {
		Task json.RawMessage `json:"task"`
	}
	if err := json.Unmarshal(data, &response); err != nil {
		return nil, err
	}
	if len(response.Task) == 0 {
		return nil, fmt.Errorf("Control lease response is missing task")
	}
	if string(response.Task) == "null" {
		return nil, nil
	}
	var task struct {
		ID, Action string
		Payload    json.RawMessage
	}
	if err := json.Unmarshal(response.Task, &task); err != nil {
		return nil, err
	}
	if task.ID == "" || task.Action == "" || !object(task.Payload) {
		return nil, fmt.Errorf("Control returned an invalid node task")
	}
	return &node.Task{ID: task.ID, Action: task.Action, Payload: task.Payload}, nil
}
func (c *AgentClient) Heartbeat(ctx context.Context, p node.Presence) (node.Heartbeat, error) {
	data, err := c.client.json(ctx, "POST", "/v1/node-agent/heartbeat", p)
	if err != nil {
		return node.Heartbeat{}, err
	}
	var response struct {
		CancelRequested bool   `json:"cancelRequested"`
		ActiveTaskID    string `json:"activeTaskId"`
		NodeName        string `json:"nodeName"`
	}
	if err := json.Unmarshal(data, &response); err != nil {
		return node.Heartbeat{}, err
	}
	if response.NodeName == "" || response.ActiveTaskID != p.ActiveTaskID {
		return node.Heartbeat{}, fmt.Errorf("Control heartbeat did not acknowledge the active task")
	}
	return node.Heartbeat{CancelRequested: response.CancelRequested}, nil
}
func (c *AgentClient) Progress(ctx context.Context, identity node.Identity, taskID string, events []node.Event) error {
	body := struct {
		node.Identity
		TaskID string       `json:"taskId"`
		Events []node.Event `json:"events"`
	}{identity, taskID, events}
	data, err := c.client.json(ctx, "POST", "/v1/node-agent/tasks/progress", body)
	if err != nil {
		return err
	}
	var response struct {
		TaskID string `json:"taskId"`
		Events int    `json:"events"`
	}
	if err := json.Unmarshal(data, &response); err != nil {
		return err
	}
	if response.TaskID != taskID {
		return fmt.Errorf("Control progress acknowledged a different task")
	}
	return nil
}
func (c *AgentClient) Complete(ctx context.Context, r node.Receipt) (node.Acknowledgement, error) {
	if err := r.Validate(); err != nil {
		return node.Acknowledgement{}, err
	}
	if !object(r.Result) {
		return node.Acknowledgement{}, fmt.Errorf("agent result must be an object")
	}
	body := struct {
		node.Identity
		TaskID  string          `json:"taskId"`
		Status  string          `json:"status"`
		Message string          `json:"message"`
		Result  json.RawMessage `json:"result"`
	}{r.Identity, r.TaskID, r.Status, r.Message, r.Result}
	data, err := c.client.json(ctx, "POST", "/v1/node-agent/tasks/complete", body)
	if err != nil {
		return node.Acknowledgement{}, err
	}
	var response struct {
		TaskID string `json:"taskId"`
		Status string `json:"status"`
	}
	if err := json.Unmarshal(data, &response); err != nil {
		return node.Acknowledgement{}, err
	}
	if response.TaskID != r.TaskID || !node.Terminal(response.Status) {
		return node.Acknowledgement{}, fmt.Errorf("Control did not acknowledge terminal task result")
	}
	return node.Acknowledgement{TaskID: response.TaskID, Status: response.Status}, nil
}
