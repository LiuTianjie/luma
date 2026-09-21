package deployment

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/LiuTianjie/luma/internal/credentials"
	"github.com/LiuTianjie/luma/internal/node"
	regionpolicy "github.com/LiuTianjie/luma/internal/region"
	"github.com/LiuTianjie/luma/internal/registry"
	"github.com/LiuTianjie/luma/internal/storage"
)

var ErrInput = errors.New("invalid deployment request")
var ErrNotMigrated = errors.New("deployment integration has not been migrated")
var ErrConflict = errors.New("deployment changed while preparing; inspect the latest state before retrying")

type ControlRequest struct {
	GitSource        *GitSource        `json:"-"`
	Manifest         string            `json:"manifest"`
	ComposeContent   string            `json:"composeContent,omitempty"`
	SourceName       string            `json:"sourceName"`
	SkipDNS          bool              `json:"skipDns"`
	SkipOrchestrator bool              `json:"skipOrchestrator"`
	EnvSecrets       map[string]string `json:"envSecrets,omitempty"`
	Origin           string            `json:"origin,omitempty"`
	Engine           string            `json:"engine,omitempty"`
}
type Decoder interface {
	Service(string) (Service, error)
	Compose(string, string) (Compose, error)
	Pin(string, string, bool) (string, error)
	PinCompose(string, string, []string) (string, error)
}
type Record struct {
	GitSource           *GitSource                   `json:"gitSource"`
	Health              map[string]RouteHealth       `json:"health"`
	Origin              string                       `json:"origin"`
	Region              string                       `json:"region"`
	Node                string                       `json:"node,omitempty"`
	NodeID              string                       `json:"nodeId,omitempty"`
	Kind                string                       `json:"kind"`
	Name                string                       `json:"name"`
	Slug                string                       `json:"slug"`
	Manifest            string                       `json:"manifest"`
	ComposeContent      string                       `json:"composeContent,omitempty"`
	SourceName          string                       `json:"sourceName"`
	Status              string                       `json:"status"`
	LastError           string                       `json:"lastError"`
	OperationID         string                       `json:"operationId"`
	UpdatedAt           int64                        `json:"updatedAt"`
	LocalStorage        *storage.Binding             `json:"localStorage,omitempty"`
	Runtime             *RolloutResult               `json:"runtime,omitempty"`
	Images              map[string]ImageResult       `json:"images,omitempty"`
	Requirements        map[string]RequirementReport `json:"requirements,omitempty"`
	TCPRelayPorts       []int                        `json:"tcpRelayPorts"`
	IngressRefreshPorts []int                        `json:"ingressRefreshPorts"`
	NetworkPrepared     []NetworkResult              `json:"networkPrepared,omitempty"`
	StoragePrepared     []LocalPathResult            `json:"storagePrepared,omitempty"`
}
type Snapshot struct {
	ClusterID, Revision string
	Previous            *Record
	Nodes               []storage.Node
	NodeAddresses       map[string]NodeAddress
	Egress              EgressSettings
	Cache               registry.CacheConfig
	ImageNodes          map[string]ImageNode
	RelayReservations   []RelayReservation
}
type Repository interface {
	DeploymentSnapshot(context.Context, string, string) (Snapshot, error)
	BeginDeployment(context.Context, string, string, Record, map[string]string) error
	FinishDeployment(context.Context, Record) error
	RecoverDeployments(context.Context) error
}
type Coordinator struct {
	Decoder               Decoder
	Compiler              Compiler
	ComposeCompiler       ComposeCompiler
	Repository            Repository
	Secrets               credentials.SecretReader
	ImageCredentials      ImageCredentialReader
	NodeTasks             node.CommandTaskRunner
	Prerequisites         PrerequisiteReader
	ImageResolver         ImageResolver
	CacheInsecure         *bool
	Runtime               Runtime
	Config                RenderConfig
	DNSConfig             DNSConfig
	DNS                   DNSManager
	Files                 DeploymentFiles
	Prober                RouteProber
	Timeout, PollInterval time.Duration
	mu                    sync.Mutex
}
type Prepared struct {
	Record        Record
	Plan          Plan
	Snapshot      Snapshot
	ExpectedIndex uint64
	Services      []Service
	DNSRecords    []DNSRecord
	ShadowRoutes  []string
	ImageServices map[string]Service
	RenderConfig  RenderConfig
	LocalPaths    []LocalPathPlan
}

