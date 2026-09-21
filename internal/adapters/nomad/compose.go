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

var portLabel = regexp.MustCompile(`[^a-zA-Z0-9_]`)

func (Compiler) CompileCompose(c deployment.Compose, config deployment.RenderConfig) (deployment.Plan, error) {
	p, err := c.Placement()
	if err != nil {
		return deployment.Plan{}, err
	}
	name := c.Slug()
	plan := deployment.Plan{Name: c.Name, Region: p.Region, Kind: "compose", Warnings: append([]string(nil), c.Warnings...)}
	job := Job{ID: name, Name: name, Type: "service", Datacenters: []string{"dc1"}, Meta: map[string]string{"luma.managed": "true", "luma.region": p.Region, "luma.compose": "true"}, Constraints: []Constraint{{LTarget: "${meta.region}", RTarget: p.Region, Operand: "="}}, Update: Update{AutoRevert: true, MaxParallel: 1, MinHealthyTime: 6e9, HealthyDeadline: 1800e9, ProgressDeadline: 2400e9, HealthCheck: "task_states"}}
	if p.NodeID != "" {
		job.Constraints = append(job.Constraints, Constraint{LTarget: "${node.unique.id}", RTarget: p.NodeID, Operand: "="})
	} else if p.Node != "" {
		job.Constraints = append(job.Constraints, Constraint{LTarget: "${meta.luma_node_name}", RTarget: p.Node, Operand: "="})
	}
	extraHosts := []string{}
	for _, s := range c.Services {
		extraHosts = append(extraHosts, s.Name+":127.0.0.1")
	}
	group := Group{Name: name, Count: 1, MaxClientDisconnect: 3600e9}
	network := Network{Mode: "bridge"}
	dynamicEdge, canary := false, true
	labels := map[string]bool{}
	for _, s := range c.Services {
		label := portLabel.ReplaceAllString(s.Name, "_")
		if labels[label] {
			return plan, fmt.Errorf("compose port labels collide: %s", s.Name)
		}
		labels[label] = true
		docker := DockerConfig{Image: s.Image, ExtraHosts: extraHosts}
		if s.Command != nil {
			docker.Args = &s.Command
		}
		if p.HostNetwork {
			docker.NetworkMode = "host"
		} else if s.Port > 0 && (edge(s.Service) || hostPort(s.Service) || s.PublishPort > 0) {
			docker.Ports = []string{label}
			if edge(s.Service) && s.PublishPort == 0 {
				network.DynamicPorts = append(network.DynamicPorts, Port{Label: label, To: s.Port, HostNetwork: "default"})
			} else {
				port := s.PublishPort
				if port == 0 {
					port = s.Port
				}
				network.ReservedPorts = append(network.ReservedPorts, Port{Label: label, Value: port, To: s.Port, HostNetwork: "default"})
			}
		}
		for _, m := range s.Mounts {
			mount := composeMount(c, m)
			docker.Mount = append(docker.Mount, mount)
		}
		res, warnings, err := resources(s.Resources)
		if err != nil {
			return plan, err
		}
		for _, warning := range warnings {
			plan.Warnings = append(plan.Warnings, s.Name+": "+warning)
		}
		task := Task{Name: s.Name, Driver: "docker", Config: docker, Resources: res, Env: deployment.RuntimeEnvironment(s.Service, config.EgressProxyURL)}
		if s.StopGracePeriod != "" {
			ns, err := duration(s.StopGracePeriod)
			if err != nil {
				return plan, err
			}
			task.KillTimeout = &ns
		}
		group.Tasks = append(group.Tasks, task)
		var checks []Check
		if s.Healthcheck.Present && (edge(s.Service) || hostPort(s.Service)) {
			check, err := composeHealthcheck(s, name, label)
			if err != nil {
				return plan, err
			}
			checks = []Check{check}
			job.Update.HealthCheck = "checks"
		}
		if edge(s.Service) {
			service := s.Service
			service.Name = name + "-" + s.Slug()
			service.Node = p.Node
			service.Healthcheck.Present = false
			discovery, err := discovery(service, config, label)
			if err != nil {
				return plan, err
			}
			base := append([]string(nil), discovery.Tags[:4]...)
			discovery.Tags = append(append(base, "traefik.http.routers."+service.Name+".service="+service.Name), discovery.Tags[4:]...)
			discovery.Checks = checks
			group.Services = append(group.Services, discovery)
		} else if hostPort(s.Service) && len(checks) > 0 {
			group.Services = append(group.Services, DiscoveryService{Name: name + "-" + s.Slug() + "-health", PortLabel: label, Provider: "nomad", Checks: checks})
		}
		if edge(s.Service) && s.Port > 0 {
			dynamicEdge = true
		}
		if s.PublishPort > 0 || hostPort(s.Service) || s.HasVolumes {
			canary = false
		}
	}
	if !p.HostNetwork && (len(group.Tasks) > 1 || len(network.ReservedPorts) > 0 || len(network.DynamicPorts) > 0) {
		group.Networks = []Network{network}
	}
	node := config.Nodes[p.Node]
	if len(group.Services) > 0 && !node.LocalIngress && node.Address == "" {
		job.Constraints = append(job.Constraints, Constraint{LTarget: tailscaleAddress, Operand: "is_set"})
	}
	if dynamicEdge && canary {
		job.Update.Canary = 1
		job.Update.AutoPromote = true
	}
	job.TaskGroups = []Group{group}
	content, err := json.MarshalIndent(JobEnvelope{Job: job}, "", "  ")
	if err != nil {
		return plan, err
	}
	plan.Artifacts = []deployment.Artifact{{Kind: "job", Path: filepath.Join(config.StackRoot, "compose", name, name+".nomad.json"), Content: string(content)}}
	for _, s := range c.Services {
		if !hostPort(s.Service) {
			continue
		}
		routeService := s.Service
		routeService.Name = name + "-" + s.Slug()
		if routeService.Node == "" {
			routeService.Node = name + "_" + s.Name
		}
		// Preserve the existing Compose router priority while honoring explicit upstream lists.
		routeService.Relay.Priority = 0
		content, err := compileRoute(routeService, config)
		if err != nil {
			return plan, err
		}
		plan.Artifacts = append(plan.Artifacts, deployment.Artifact{Kind: "route", Path: filepath.Join(config.RoutesRoot, routeService.Name+".yml"), Content: content})
	}
	return plan, nil
}

func composeHealthcheck(s deployment.ComposeService, name, label string) (Check, error) {
	interval, err := duration(s.Healthcheck.Interval)
	if err != nil {
		return Check{}, err
	}
	timeout, err := duration(s.Healthcheck.Timeout)
	if err != nil {
		return Check{}, err
	}
	check := Check{Name: name + "-" + s.Slug() + "-health", PortLabel: label, Type: "tcp", Interval: interval, Timeout: timeout}
	if target := healthURL.FindString(strings.Join(s.Healthcheck.Test, " ")); target != "" {
		parsed, err := url.Parse(target)
		if err != nil {
			return check, err
		}
		check.Type = "http"
		check.Path = parsed.EscapedPath()
		if check.Path == "" {
			check.Path = "/"
		}
	}
	return check, nil
}
