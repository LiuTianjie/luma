package deployment

import (
	"context"
	"fmt"

	"github.com/LiuTianjie/luma/internal/region"
	"github.com/LiuTianjie/luma/internal/storage"
)

// Compose is an ordered set of colocated services, not arbitrary Compose YAML.
type Compose struct {
	Name, Region, ComposePath string
	Services                  []ComposeService
	Volumes                   map[string]storage.Volume
	Warnings                  []string
}
type ComposeService struct {
	Service
	RegionOverride, NetworkMode, StopGracePeriod string
	Mounts                                       []VolumeMount
	HasVolumes                                   bool
}
type VolumeMount struct {
	Kind, Source, Target string
	ReadOnly             bool
}
type Placement struct {
	Region, Node, NodeID string
	HostNetwork          bool
}

func (c Compose) Slug() string { return (Service{Name: c.Name}).Slug() }
func (c Compose) Placement() (Placement, error) {
	return c.placement(true)
}

// PlacementForControl permits an unresolved local node until ownership is
// established from stored/runtime data. Compilers still require resolved pins.
func (c Compose) PlacementForControl() (Placement, error) { return c.placement(false) }
func (c Compose) placement(requireLocalNode bool) (Placement, error) {
	p := Placement{Region: c.Region}
	if c.Slug() == "" || len(c.Services) == 0 {
		return p, fmt.Errorf("compose deployment requires a name and services")
	}
	if _, err := region.Parse(c.Region); err != nil {
		return p, err
	}
	regions, nodes, modes := map[string]bool{}, map[string]bool{}, map[string]bool{}
	ports, labels := map[int]string{}, map[string]bool{}
	for _, s := range c.Services {
		if s.NodeID != "" {
			if p.NodeID != "" && p.NodeID != s.NodeID {
				return p, fmt.Errorf("conflicting resolved Compose node IDs")
			}
			p.NodeID = s.NodeID
		}
		if err := s.Validate(); err != nil {
			return p, fmt.Errorf("compose service %s: %w", s.Name, err)
		}
		if s.Exposure == "cloudflare-tunnel" {
			return p, fmt.Errorf("compose cloudflare-tunnel is unsupported; use a native manifest")
		}
		if s.RegionOverride != "" {
			regions[s.RegionOverride] = true
		}
		if s.Node != "" {
			nodes[s.Node] = true
		}
		if labels[s.Slug()] {
			return p, fmt.Errorf("compose service names collide after normalization: %s", s.Name)
		}
		labels[s.Slug()] = true
		if s.NetworkMode != "host" && s.NetworkMode != "bridge" {
			return p, fmt.Errorf("compose network_mode must be host or bridge")
		}
		modes[s.NetworkMode] = true
		if s.NetworkMode == "host" && (s.Exposure != "none" || s.PublishPort > 0) {
			return p, fmt.Errorf("host networking requires exposure none without publishPort")
		}
		if s.Exposure == "tcp-relay" {
			port := s.PublishPort
			if port == 0 {
				port = s.Port
			}
			if previous := ports[port]; previous != "" {
				return p, fmt.Errorf("tcp-relay publishPort %d is already used by %s", port, previous)
			}
			ports[port] = s.Name
		}
	}
	local := false
	for _, v := range c.Volumes {
		if err := v.Validate(); err != nil {
			return p, err
		}
		local = local || v.Local()
		if v.LocalNode != "" {
			nodes[v.LocalNode] = true
		}
	}
	if len(regions) > 1 || len(nodes) > 1 {
		return p, fmt.Errorf("a Compose group runs in one region on one node; split conflicting pins into separate deployments")
	}
	if len(modes) > 1 {
		return p, fmt.Errorf("compose services cannot mix host and bridge networks")
	}
	for value := range regions {
		p.Region = value
	}
	for value := range nodes {
		p.Node = value
	}
	if requireLocalNode && local && p.Node == "" {
		return p, fmt.Errorf("local volume paths require an explicit deployment node for local rendering")
	}
	p.HostNetwork = modes["host"]
	return p, nil
}

type ComposeCompiler interface {
	CompileCompose(Compose, RenderConfig) (Plan, error)
}

func (a Application) PreviewCompose(c Compose, config RenderConfig) (Plan, error) {
	if _, err := c.Placement(); err != nil {
		return Plan{}, err
	}
	if config.Engine != "" && config.Engine != "nomad" {
		return Plan{}, fmt.Errorf("engine must be nomad")
	}
	if a.ComposeCompiler == nil {
		return Plan{}, fmt.Errorf("compose compiler is not configured")
	}
	return a.ComposeCompiler.CompileCompose(c, config)
}
func (a Application) DeployCompose(ctx context.Context, c Compose, config RenderConfig, request SubmitRequest, emit func(Event) error) ([]byte, error) {
	// The Control resolves data ownership before compiling. Local render/validate
	// still require an explicit pin; online submission may use the existing owner.
	if _, err := c.PlacementForControl(); err != nil {
		return nil, err
	}
	if config.Engine != "" && config.Engine != "nomad" {
		return nil, fmt.Errorf("engine must be nomad")
	}
	if request.ComposeContent == "" {
		return nil, fmt.Errorf("compose content is required")
	}
	return a.submit(ctx, request, emit)
}
