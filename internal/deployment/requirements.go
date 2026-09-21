package deployment

import (
	"context"
	"fmt"
	"slices"
	"strings"

	"github.com/LiuTianjie/luma/internal/registry"
)

type PrerequisiteState struct {
	Secrets    map[string]bool
	Registries map[string]bool
	Verified   map[string]bool
}
type PrerequisiteReader interface {
	DeploymentPrerequisites(context.Context, []string) (PrerequisiteState, error)
}
type RequirementCheck struct {
	Kind     string   `json:"kind"`
	Required bool     `json:"required"`
	Status   string   `json:"status"`
	Detail   string   `json:"detail"`
	Missing  []string `json:"missing"`
	Explicit bool     `json:"explicit"`
}
type RequirementReport struct {
	Ready           bool               `json:"ready"`
	Checks          []RequirementCheck `json:"checks"`
	RequiredSecrets []string           `json:"requiredSecrets"`
	Init            []string           `json:"init"`
	Notes           []string           `json:"notes"`
}

func requirementsFor(s Service, snapshot Snapshot, state PrerequisiteState, dns DNSConfig, present map[string]bool, verified bool) RequirementReport {
	r := RequirementReport{Ready: true, Checks: []RequirementCheck{}, RequiredSecrets: RequiredSecretNames([]Service{s}), Init: append([]string{}, s.Requirements.Init...), Notes: append([]string{}, s.Requirements.Notes...)}
	explicit := func(kind string) bool { return slices.Contains(s.Requirements.Capabilities, kind) }
	add := func(kind string, required, configured, declared bool, detail string, missing ...string) {
		status := "ready"
		if !configured {
			status = "skipped"
			if required {
				status = "missing"
				r.Ready = false
			}
		}
		if configured || !required {
			missing = []string{}
		}
		if missing == nil {
			missing = []string{}
		}
		r.Checks = append(r.Checks, RequirementCheck{Kind: kind, Required: required, Status: status, Detail: detail, Missing: missing, Explicit: declared})
	}
	count, tailnet := 0, false
	for _, n := range snapshot.Nodes {
		if n.Region == s.Region && n.Ready {
			count++
			tailnet = tailnet || snapshot.NodeAddresses[n.Name].Tailnet
		}
	}
	add("region-capacity", true, count > 0, false, fmt.Sprintf("%d ready node(s) in region %s", count, s.Region), "node in region "+s.Region)
	token := dns.TokenEnv
	if token == "" {
		token = "CLOUDFLARE_API_TOKEN"
	}
	needsCF := slices.Contains([]string{"cn-edge", "external-edge", "cloudflare-tunnel"}, s.Exposure) || explicit("cloudflare")
	cf := dns.Provider == "cloudflare" && state.Secrets[token]
	missingCF := []string{"providers.dns.provider=cloudflare", token}
	if cf && verified && !state.Verified["cloudflare"] {
		missingCF = []string{"run Dashboard setup check for Cloudflare"}
	}
	add("cloudflare", needsCF, cf && (!verified || state.Verified["cloudflare"]), explicit("cloudflare"), "Cloudflare DNS provider and token available", missingCF...)
	add("tailscale", s.Exposure == "tailscale-relay" || s.Region == "home" || explicit("tailscale"), tailnet, explicit("tailscale"), "A node in the target region has a Tailscale address", "Tailscale-connected node")
	egress := state.Secrets["EGRESS_SUBSCRIPTION_URL"]
	missingEgress := "EGRESS_SUBSCRIPTION_URL"
	if egress && verified && !state.Verified["egress"] {
		missingEgress = "run Dashboard setup check for Egress"
	}
	add("egress", s.Proxy || explicit("egress"), egress && (!verified || state.Verified["egress"]), explicit("egress"), "Egress subscription is available", missingEgress)
	missingSecrets := []string{}
	for _, name := range r.RequiredSecrets {
		if !present[name] {
			missingSecrets = append(missingSecrets, name)
		}
	}
	add("secrets", len(r.RequiredSecrets) > 0, len(missingSecrets) == 0, len(s.Requirements.Secrets) > 0, "Referenced service secrets are present", missingSecrets...)
	image, _ := registry.ParseImage(s.Image)
	host := image.Registry
	private := !slices.Contains([]string{"docker.io", "registry-1.docker.io", "ghcr.io", "quay.io", "gcr.io"}, host)
	configured := state.Registries[host]
	if host == "docker.io" || host == "registry-1.docker.io" {
		configured = configured || state.Registries["docker.io"] || state.Registries["registry-1.docker.io"]
	}
	needed := (private || explicit("registry")) && !configured
	add("registry", needed, !needed, explicit("registry"), "Image registry "+host+" is available", "registry credential for "+host)
	return r
}

