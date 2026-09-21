package deployment

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"strings"

	"github.com/LiuTianjie/luma/internal/node"
	"github.com/LiuTianjie/luma/internal/registry"
)

type ImageNode struct {
	Name, Region, Platform       string
	HostOS                       string
	Aliases, Capabilities        []string
	SupportedActions             *[]string
	Ready, Builder, DirectEgress bool
}
type ImageCredentialReader interface {
	DeploymentImageAuth(context.Context, map[string]string) (map[string]registry.RuntimeAuth, error)
}
type ImageResolver interface {
	Resolve(context.Context, string, registry.RuntimeAuth) (string, error)
}
type ImageResult struct {
	Requested    string `json:"requested"`
	Selected     string `json:"selected"`
	Deployed     string `json:"deployed"`
	CacheImage   string `json:"cacheImage,omitempty"`
	BuilderNode  string `json:"builderNode,omitempty"`
	Platform     string `json:"platform,omitempty"`
	Cached       bool   `json:"cached"`
	Fallback     bool   `json:"fallback"`
	RegistryAuth bool   `json:"registryAuth"`
	ResolvedBy   string `json:"resolvedBy"`
	Node         string `json:"node,omitempty"`
	ForcePull    bool   `json:"forcePull"`
	Deferred     bool   `json:"deferred,omitempty"`
	Reason       string `json:"reason,omitempty"`
}

func imagePlatform(s Service, snapshot Snapshot) string { return snapshot.ImageNodes[s.Node].Platform }
func cacheBuilder(plan registry.CachePlan, snapshot Snapshot) (ImageNode, error) {
	var selected ImageNode
	for name, n := range snapshot.ImageNodes {
		if name != plan.Builder && !slices.Contains(n.Aliases, plan.Builder) {
			continue
		}
		if selected.Name != "" && selected.Name != name {
			return selected, inputError("ambiguous Builder node identity")
		}
		n.Name = name
		selected = n
	}
	if selected.Name == "" || !selected.Builder || !selected.Ready || !slices.Contains(selected.Capabilities, "runtime-image-cache-v1") || selected.SupportedActions != nil && !slices.Contains(*selected.SupportedActions, "cache-runtime-image") {
		return selected, inputError("image caching requires a declared, ready Builder with runtime-image-cache-v1 capability")
	}
	return selected, nil
}
func (c *Coordinator) validateImagePlans(p Prepared) error {
	for _, s := range p.ImageServices {
		plan, err := registry.PlanCache(s.Image, imagePlatform(s, p.Snapshot), p.Snapshot.Cache)
		if err != nil {
			return inputError(err.Error())
		}
		if plan.Needed {
			if c.NodeTasks == nil {
				return fmt.Errorf("%w: image task runner", ErrNotMigrated)
			}
			if _, err := cacheBuilder(plan, p.Snapshot); err != nil {
				return err
			}
		}
	}
	return nil
}
func (c *Coordinator) cacheImage(ctx context.Context, token string, p *Prepared, s Service) (ImageResult, error) {
	platform := imagePlatform(s, p.Snapshot)
	r := ImageResult{Requested: s.Image, Selected: s.Image, Deployed: s.Image, Platform: platform, ResolvedBy: "scheduled-node"}
	candidates := []string{s.Image}
	parsed, err := registry.ParseImage(s.Image)
	if err != nil {
		return r, err
	}
	if parsed.Repository == parsed.Path {
		for _, mirror := range p.RenderConfig.ImageMirrors {
			if host, err := registry.RegistryHost(mirror); err == nil {
				candidates = append(candidates, host+"/"+s.Image)
			}
		}
	}
	var last error
	for _, image := range candidates {
		plan, err := registry.PlanCache(image, platform, p.Snapshot.Cache)
		if err != nil {
			return r, inputError(err.Error())
		}
		if !plan.Needed {
			r.Selected, r.Deployed = image, plan.Deployed
			if plan.Builder != "" {
				r.BuilderNode, r.CacheImage, r.ResolvedBy = plan.Builder, plan.Deployed, "internal-registry"
			}
			return r, nil
		}
		builder, err := cacheBuilder(plan, p.Snapshot)
		if err != nil {
			return r, err
		}
		proxy := ""
		if !builder.DirectEgress {
			proxy, err = ResolveEgressProxy(builder.Region, p.Snapshot.Egress, p.RenderConfig)
			if err != nil {
				return r, inputError(err.Error())
			}
		}
		payload, err := json.Marshal(map[string]any{"sourceImage": image, "pushImage": plan.Push, "destinationImage": plan.Destination, "platform": platform, "insecure": plan.Insecure, "proxy": proxy, "timeout": 1800})
		if err != nil {
			return r, err
		}
		raw, err := c.NodeTasks.Run(ctx, token, node.TaskOwner{Kind: p.Record.Kind, Slug: p.Record.Slug, OperationID: p.Record.OperationID}, builder.Name, "cache-runtime-image", "runtime-image-cache-v1", payload)
		if err != nil {
			if ctx.Err() != nil {
				return r, ctx.Err()
			}
			last = err
			continue
		}
		var result struct {
			Digest      string `json:"digest"`
			Source      string `json:"sourceImage"`
			Push        string `json:"pushImage"`
			Destination string `json:"destinationImage"`
		}
		if json.Unmarshal(raw, &result) != nil || !registry.ValidDigest(result.Digest) || result.Source != image || result.Push != plan.Push || result.Destination != plan.Destination {
			return r, fmt.Errorf("invalid verified image cache result")
		}
		destination, _ := registry.ParseImage(plan.Destination)
		r.Selected, r.Deployed, r.CacheImage, r.BuilderNode = image, destination.Repository+"@"+result.Digest, plan.Destination, builder.Name
		r.Cached, r.Fallback, r.ResolvedBy = true, image != s.Image, "builder-cache"
		return r, nil
	}
	return r, fmt.Errorf("Builder image cache failed: %w", last)
}

