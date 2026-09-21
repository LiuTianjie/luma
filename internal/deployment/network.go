package deployment

import (
	"context"
	"encoding/json"
	"slices"
	"time"

	"github.com/LiuTianjie/luma/internal/node"
)

type NetworkResult struct {
	Node    string          `json:"node"`
	Ports   []int           `json:"hostPorts"`
	Status  string          `json:"status"`
	Message string          `json:"message"`
	Result  json.RawMessage `json:"result,omitempty"`
}

// CNI cleanup is best effort, as with the former Control. Its result is separate
// from both Nomad allocation health and the public route probe.
func (c *Coordinator) prepareNetwork(ctx context.Context, token string, p Prepared) ([]NetworkResult, error) {
	ports := []int{}
	needsNetwork := false
	for _, s := range p.Services {
		needsNetwork = needsNetwork || s.Port > 0 && (s.Exposure != "tailscale-relay" && s.Exposure != "tcp-relay" || s.PublishPort > 0 || p.Record.Kind == "compose")
		port := s.PublishPort
		if port == 0 && p.Record.Kind == "compose" && (s.Exposure == "tcp-relay" || s.Exposure == "tailscale-relay") {
			port = s.Port
		}
		if port > 0 && !slices.Contains(ports, port) {
			ports = append(ports, port)
		}
	}
	results := []NetworkResult{}
	if !needsNetwork {
		return results, nil
	}
	allocs, err := c.Runtime.Allocations(ctx, p.Record.Slug)
	if err != nil {
		return results, err
	}
	nodes := map[string][]int{}
	for _, a := range allocs {
		if a.JobID != p.Record.Slug || a.Version == nil || p.Record.Runtime == nil || *a.Version != p.Record.Runtime.Version || a.Status != "running" || a.Desired != "run" && a.Desired != "running" && a.Desired != "" {
			continue
		}
		for _, n := range p.Snapshot.Nodes {
			if n.ID == a.NodeID {
				nodePorts := append([]int(nil), ports...)
				for _, port := range append(nodes[n.Name], a.HostPorts...) {
					if !slices.Contains(nodePorts, port) {
						nodePorts = append(nodePorts, port)
					}
				}
				if len(nodePorts) > 0 {
					nodes[n.Name] = nodePorts
				}
			}
		}
	}
	if len(nodes) == 0 && p.Record.Node != "" && len(ports) > 0 {
		nodes[p.Record.Node] = ports
	}
	names := []string{}
	for name := range nodes {
		names = append(names, name)
	}
	slices.Sort(names)
	slices.Sort(ports)
	for _, name := range names {
		ports := nodes[name]
		slices.Sort(ports)
		r := NetworkResult{Node: name, Ports: ports, Status: "skipped", Message: "Agent does not support Nomad CNI hostport repair"}
		n := p.Snapshot.ImageNodes[name]
		if c.NodeTasks != nil && n.Ready && slices.Contains(n.Capabilities, "nomad-cni-repair") && (n.SupportedActions == nil || slices.Contains(*n.SupportedActions, "repair-nomad-cni-hostports")) {
			taskCtx, cancel := context.WithTimeout(ctx, 120*time.Second)
			payload, _ := json.Marshal(map[string]any{"ports": ports})
			raw, taskErr := c.NodeTasks.Run(taskCtx, token, node.TaskOwner{Kind: p.Record.Kind, Slug: p.Record.Slug, OperationID: p.Record.OperationID}, name, "repair-nomad-cni-hostports", "nomad-cni-repair", payload)
			cancel()
			if taskErr == nil {
				r.Status, r.Message, r.Result = "completed", "Nomad CNI hostport repair completed", raw
			} else {
				r.Message = "Nomad CNI hostport repair failed: " + taskErr.Error()
			}
		}
		results = append(results, r)
		if err := ctx.Err(); err != nil {
			return results, err
		}
	}
	return results, nil
}