func (r RequirementReport) Require(component string, explicitOnly bool) error {
	checks := map[string]RequirementCheck{}
	for _, check := range r.Checks {
		checks[check.Kind] = check
		if check.Required && check.Status != "ready" && (!explicitOnly || check.Explicit) {
			return inputError("deployment prerequisites are not ready for " + component + ": " + check.Kind + ": " + strings.Join(check.Missing, ", "))
		}
	}
	for _, action := range r.Init {
		kind, ok := map[string]string{"cloudflare-dns": "cloudflare", "cloudflare-tunnel": "cloudflare", "tailscale-node": "tailscale", "egress": "egress", "registry": "registry", "secrets": "secrets"}[action]
		if !ok {
			return inputError("unsupported initialization action: " + action)
		}
		check := checks[kind]
		if check.Status == "missing" || check.Status == "error" {
			return inputError("initialization " + action + " is not ready for " + component + ": " + strings.Join(check.Missing, ", "))
		}
	}
	return nil
}

func (c *Coordinator) RequirementReports(ctx context.Context, r ControlRequest, p Prepared, resolved map[string]string) (map[string]RequirementReport, error) {
	state := PrerequisiteState{Secrets: map[string]bool{}, Registries: map[string]bool{}, Verified: map[string]bool{}}
	token := c.DNSConfig.TokenEnv
	if token == "" {
		token = "CLOUDFLARE_API_TOKEN"
	}
	if c.Prerequisites != nil {
		var err error
		state, err = c.Prerequisites.DeploymentPrerequisites(ctx, []string{token, "EGRESS_SUBSCRIPTION_URL"})
		if err != nil {
			return nil, err
		}
	}
	if state.Secrets == nil {
		state.Secrets = map[string]bool{}
	}
	present := map[string]bool{}
	if resolved != nil {
		for name, value := range resolved {
			present[name] = true
			state.Secrets[name] = value != ""
		}
	} else {
		names, _ := deploymentSecretNames(r, p.Services)
		if c.Secrets != nil && len(names) > 0 {
			secrets, err := c.Secrets.ReadDeploymentSecrets(ctx, p.Record.Slug, names)
			if err != nil {
				return nil, err
			}
			values := secrets.Global
			if r.EnvSecrets != nil || secrets.HasScoped {
				values = secrets.Scoped
			}
			for _, name := range names {
				if value, ok := values[name]; ok {
					present[name] = true
					state.Secrets[name] = value != ""
				}
				if value, ok := r.EnvSecrets[name]; ok {
					present[name] = true
					state.Secrets[name] = value != ""
				}
			}
			clear(secrets.Global)
			clear(secrets.Scoped)
		}
	}
	reports := map[string]RequirementReport{}
	for name, s := range p.ImageServices {
		reports[name] = requirementsFor(s, p.Snapshot, state, c.DNSConfig, present, r.Origin == "dashboard")
	}
	return reports, nil
}
func (p Prepared) PublicRequirements(reports map[string]RequirementReport) any {
	if p.Record.Kind == "service" {
		return reports[p.Record.Slug]
	}
	return reports
}
