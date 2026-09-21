package deployment

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"

	"github.com/LiuTianjie/luma/internal/node"
	"github.com/LiuTianjie/luma/internal/registry"
)

func supportsImagePull(n ImageNode) bool {
	return n.Ready && slices.Contains(n.Capabilities, "docker-image") && (n.SupportedActions == nil || slices.Contains(*n.SupportedActions, "resolve-docker-image"))
}

func (c *Coordinator) directImage(ctx context.Context, token string, p *Prepared, s Service) (ImageResult, error) {
	i, err := registry.ParseImage(s.Image)
	r := ImageResult{Requested: s.Image, Selected: s.Image, Deployed: s.Image, Node: s.Node, Platform: imagePlatform(s, p.Snapshot), ResolvedBy: "scheduled-node"}
	if err != nil {
		return r, err
	}
	n := p.Snapshot.ImageNodes[s.Node]
	if s.Node == "" || !supportsImagePull(n) {
		if registry.MutableLatest(i) {
			if c.ImageResolver == nil {
				return r, fmt.Errorf("%w: registry image resolver", ErrNotMigrated)
			}
			auth := registry.RuntimeAuth{}
			if c.ImageCredentials != nil {
				values, err := c.ImageCredentials.DeploymentImageAuth(ctx, map[string]string{"image": s.Image})
				if err != nil {
					return r, err
				}
				auth = values["image"]
				clear(values)
			}
			resolved, err := c.ImageResolver.Resolve(ctx, s.Image, auth)
			if err != nil {
				return r, err
			}
			r.Deployed, err = registry.ResolveResult(s.Image, resolved)
			if err != nil {
				return r, err
			}
			parsed, _ := registry.ParseImage(r.Deployed)
			if parsed.Digest == "" {
				return r, fmt.Errorf("registry resolution did not return a digest")
			}
			r.ResolvedBy = "registry"
			return r, nil
		}
		r.Deferred, r.Reason = true, "Nomad will pull on the scheduled node"
		if s.Node != "" {
			r.Reason = "Target node does not advertise a ready resolve-docker-image action"
		}
		return r, nil
	}
	if c.NodeTasks == nil {
		return r, fmt.Errorf("%w: image task runner", ErrNotMigrated)
	}
	candidates := []string{s.Image}
	if i.Repository == i.Path {
		for _, mirror := range p.RenderConfig.ImageMirrors {
			if host, err := registry.RegistryHost(mirror); err == nil {
				candidates = append(candidates, host+"/"+s.Image)
			}
		}
	}
	var last error
	for _, image := range candidates {
		parsed, err := registry.ParseImage(image)
		if err != nil {
			return r, err
		}
		force := registry.MutableLatest(parsed)
		payload, _ := json.Marshal(map[string]any{"image": image, "forcePull": force, "platform": r.Platform, "timeout": 600})
		raw, err := c.NodeTasks.Run(ctx, token, node.TaskOwner{Kind: p.Record.Kind, Slug: p.Record.Slug, OperationID: p.Record.OperationID}, s.Node, "resolve-docker-image", "docker-image", payload)
		if err != nil {
			if ctx.Err() != nil {
				return r, ctx.Err()
			}
			last = err
			continue
		}
		var result struct {
			Image    string `json:"image"`
			Deployed string `json:"deployed"`
			Platform string `json:"platform"`
		}
		if json.Unmarshal(raw, &result) != nil || result.Image != image || result.Platform != r.Platform {
			return r, fmt.Errorf("invalid target node image result")
		}
		r.Deployed, err = registry.ResolveResult(image, result.Deployed)
		if err != nil {
			return r, err
		}
		resolved, _ := registry.ParseImage(r.Deployed)
		if force && resolved.Digest == "" {
			return r, fmt.Errorf("target node did not return a digest for the mutable image")
		}
		r.Selected, r.Fallback, r.ForcePull, r.ResolvedBy = image, image != s.Image, force, "target-node"
		return r, nil
	}
	return r, fmt.Errorf("target node image resolution failed: %w", last)
}