func transformImageJob(raw []byte, replacements map[string]ImageResult, auth map[string]registry.RuntimeAuth) (json.RawMessage, map[string]string, error) {
	var envelope map[string]any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if decoder.Decode(&envelope) != nil {
		return nil, nil, fmt.Errorf("invalid compiled image job")
	}
	job, ok := envelope["Job"].(map[string]any)
	if !ok {
		return nil, nil, fmt.Errorf("invalid compiled image job")
	}
	images := map[string]string{}
	groups, _ := job["TaskGroups"].([]any)
	for _, group := range groups {
		g, _ := group.(map[string]any)
		tasks, _ := g["Tasks"].([]any)
		for _, task := range tasks {
			t, _ := task.(map[string]any)
			name, _ := t["Name"].(string)
			config, _ := t["Config"].(map[string]any)
			if config == nil {
				return nil, nil, fmt.Errorf("invalid compiled image task")
			}
			if r, ok := replacements[name]; ok {
				config["image"] = r.Deployed
			}
			image, _ := config["image"].(string)
			images[name] = image
			delete(config, "auth")
			if value, ok := auth[name]; ok {
				config["auth"] = value
			}
		}
	}
	result, err := json.Marshal(envelope)
	return result, images, err
}
func (c *Coordinator) resolveImages(ctx context.Context, token string, p *Prepared, job json.RawMessage) (json.RawMessage, map[string]ImageResult, error) {
	results := map[string]ImageResult{}
	dedup := map[string]ImageResult{}
	names := make([]string, 0, len(p.ImageServices))
	for name := range p.ImageServices {
		names = append(names, name)
	}
	slices.Sort(names)
	for _, name := range names {
		s := p.ImageServices[name]
		key := s.Image + "\n" + imagePlatform(s, p.Snapshot)
		if p.Snapshot.Cache.PullHost == "" && p.Snapshot.Cache.PushHost == "" {
			key += "\n" + s.Node
		}
		r, ok := dedup[key]
		if !ok {
			var err error
			if p.Snapshot.Cache.PullHost == "" && p.Snapshot.Cache.PushHost == "" {
				r, err = c.directImage(ctx, token, p, s)
			} else {
				r, err = c.cacheImage(ctx, token, p, s)
			}
			if err != nil {
				return nil, results, err
			}
			dedup[key] = r
		}
		results[name] = r
	}
	public, images, err := transformImageJob(job, results, nil)
	if err != nil {
		return nil, results, err
	}
	for i, a := range p.Plan.Artifacts {
		if a.Kind != "job" {
			continue
		}
		// Retain placeholder Env in generated files; replace only public image refs.
		updated, _, err := transformImageJob([]byte(a.Content), results, nil)
		if err != nil {
			return nil, results, err
		}
		p.Plan.Artifacts[i].Content = string(updated)
	}
	if c.ImageCredentials == nil {
		return public, results, nil
	}
	credentials, err := c.ImageCredentials.DeploymentImageAuth(ctx, images)
	if err != nil {
		return nil, results, err
	}
	defer clear(credentials)
	for name, r := range results {
		_, r.RegistryAuth = credentials[name]
		results[name] = r
	}
	runtime, _, err := transformImageJob(public, results, credentials)
	return runtime, results, err
}

// Docker containers remain Linux on macOS hosts backed by a Linux VM.
func RuntimeImagePlatform(osName, arch string) string {
	osName, arch = strings.ToLower(strings.TrimSpace(osName)), strings.ToLower(strings.TrimSpace(arch))
	if slices.Contains([]string{"darwin", "macos", "mac os", "mac os x"}, osName) {
		osName = "linux"
	}
	if arch == "aarch64" || arch == "arm64/v8" {
		arch = "arm64"
	}
	if arch == "x86_64" {
		arch = "amd64"
	}
	if osName == "" || arch == "" {
		return ""
	}
	return osName + "/" + arch
}