func inputError(message string) error { return fmt.Errorf("%w: %s", ErrInput, message) }
func serviceMounts(s Service) []storage.Mount {
	result := []storage.Mount{}
	for _, v := range s.Volumes {
		p := strings.Split(v, ":")
		if len(p) < 2 || p[0] == "" || p[1] == "" || strings.HasSuffix(p[0], ".sock") || len(p) > 2 && strings.Contains(","+p[2]+",", ",ro,") {
			continue
		}
		kind := "volume"
		if strings.ContainsAny(p[0][:1], "/.~") {
			kind = "bind"
		}
		result = append(result, storage.Mount{Type: kind, Source: p[0], Target: p[1]})
	}
	return result
}
func composeMounts(c Compose) []storage.Mount {
	result := []storage.Mount{}
	for _, s := range c.Services {
		for _, m := range s.Mounts {
			if m.ReadOnly || strings.HasSuffix(m.Source, ".sock") {
				continue
			}
			mount := storage.Mount{Service: s.Name, Type: m.Kind, Source: m.Source, Target: m.Target}
			if v, ok := c.Volumes[m.Source]; ok && v.Local() {
				mount.Type = "bind"
				mount.Source = v.LocalPath
			}
			result = append(result, mount)
		}
	}
	return result
}

// This lane is wired incrementally, never silently skipping external integrations.
// Build workflows still need the build queue and execution integrations.
func migratedService(s Service) error {
	if s.Build != nil {
		return fmt.Errorf("%w: build requires the remaining Control integrations", ErrNotMigrated)
	}
	return nil
}
func (c *Coordinator) Prepare(ctx context.Context, r ControlRequest, compose bool) (Prepared, error) {
	p := Prepared{ImageServices: map[string]Service{}}
	if strings.TrimSpace(r.Manifest) == "" || r.SkipOrchestrator || r.Engine != "" && r.Engine != "nomad" {
		return p, inputError("manifest and actual Nomad execution are required")
	}
	var s Service
	var cp Compose
	var err error
	region := ""
	needsProxy := false
	requested := []string{}
	mounts := []storage.Mount{}
	if compose {
		cp, err = c.Decoder.Compose(r.Manifest, r.ComposeContent)
		if err != nil {
			return p, inputError(err.Error())
		}
		placement, err := cp.PlacementForControl()
		if err != nil {
			return p, inputError(err.Error())
		}
		region = placement.Region
		for i := range cp.Services {
			cp.Services[i].Region = region
			if err := cp.Services[i].Validate(); err != nil {
				return p, inputError(err.Error())
			}
		}
		p.Record = Record{Kind: "compose", Name: cp.Name, Slug: cp.Slug()}
		mounts = composeMounts(cp)
		for _, item := range cp.Services {
			needsProxy = needsProxy || needsEgressDefaults(item.Service)
			if err := migratedService(item.Service); err != nil {
				return p, err
			}
			requested = append(requested, item.Node)
		}
		for _, v := range cp.Volumes {
			requested = append(requested, v.LocalNode)
		}
	} else {
		s, err = c.Decoder.Service(r.Manifest)
		if err != nil {
			return p, inputError(err.Error())
		}
		if err := migratedService(s); err != nil {
			return p, err
		}
		region = s.Region
		needsProxy = needsEgressDefaults(s)
		requested = append(requested, s.Node)
		mounts = serviceMounts(s)
		if len(mounts) > 0 && s.Replicas != 1 {
			return p, inputError("local persistent data requires replicas: 1")
		}
		p.Record = Record{Kind: "service", Name: s.Name, Slug: s.Slug()}
	}
	p.Snapshot, err = c.Repository.DeploymentSnapshot(ctx, p.Record.Kind, p.Record.Slug)
	if err != nil {
		return p, err
	}
	if _, err := regionpolicy.EgressMode(region, p.Snapshot.Egress.Regions); err != nil {
		return p, inputError(err.Error())
	}
	if p.Snapshot.Previous != nil && p.Snapshot.Previous.Name != p.Record.Name {
		return p, ErrConflict
	}
	previous := storage.Binding{}
	if old := p.Snapshot.Previous; old != nil {
		if old.LocalStorage != nil {
			previous = *old.LocalStorage
		} else if old.Manifest != "" {
			if compose {
				oldSpec, e := c.Decoder.Compose(old.Manifest, old.ComposeContent)
				if e != nil {
					return p, fmt.Errorf("%w: previous storage manifest requires explicit migration", storage.ErrOwnership)
				}
				previous.Mounts = composeMounts(oldSpec)
				for _, item := range oldSpec.Services {
					if item.Node != "" {
						if previous.Node != "" && previous.Node != item.Node {
							return p, storage.ErrOwnership
						}
						previous.Node = item.Node
					}
				}
			} else {
				oldSpec, e := c.Decoder.Service(old.Manifest)
				if e != nil {
					return p, fmt.Errorf("%w: previous storage manifest requires explicit migration", storage.ErrOwnership)
				}
				previous.Mounts = serviceMounts(oldSpec)
				previous.Node = oldSpec.Node
			}
		}
	}
	job, err := c.Runtime.Inspect(ctx, p.Record.Slug)
	if err != nil && !errors.Is(err, ErrJobNotFound) {
		return p, err
	}
	if err == nil {
		if job.ID != p.Record.Slug {
			return p, runtimeError("Nomad job identity mismatch")
		}
		p.ExpectedIndex = job.ModifyIndex
	}
	if !compose {
		for i := range job.Placement.Mounts {
			job.Placement.Mounts[i].Service = ""
		}
	}
	persistent := len(mounts) > 0 || len(previous.Mounts) > 0 || previous.NodeID != "" || job.Placement.UnsupportedStorage || len(job.Placement.Mounts) > 0
	if persistent {
		if job.Placement.Exists {
			allocs, e := c.Runtime.Allocations(ctx, p.Record.Slug)
			if e != nil {
				return p, e
			}
			active := []Allocation{}
			var latest uint64
			for _, a := range allocs {
				if a.JobID != "" && a.JobID != p.Record.Slug {
					return p, runtimeError("allocation belongs to another job")
				}
				if a.Status == "running" || a.Status == "unknown" {
					active = append(active, a)
				}
				latest = max(latest, a.CreateIndex)
			}
			if len(active) == 0 && len(job.Placement.PinnedIDs)+len(job.Placement.PinnedNames) == 0 {
				for _, a := range allocs {
					if a.CreateIndex == latest {
						active = append(active, a)
					}
				}
			}
			for _, a := range active {
				if a.NodeID == "" {
					return p, storage.ErrOwnership
				}
				job.Placement.OwnerIDs = append(job.Placement.OwnerIDs, a.NodeID)
			}
		}
		binding, e := storage.Ownership(storage.OwnershipRequest{Region: region, Requested: requested, Previous: previous, PreviousRecord: len(previous.Mounts) > 0, Current: mounts, Runtime: job.Placement, Nodes: p.Snapshot.Nodes})
		if e != nil {
			return p, e
		}
		p.Record.LocalStorage = &binding
		requested = []string{binding.Node}
	}
	// Explicit pins are canonicalized to a stable Nomad node ID even for stateless jobs.
	var selected *storage.Node
	for _, wanted := range requested {
		if wanted == "" {
			continue
		}
		matched := false
		for _, n := range p.Snapshot.Nodes {
			if n.Name == wanted || n.ID == wanted || slices.Contains(n.Aliases, wanted) {
				if selected != nil && selected.ID != n.ID {
					return p, inputError("conflicting node pins")
				}
				matched = true
				copy := n
				selected = &copy
			}
		}
		if !matched {
			return p, inputError("unknown node pin")
		}
	}
	if selected != nil && (!selected.Ready || selected.Region != region || selected.ID == "") {
		return p, inputError("selected node is unavailable or outside the deployment region")
	}
	p.Record.Region = region
	p.Record.Origin = "cli"
	if r.Origin == "dashboard" {
		p.Record.Origin = "dashboard"
	}
	p.Record.Manifest = r.Manifest
	p.Record.ComposeContent = r.ComposeContent
	p.Record.SourceName = r.SourceName
	p.Record.GitSource = r.GitSource
	if p.Record.SourceName == "" {
		p.Record.SourceName = "service.yaml"
		if compose {
			p.Record.SourceName = "luma.compose.yml"
		}
	}
	if selected != nil {
		p.Record.Node = selected.Name
		p.Record.NodeID = selected.ID
		if compose {
			names := []string{}
			for i := range cp.Services {
				cp.Services[i].Node = selected.Name
				cp.Services[i].NodeID = selected.ID
				names = append(names, cp.Services[i].Name)
			}
			for key, v := range cp.Volumes {
				if v.Local() {
					v.LocalNode = selected.Name
					cp.Volumes[key] = v
				}
			}
			p.Record.Manifest, err = c.Decoder.PinCompose(r.Manifest, selected.Name, names)
		} else {
			s.Node = selected.Name
			s.NodeID = selected.ID
			p.Record.Manifest, err = c.Decoder.Pin(r.Manifest, selected.Name, false)
		}
		if err != nil {
			return p, err
		}
	}
	config := c.Config
	if needsProxy {
		config.EgressProxyURL, err = ResolveEgressProxy(region, p.Snapshot.Egress, config)
		if err != nil {
			return p, inputError(err.Error())
		}
	}
	config.Nodes = maps.Clone(c.Config.Nodes)
	if config.Nodes == nil {
		config.Nodes = map[string]NodeAddress{}
	}
	for name, n := range p.Snapshot.NodeAddresses {
		old := config.Nodes[name]
		if n.Address != "" {
			old.Address = n.Address
		}
		if n.Region != "" {
			old.Region = n.Region
		}
		if n.PublicIP != "" {
			old.PublicIP = n.PublicIP
		}
		if len(n.Roles) > 0 {
			old.Roles = n.Roles
		}
		old.LocalIngress = old.LocalIngress || n.LocalIngress
		config.Nodes[name] = old
	}
	if compose {
		p.LocalPaths, err = planLocalPaths(cp, p.Snapshot, c.NodeTasks != nil)
		if err != nil {
			return p, err
		}
		for i := range cp.Services {
			if selected != nil && p.Snapshot.ImageNodes[selected.Name].HostOS == "darwin" && cp.Services[i].Port > 0 && cp.Services[i].Exposure != "none" && cp.Services[i].Exposure != "cloudflare-tunnel" {
				return p, inputError("Compose bridge exposure is unavailable on macOS; select a Linux node")
			}
			cp.Services[i].Service, err = ResolveRelay(cp.Services[i].Service, config.Nodes)
			if err != nil {
				return p, err
			}
		}
		p.Plan, err = c.ComposeCompiler.CompileCompose(cp, config)
		for _, item := range cp.Services {
			p.ImageServices[item.Name] = item.Service
			service := item.Service
			service.Name = cp.Slug() + "-" + item.Slug()
			p.Services = append(p.Services, service)
		}
	} else {
		s, err = ResolveRelay(s, config.Nodes)
		if err != nil {
			return p, err
		}
		p.Plan, err = c.Compiler.Compile(s, config)
		p.Services = []Service{s}
		p.ImageServices[s.Slug()] = s
	}
	if err != nil {
		return p, err
	}
	for _, service := range p.Services {
		if service.Exposure != "none" {
			if c.Files == nil || c.DNS == nil || c.Prober == nil {
				return p, publicIntegrationError()
			}
			if shadow := ShadowRoute(service, config); shadow != "" {
				p.ShadowRoutes = append(p.ShadowRoutes, shadow)
			}
		}
	}
	p.Record.TCPRelayPorts = RelayPorts(p.Services)
	if err := CheckRelayPorts(p.Record.Kind, p.Record.Slug, p.Record.TCPRelayPorts, p.Snapshot.RelayReservations); err != nil {
		return p, err
	}
	p.Record.IngressRefreshPorts = RelayRefreshPorts(p.Record.TCPRelayPorts, p.Snapshot.RelayReservations)
	if len(p.Record.IngressRefreshPorts) > 0 {
		p.Plan.Warnings = append(p.Plan.Warnings, fmt.Sprintf("New TCP ports %v require a Traefik static entrypoint and firewall refresh via luma update manager", p.Record.IngressRefreshPorts))
	}
	p.DNSRecords, err = PlanDNS(p.Services, c.DNSConfig, config.Nodes, r.SkipDNS)
	if err != nil {
		return p, err
	}
	if c.Files != nil {
		err = c.Files.Validate(p.Plan, p.ShadowRoutes)
	}
	p.RenderConfig = config
	p.Snapshot.Cache.Insecure = c.CacheInsecure
	if err == nil {
		err = c.validateImagePlans(p)
	}
	return p, err
}
func (c *Coordinator) Deploy(ctx context.Context, token string, r ControlRequest, compose bool, emit func(Event) error) (map[string]any, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	p, err := c.Prepare(ctx, r, compose)
	if err != nil {
		return nil, err
	}
	names, required := deploymentSecretNames(r, p.Services)
	snapshot := credentials.SecretSnapshot{}
	if len(names) > 0 || r.EnvSecrets != nil {
		if c.Secrets == nil {
			return nil, fmt.Errorf("%w: deployment secret repository", ErrNotMigrated)
		}
		snapshot, err = c.Secrets.ReadDeploymentSecrets(ctx, p.Record.Slug, names)
		if err != nil {
			return nil, err
		}
	}
	resolved, err := credentials.ResolveSecrets(p.Record.Slug, names, required, r.EnvSecrets, snapshot)
	if err != nil {
		return nil, err
	}
	defer clear(resolved.Values)
	defer clear(resolved.Imports)
	p.Record.Requirements, err = c.RequirementReports(ctx, r, p, resolved.Values)
	if err != nil {
		return nil, err
	}
	for name, report := range p.Record.Requirements {
		if err := report.Require(name, r.Origin != "dashboard"); err != nil {
			return nil, err
		}
	}
	var job json.RawMessage
	for _, a := range p.Plan.Artifacts {
		if a.Kind == "job" {
			job, err = resolveJobSecrets([]byte(a.Content), resolved.Values)
			if err != nil {
				return nil, err
			}
		}
	}
	var id [16]byte
	if _, err := rand.Read(id[:]); err != nil {
		return nil, err
	}
	p.Record.OperationID = "deploy-" + hex.EncodeToString(id[:])
	p.Record.Status = "pending"
	p.Record.UpdatedAt = time.Now().Unix()
	if err := c.Repository.BeginDeployment(ctx, token, p.Snapshot.Revision, p.Record, resolved.Imports); err != nil {
		return nil, err
	}
	finish := func(failure error) error {
		p.Record.Status = "active"
		p.Record.LastError = ""
		if failure != nil {
			p.Record.Status = "failed_partial"
			p.Record.LastError = failure.Error()
		}
		p.Record.UpdatedAt = time.Now().Unix()
		writeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
		defer cancel()
		return c.Repository.FinishDeployment(writeCtx, p.Record)
	}
	if emit != nil {
		if err := emit(Event{Status: "start", Message: "Preparing deployment images and node-local storage"}); err != nil {
			_ = finish(err)
			return nil, err
		}
	}
	timeout := c.Timeout
	if timeout <= 0 {
		timeout = 10 * time.Minute
	}
	runCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	written, dnsMessages := []string{}, []string{}
	job, p.Record.Images, err = c.resolveImages(runCtx, token, &p, job)
	if err == nil {
		p.Record.StoragePrepared, err = c.prepareLocalPaths(runCtx, token, p)
	}
	if err == nil && c.Files != nil {
		written, err = c.Files.WriteJobs(runCtx, p.Plan)
	}
	if err == nil && c.DNS != nil {
		dnsMessages, err = c.DNS.Sync(runCtx, c.DNSConfig, p.DNSRecords)
	}
	var reg Registration
	if err == nil {
		reg, err = c.Runtime.Register(runCtx, job, p.ExpectedIndex)
	}
	if err == nil {
		var result RolloutResult
		result, err = (RolloutMonitor{Runtime: c.Runtime, PollInterval: c.PollInterval}).Wait(runCtx, p.Record.Slug, reg)
		p.Record.Runtime = &result
	}
	if err == nil {
		p.Record.NetworkPrepared, err = c.prepareNetwork(runCtx, token, p)
	}
	if err == nil && c.Files != nil {
		var routes []string
		routes, err = c.Files.ReconcileRoutes(runCtx, p.Plan, p.ShadowRoutes)
		written = append(written, routes...)
	}
	if err == nil {
		p.Record.Health = map[string]RouteHealth{}
		for _, service := range p.Services {
			if service.Exposure == "none" {
				health := RouteHealth{Status: "not-configured", Kind: "nomad-check", Target: service.Name, Message: "No service healthcheck is configured.", CheckedAt: time.Now().Unix()}
				if service.Healthcheck.Present {
					health.Status = "configured"
					health.Message = "Nomad health check configured; runtime allocation health is reported by Nomad."
				}
				p.Record.Health[service.Name] = health
				continue
			}
			var health RouteHealth
			health, err = c.Prober.Probe(runCtx, service)
			p.Record.Health[service.Name] = health
			if err != nil {
				break
			}
		}
	}
	if saveErr := finish(err); saveErr != nil {
		return nil, saveErr
	}
	if err != nil {
		return nil, err
	}
	result := map[string]any{"clusterId": p.Snapshot.ClusterID, "sourceName": p.Record.SourceName, "orchestrator": p.Record.Runtime, "operationId": p.Record.OperationID, "written": written, "dns": dnsMessages, "health": p.Record.Health, "storagePreparation": "existing node-local mounts preserved", "secrets": resolved.Info}
	health, probes := []RouteHealth{}, []string{}
	result["images"] = p.Record.Images
	result["networkPrepared"] = p.Record.NetworkPrepared
	result["ingressRefreshPorts"] = p.Record.IngressRefreshPorts
	result["warnings"] = p.Plan.Warnings
	result["requirements"] = p.PublicRequirements(p.Record.Requirements)
	result["storagePrepared"] = p.Record.StoragePrepared
	for _, service := range p.Services {
		h := p.Record.Health[service.Name]
		health = append(health, h)
		if service.Exposure != "none" {
			probes = append(probes, h.Message)
		}
	}
	if len(dnsMessages) == 0 {
		message := "DNS skipped: dns.provider is not cloudflare"
		if !compose && p.Services[0].Exposure == "cloudflare-tunnel" {
			message = "DNS skipped: Cloudflare Tunnel public hostname is managed in Cloudflare"
		}
		if r.SkipDNS {
			message = "DNS skipped: --skip-dns"
		}
		dnsMessages = append(dnsMessages, message)
	}
	if compose {
		result["deployment"] = p.Record.Name
		result["health"] = health
		result["probe"] = probes
		result["dns"] = dnsMessages
	} else {
		result["service"] = p.Record.Name
		result["health"] = health[0]
		result["probe"] = health[0].Message
		result["dns"] = dnsMessages[0]
	}
	return result, nil
}
