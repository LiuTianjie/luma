package nomad

import (
	"encoding/json"
	"fmt"
	"net/url"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/LiuTianjie/luma/internal/deployment"
)

const tailscaleAddress = "${meta.luma_tailscale_ip}"

type Compiler struct{}

func (Compiler) Compile(s deployment.Service, config deployment.RenderConfig) (deployment.Plan, error) {
	if err := s.Validate(); err != nil {
		return deployment.Plan{}, err
	}
	job, warnings, err := compileJob(s, config)
	if err != nil {
		return deployment.Plan{}, err
	}
	content, err := json.MarshalIndent(job, "", "  ")
	if err != nil {
		return deployment.Plan{}, err
	}
	target := s.StackPath
	if target == "" {
		target = filepath.Join(config.StackRoot, s.Region, s.Slug(), s.Slug()+".nomad.json")
	}
	plan := deployment.Plan{Name: s.Name, Region: s.Region, Exposure: s.Exposure, Kind: s.Kind(), Warnings: warnings, Artifacts: []deployment.Artifact{{Kind: "job", Path: target, Content: string(content)}}}
	if route, err := compileRoute(s, config); err != nil {
		return plan, err
	} else if route != "" {
		path := s.RoutePath
		if path == "" {
			path = filepath.Join(config.RoutesRoot, s.Slug()+".yml")
		}
		plan.Artifacts = append(plan.Artifacts, deployment.Artifact{Kind: "route", Path: path, Content: route})
	}
	return plan, nil
}

func edge(s deployment.Service) bool { return s.Exposure == "cn-edge" || s.Exposure == "external-edge" }
func hostPort(s deployment.Service) bool {
	return s.Exposure == "tailscale-relay" || s.Exposure == "tcp-relay"
}

func compileJob(s deployment.Service, config deployment.RenderConfig) (JobEnvelope, []string, error) {
	name := s.Slug()
	job := Job{ID: name, Name: name, Type: "service", Datacenters: []string{"dc1"}, Meta: map[string]string{"luma.managed": "true", "luma.region": s.Region}, Constraints: []Constraint{{LTarget: "${meta.region}", RTarget: s.Region, Operand: "="}}}
	if s.NodeID != "" {
		job.Constraints = append(job.Constraints, Constraint{LTarget: "${node.unique.id}", RTarget: s.NodeID, Operand: "="})
	} else if s.Node != "" {
		job.Constraints = append(job.Constraints, Constraint{LTarget: "${meta.luma_node_name}", RTarget: s.Node, Operand: "="})
	}
	for _, raw := range s.Constraints {
		c, err := constraint(raw)
		if err != nil {
			return JobEnvelope{}, nil, err
		}
		job.Constraints = append(job.Constraints, c)
	}
	group := Group{Name: name, Count: s.Replicas, MaxClientDisconnect: 3600e9}
	label := ""
	if !(hostPort(s) && s.PublishPort == 0) && s.Port > 0 {
		label = "http"
		if s.Exposure == "tcp-relay" {
			label = "tcp"
		}
		network := Network{Mode: "bridge"}
		if s.PublishPort > 0 {
			network.ReservedPorts = []Port{{Label: label, Value: s.PublishPort, To: s.Port, HostNetwork: "default"}}
		} else {
			network.DynamicPorts = []Port{{Label: label, To: s.Port}}
			if !edge(s) {
				network.Mode = "host"
			}
		}
		group.Networks = []Network{network}
	}
	if s.Exposure == "cloudflare-tunnel" && s.PublishPort == 0 {
		// cloudflared must reach the origin through localhost in the allocation.
		// A host-mode group leaves Docker tasks in separate bridge namespaces.
		// Tunnel-only traffic needs no published host port.
		group.Networks = []Network{{Mode: "bridge"}}
		label = ""
	}
	if edge(s) {
		service, err := discovery(s, config, label)
		if err != nil {
			return JobEnvelope{}, nil, err
		}
		group.Services = []DiscoveryService{service}
		if service.Address == tailscaleAddress {
			job.Constraints = append(job.Constraints, Constraint{LTarget: tailscaleAddress, Operand: "is_set"})
		}
	}
	docker := DockerConfig{Image: s.Image}
	if hostPort(s) && label == "" {
		docker.NetworkMode = "host"
	} else if label != "" {
		docker.Ports = []string{label}
	}
	if s.Command != nil {
		docker.Args = &s.Command
	}
	for _, raw := range s.Volumes {
		parts := strings.Split(raw, ":")
		if len(parts) < 2 || parts[0] == "" || parts[1] == "" {
			return JobEnvelope{}, nil, fmt.Errorf("invalid volume spec (expected source:target): %q", raw)
		}
		kind := "volume"
		if strings.ContainsAny(parts[0][:1], "/.~") {
			kind = "bind"
		}
		docker.Mount = append(docker.Mount, Mount{Type: kind, Source: parts[0], Target: parts[1], Readonly: len(parts) > 2 && parts[2] == "ro"})
	}
	res, warnings, err := resources(s.Resources)
	if err != nil {
		return JobEnvelope{}, nil, err
	}
	group.Tasks = []Task{{Name: name, Driver: "docker", Config: docker, Resources: res, Env: deployment.RuntimeEnvironment(s, config.EgressProxyURL)}}
	if s.Exposure == "cloudflare-tunnel" {
		args := []string{"tunnel", "--no-autoupdate", "run"}
		group.Tasks = append(group.Tasks, Task{Name: "cloudflared", Driver: "docker", Config: DockerConfig{Image: "cloudflare/cloudflared:latest", Args: &args}, Env: map[string]string{"TUNNEL_TOKEN": "${" + s.TunnelTokenEnv + "}"}, Resources: Resources{CPU: 100, MemoryMB: 128}})
	}
	job.Update = Update{AutoRevert: true, MaxParallel: 1, MinHealthyTime: 5e9, HealthyDeadline: 1800e9, ProgressDeadline: 2400e9, HealthCheck: "task_states"}
	if len(group.Services) > 0 && len(group.Services[0].Checks) > 0 {
		job.Update.HealthCheck = "checks"
	}
	if len(s.Volumes) == 0 && s.Replicas == 1 && edge(s) && s.PublishPort == 0 {
		job.Update.Canary = 1
		job.Update.AutoPromote = true
	}
	job.TaskGroups = []Group{group}
	return JobEnvelope{Job: job}, warnings, nil
}

