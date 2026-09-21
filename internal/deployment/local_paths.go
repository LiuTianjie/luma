package deployment

import (
	"context"
	"encoding/json"
	"fmt"
	"path"
	"slices"

	"github.com/LiuTianjie/luma/internal/node"
)

type LocalPathPlan struct{ Volume, Node, Path string }
type LocalPathResult struct {
	Volume   string `json:"volume"`
	Node     string `json:"node"`
	Path     string `json:"path"`
	Prepared string `json:"prepared"`
}

func planLocalPaths(c Compose, snapshot Snapshot, hasRunner bool) ([]LocalPathPlan, error) {
	result := []LocalPathPlan{}
	names := []string{}
	for name := range c.Volumes {
		names = append(names, name)
	}
	slices.Sort(names)
	for _, name := range names {
		v := c.Volumes[name]
		if !v.Local() {
			continue
		}
		n := snapshot.ImageNodes[v.LocalNode]
		if !hasRunner || !n.Ready || !slices.Contains(n.Capabilities, "managed-volume-path") || n.SupportedActions != nil && !slices.Contains(*n.SupportedActions, "prepare-managed-volume-path") {
			return nil, inputError("local volume preparation requires a ready Agent with managed-volume-path on the deployment node")
		}
		result = append(result, LocalPathPlan{Volume: name, Node: v.LocalNode, Path: path.Clean(v.LocalPath)})
	}
	return result, nil
}
func (c *Coordinator) prepareLocalPaths(ctx context.Context, token string, p Prepared) ([]LocalPathResult, error) {
	result := []LocalPathResult{}
	prepared := map[string]bool{}
	for _, volume := range p.LocalPaths {
		key := volume.Node + "\n" + volume.Path
		if !prepared[key] {
			payload, _ := json.Marshal(map[string]any{"root": path.Dir(volume.Path), "relative": path.Base(volume.Path), "preserveExisting": true})
			raw, err := c.NodeTasks.Run(ctx, token, node.TaskOwner{Kind: p.Record.Kind, Slug: p.Record.Slug, OperationID: p.Record.OperationID}, volume.Node, "prepare-managed-volume-path", "managed-volume-path", payload)
			if err != nil {
				return result, err
			}
			var reply struct {
				Path string `json:"path"`
			}
			if json.Unmarshal(raw, &reply) != nil || reply.Path != volume.Path {
				return result, fmt.Errorf("node returned an unexpected prepared volume path")
			}
			prepared[key] = true
		}
		result = append(result, LocalPathResult{Volume: volume.Volume, Node: volume.Node, Path: volume.Path, Prepared: "node-local directory ready; existing contents and permissions preserved"})
	}
	return result, nil
}
