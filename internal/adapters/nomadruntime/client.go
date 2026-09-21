// Package nomadruntime owns Nomad HTTP I/O. Rendering remains a separate adapter;
// rollout success and local data ownership are domain decisions.
package nomadruntime

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"slices"
	"strings"
	"time"

	"github.com/LiuTianjie/luma/internal/deployment"
	"github.com/LiuTianjie/luma/internal/storage"
)

type Client struct {
	base, token string
	http        *http.Client
}

func New(endpoint, token string) (*Client, error) {
	u, err := url.Parse(endpoint)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || u.Path != "" && u.Path != "/" {
		return nil, errors.New("Nomad endpoint must be an explicit HTTP(S) origin without credentials, path or query")
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	return &Client{base: strings.TrimRight(endpoint, "/"), token: token, http: &http.Client{Transport: transport, Timeout: 15 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}, nil
}
func (c *Client) Close() { c.http.CloseIdleConnections() }
func (c *Client) request(ctx context.Context, method, path string, input, output any) error {
	var body io.Reader
	if input != nil {
		raw, err := json.Marshal(input)
		if err != nil {
			return err
		}
		body = bytes.NewReader(raw)
	}
	r, err := http.NewRequestWithContext(ctx, method, c.base+path, body)
	if err != nil {
		return err
	}
	r.Header.Set("Accept", "application/json")
	if input != nil {
		r.Header.Set("Content-Type", "application/json")
	}
	if c.token != "" {
		r.Header.Set("X-Nomad-Token", c.token)
	}
	response, err := c.http.Do(r)
	if err != nil {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return fmt.Errorf("%w: Nomad transport failed; writes are never replayed", deployment.ErrRuntime)
	}
	defer response.Body.Close()
	if response.StatusCode == 404 && method == http.MethodGet {
		return deployment.ErrJobNotFound
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return fmt.Errorf("%w: Nomad HTTP %d", deployment.ErrRuntime, response.StatusCode)
	}
	raw, err := io.ReadAll(io.LimitReader(response.Body, (16<<20)+1))
	if err != nil || len(raw) > 16<<20 {
		return fmt.Errorf("%w: Nomad response interrupted or oversized", deployment.ErrRuntime)
	}
	if len(raw) == 0 || bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
		// Null lists are Nomad's empty result, but null objects are not evidence.
		switch output.(type) {
		case *[]deployment.Allocation, *[]rolloutWire, *[]allocationWire:
			raw = []byte("[]")
		default:
			return fmt.Errorf("%w: Nomad returned no object", deployment.ErrRuntime)
		}
	}
	if json.Unmarshal(raw, output) != nil {
		return fmt.Errorf("%w: invalid Nomad response", deployment.ErrRuntime)
	}
	return nil
}

type constraint struct{ LTarget, RTarget, Operand string }
type jobWire struct {
	ID             string
	JobModifyIndex uint64
	Version        *uint64
	Constraints    []constraint
	TaskGroups     []struct {
		Name        string
		Count       *int
		Constraints []constraint
		Volumes     map[string]json.RawMessage
		Tasks       []struct {
			Name, Driver string
			VolumeMounts []json.RawMessage
			Resources    struct{ MemoryMaxMB int }
			Config       struct {
				Mount []struct {
					Type, Source, Target string
					Readonly             bool
					VolumeOptions        json.RawMessage `json:"volume_options"`
				} `json:"mount"`
				Volumes []string `json:"volumes"`
			}
		}
	}
}

func (c *Client) Inspect(ctx context.Context, id string) (deployment.RuntimeJob, error) {
	var wire jobWire
	err := c.request(ctx, "GET", "/v1/job/"+url.PathEscape(id), nil, &wire)
	if err != nil {
		return deployment.RuntimeJob{}, err
	}
	j := deployment.RuntimeJob{ID: wire.ID, ModifyIndex: wire.JobModifyIndex, Version: wire.Version, Groups: map[string]int{}, Placement: storage.PlacementEvidence{Exists: true}}
	pins := func(list []constraint) {
		for _, p := range list {
			if p.Operand != "=" {
				continue
			}
			switch p.LTarget {
			case "${node.unique.id}":
				j.Placement.PinnedIDs = append(j.Placement.PinnedIDs, p.RTarget)
			case "${meta.luma_node_name}":
				j.Placement.PinnedNames = append(j.Placement.PinnedNames, p.RTarget)
			}
		}
	}
	pins(wire.Constraints)
	for _, g := range wire.TaskGroups {
		if g.Name == "" || g.Count == nil || *g.Count < 0 {
			return j, fmt.Errorf("%w: invalid Nomad group", deployment.ErrRuntime)
		}
		if _, ok := j.Groups[g.Name]; ok {
			return j, fmt.Errorf("%w: duplicate Nomad group", deployment.ErrRuntime)
		}
		j.Groups[g.Name] = *g.Count
		pins(g.Constraints)
		if len(g.Volumes) > 0 {
			j.Placement.UnsupportedStorage = true
		}
		for _, t := range g.Tasks {
			if len(t.VolumeMounts) > 0 {
				j.Placement.UnsupportedStorage = true
			}
			for _, m := range t.Config.Mount {
				if m.Type != "bind" && m.Type != "volume" || len(m.VolumeOptions) > 0 && string(m.VolumeOptions) != "null" && string(m.VolumeOptions) != "{}" {
					j.Placement.UnsupportedStorage = true
				}
				if !m.Readonly && m.Source != "" && !strings.HasSuffix(m.Source, ".sock") {
					j.Placement.Mounts = append(j.Placement.Mounts, storage.Mount{Service: t.Name, Type: m.Type, Source: m.Source, Target: m.Target})
				}
			}
			for _, v := range t.Config.Volumes {
				parts := strings.Split(v, ":")
				if len(parts) < 2 {
					j.Placement.UnsupportedStorage = true
					continue
				}
				if len(parts) > 2 && strings.Contains(","+parts[2]+",", ",ro,") || strings.HasSuffix(parts[0], ".sock") {
					continue
				}
				kind := "volume"
				if strings.HasPrefix(parts[0], "/") {
					kind = "bind"
				}
				j.Placement.Mounts = append(j.Placement.Mounts, storage.Mount{Service: t.Name, Type: kind, Source: parts[0], Target: parts[1]})
			}
		}
	}
	return j, nil
}

type portWire struct{ Value, HostPort int }
type networkWire struct{ ReservedPorts, DynamicPorts, Ports []portWire }
type allocationWire struct {
	AllocatedResources struct {
		Shared struct {
			Ports    []portWire
			Networks []networkWire
		}
		Tasks map[string]struct{ Networks []networkWire }
	}
	Resources struct{ Networks []networkWire }

	ID, NodeID, JobID, TaskGroup, DesiredStatus, ClientStatus string
	JobVersion                                                *uint64
	CreateIndex                                               uint64
	TaskStates                                                map[string]deployment.TaskState
	DeploymentStatus                                          *struct{ Healthy *bool }
}

func (c *Client) Allocations(ctx context.Context, id string) ([]deployment.Allocation, error) {
	var wire []allocationWire
	if err := c.request(ctx, "GET", "/v1/job/"+url.PathEscape(id)+"/allocations", nil, &wire); err != nil {
		return nil, err
	}
	result := make([]deployment.Allocation, 0, len(wire))
	for _, a := range wire {
		var healthy *bool
		if a.DeploymentStatus != nil {
			healthy = a.DeploymentStatus.Healthy
		}
		ports := []int{}
		add := func(values []portWire) {
			for _, v := range values {
				p := v.Value
				if p == 0 {
					p = v.HostPort
				}
				if p > 0 && p <= 65535 && !slices.Contains(ports, p) {
					ports = append(ports, p)
				}
			}
		}
		networks := func(values []networkWire) {
			for _, n := range values {
				add(n.ReservedPorts)
				add(n.DynamicPorts)
				add(n.Ports)
			}
		}
		add(a.AllocatedResources.Shared.Ports)
		networks(a.AllocatedResources.Shared.Networks)
		networks(a.Resources.Networks)
		for _, task := range a.AllocatedResources.Tasks {
			networks(task.Networks)
		}
		slices.Sort(ports)
		result = append(result, deployment.Allocation{HostPorts: ports, ID: a.ID, NodeID: a.NodeID, JobID: a.JobID, Group: a.TaskGroup, Version: a.JobVersion, CreateIndex: a.CreateIndex, Desired: a.DesiredStatus, Status: a.ClientStatus, Tasks: a.TaskStates, Healthy: healthy})
	}
	return result, nil
}
func (c *Client) Register(ctx context.Context, raw json.RawMessage, expected uint64) (deployment.Registration, error) {
	var input struct{ Job json.RawMessage }
	if json.Unmarshal(raw, &input) != nil || len(input.Job) == 0 {
		return deployment.Registration{}, errors.New("invalid compiled Nomad job")
	}
	var job jobWire
	if json.Unmarshal(input.Job, &job) != nil || job.ID == "" {
		return deployment.Registration{}, errors.New("invalid compiled Nomad job")
	}
	memoryMax := false
	for _, g := range job.TaskGroups {
		for _, t := range g.Tasks {
			memoryMax = memoryMax || t.Resources.MemoryMaxMB > 0
		}
	}
	if memoryMax {
		var config struct{ SchedulerConfig map[string]json.RawMessage }
		if err := c.request(ctx, "GET", "/v1/operator/scheduler/configuration", nil, &config); err != nil {
			return deployment.Registration{}, err
		}
		if config.SchedulerConfig == nil {
			return deployment.Registration{}, errors.New("Nomad scheduler configuration missing")
		}
		if string(config.SchedulerConfig["MemoryOversubscriptionEnabled"]) != "true" {
			config.SchedulerConfig["MemoryOversubscriptionEnabled"] = json.RawMessage(`true`)
			var response struct{ Updated bool }
			if err := c.request(ctx, "PUT", "/v1/operator/scheduler/configuration", config.SchedulerConfig, &response); err != nil {
				return deployment.Registration{}, err
			}
			if !response.Updated {
				return deployment.Registration{}, errors.New("Nomad memory oversubscription was not enabled")
			}
		}
	}
	var response struct {
		EvalID         string
		JobModifyIndex uint64
	}
	err := c.request(ctx, "POST", "/v1/jobs", struct {
		Job            json.RawMessage
		EnforceIndex   bool
		JobModifyIndex uint64
	}{input.Job, true, expected}, &response)
	return deployment.Registration{EvalID: response.EvalID, ModifyIndex: response.JobModifyIndex}, err
}
func (c *Client) Evaluation(ctx context.Context, id string) (deployment.Evaluation, error) {
	var w struct {
		ID, JobID, Status, NextEval, BlockedEval, DeploymentID string
		JobModifyIndex                                         uint64
		FailedTGAllocs                                         map[string]json.RawMessage
	}
	err := c.request(ctx, "GET", "/v1/evaluation/"+url.PathEscape(id), nil, &w)
	return deployment.Evaluation{ID: w.ID, JobID: w.JobID, Status: w.Status, Next: w.NextEval, Blocked: w.BlockedEval, DeploymentID: w.DeploymentID, ModifyIndex: w.JobModifyIndex, PlacementFailed: len(w.FailedTGAllocs) > 0}, err
}

type rolloutWire struct {
	ID, JobID, Status           string
	JobModifyIndex, CreateIndex uint64
	JobVersion                  *uint64
	TaskGroups                  map[string]struct{ DesiredTotal int }
}

func (w rolloutWire) project() deployment.Rollout {
	r := deployment.Rollout{ID: w.ID, JobID: w.JobID, Status: w.Status, ModifyIndex: w.JobModifyIndex, Version: w.JobVersion, CreateIndex: w.CreateIndex, Groups: map[string]int{}}
	for name, g := range w.TaskGroups {
		r.Groups[name] = g.DesiredTotal
	}
	return r
}
func (c *Client) Rollout(ctx context.Context, id string) (deployment.Rollout, error) {
	var w rolloutWire
	err := c.request(ctx, "GET", "/v1/deployment/"+url.PathEscape(id), nil, &w)
	return w.project(), err
}
func (c *Client) Rollouts(ctx context.Context, id string) ([]deployment.Rollout, error) {
	var rows []rolloutWire
	if err := c.request(ctx, "GET", "/v1/job/"+url.PathEscape(id)+"/deployments", nil, &rows); err != nil {
		return nil, err
	}
	out := make([]deployment.Rollout, 0, len(rows))
	for _, r := range rows {
		out = append(out, r.project())
	}
	return out, nil
}