func constraint(raw string) (Constraint, error) {
	for _, op := range []string{"==", "!="} {
		left, right, found := strings.Cut(strings.TrimSpace(raw), op)
		if !found {
			continue
		}
		left, right = strings.TrimSpace(left), strings.TrimSpace(right)
		if left == "" || right == "" {
			return Constraint{}, fmt.Errorf("invalid constraint: %q", raw)
		}
		if strings.HasPrefix(left, "node.labels.") {
			left = "${meta." + strings.TrimPrefix(left, "node.labels.") + "}"
		}
		operand := op
		if op == "==" {
			operand = "="
		}
		return Constraint{LTarget: left, RTarget: right, Operand: operand}, nil
	}
	return Constraint{}, fmt.Errorf("unsupported constraint syntax (expected '==' or '!='): %q", raw)
}

func discovery(s deployment.Service, config deployment.RenderConfig, label string) (DiscoveryService, error) {
	name := s.Slug()
	router := "traefik.http.routers." + name
	service := DiscoveryService{Name: name, PortLabel: label, Provider: "nomad", Tags: []string{"traefik.enable=true", router + ".rule=Host(`" + s.Domain + "`)", router + ".entrypoints=" + config.Entrypoint, router + ".tls.certresolver=" + config.CertResolver}}
	host := strings.Trim(strings.ToLower(strings.TrimSpace(s.Domain)), ".")
	for _, raw := range config.ACMEDomains {
		domain := strings.Trim(strings.ToLower(strings.TrimSpace(raw)), ".")
		if domain != "" && (host == domain || strings.HasSuffix(host, "."+domain)) {
			service.Tags = append(service.Tags, router+".tls.domains[0].main=*."+domain, router+".tls.domains[0].sans="+domain)
			break
		}
	}
	service.Tags = append(service.Tags, s.Labels...)
	node := config.Nodes[s.Node]
	if node.LocalIngress {
		service.AddressMode = "host"
	} else {
		service.Address = node.Address
		if service.Address == "" {
			service.Address = tailscaleAddress
		}
	}
	if s.Healthcheck.Present {
		check, err := healthcheck(s, label)
		if err != nil {
			return service, err
		}
		service.Checks = []Check{check}
	}
	return service, nil
}

var healthURL = regexp.MustCompile("https?://[^'\"\\s)]+")

func healthcheck(s deployment.Service, label string) (Check, error) {
	h := s.Healthcheck
	interval, err := duration(h.Interval)
	if err != nil {
		return Check{}, err
	}
	timeout, err := duration(h.Timeout)
	if err != nil {
		return Check{}, err
	}
	check := Check{Type: "tcp", Name: s.Slug() + "-alive", PortLabel: label, Interval: interval, Timeout: timeout}
	if len(h.Test) > 0 && (h.Test[0] == "CMD" || h.Test[0] == "CMD-SHELL") {
		if target := healthURL.FindString(strings.Join(h.Test[1:], " ")); target != "" {
			parsed, err := url.Parse(target)
			if err != nil {
				return check, err
			}
			check.Type = "http"
			check.Name = s.Slug() + "-health"
			check.Path = parsed.EscapedPath()
			if check.Path == "" {
				check.Path = "/"
			}
		}
	}
	return check, nil
}
