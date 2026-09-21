// Package deployment owns service specifications and deployment use cases.
// It does not read files, inspect process environment, or depend on an orchestrator.
package deployment

import (
	"fmt"
	"regexp"
	"strings"

	"github.com/LiuTianjie/luma/internal/credentials"
	"github.com/LiuTianjie/luma/internal/region"
)

type Service struct {
	DNS                                                 DNSOptions
	NodeID                                              string // Resolved by Control from registered identity, never a manifest field.
	Name, Image, Region, Node, Exposure, Domain, Engine string
	Port, PublishPort, Replicas                         int
	Command                                             []string
	Environment                                         map[string]string
	Constraints, Labels, Networks, Volumes              []string
	Resources                                           Resources
	Healthcheck                                         Healthcheck
	Build                                               *Build
	StackPath, RoutePath                                string
	Relay                                               Relay
	TCP                                                 TCPRelay
	TunnelTokenEnv                                      string
	Proxy                                               bool
	Requirements                                        Requirements
}

type Build struct{ Context, Dockerfile, Platform, Repo string }
type Resources struct{ Limits, Reservations ResourceValues }
type ResourceValues struct{ CPUs, Memory *string }
type Healthcheck struct {
	Present           bool
	Test              []string
	Interval, Timeout string
}
type Relay struct {
	URL            string
	URLs           []string
	Scheme, Host   string
	Port, Priority int
}
type TCPRelay struct {
	Address   string
	Addresses []string
	Host      string
	Port      int
}
type Requirements struct{ Capabilities, Secrets, Init, Notes []string }

// RenderConfig is a resolved input, not a global configuration service.
type RenderConfig struct {
	StackRoot, RoutesRoot, Entrypoint, CertResolver, Engine string
	NomadServer, NomadAddr                                  string
	EgressProxyURL                                          string // Resolved by Control; never read from the developer environment.
	ACMEDomains                                             []string
	ImageMirrors                                            []string
	Nodes                                                   map[string]NodeAddress
}
type NodeAddress struct {
	Region, PublicIP string
	Roles            []string
	LocalIngress     bool
	Tailnet          bool
	Address          string
}

var slugPattern = regexp.MustCompile(`[^a-zA-Z0-9-]+`)

func (s Service) Slug() string {
	return strings.Trim(slugPattern.ReplaceAllString(strings.ToLower(strings.TrimSpace(s.Name)), "-"), "-")
}

func (s Service) Kind() string {
	switch s.Exposure {
	case "cn-edge":
		return "public-cn-service"
	case "external-edge":
		return "public-global-service"
	case "tailscale-relay":
		return "home-tailscale-relay"
	case "cloudflare-tunnel":
		return "cloudflare-tunnel-service"
	case "tcp-relay":
		return "tcp-relay-service"
	}
	switch s.Region {
	case "cn":
		return "internal-cn-service"
	case "global":
		return "global-internal-service"
	case "home":
		return "home-internal-service"
	}
	return "internal-region-service"
}

func (s Service) Validate() error {
	if strings.TrimSpace(s.Name) == "" {
		return fmt.Errorf("service manifest requires string field: name")
	}
	if s.Slug() == "" {
		return fmt.Errorf("service name must contain at least one letter or number")
	}
	if s.Image == "" && s.Build == nil {
		return fmt.Errorf("service manifest requires string field: image (or a build block)")
	}
	if s.Engine != "" && s.Engine != "nomad" {
		return fmt.Errorf("engine must be nomad")
	}
	if err := region.ValidateExposure(s.Region, s.Exposure); err != nil {
		return err
	}
	if s.Replicas < 1 {
		return fmt.Errorf("replicas must be a positive integer")
	}
	if s.Exposure != "none" && (s.Domain == "" || s.Port < 1) {
		return fmt.Errorf("public service requires domain and a positive integer port")
	}
	if s.Port < 0 || s.Port > 65535 || s.PublishPort < 0 || s.PublishPort > 65535 {
		return fmt.Errorf("ports must be between 1 and 65535")
	}
	if s.PublishPort > 0 && s.Port == 0 {
		return fmt.Errorf("publishPort requires port")
	}
	if s.Exposure == "cloudflare-tunnel" && !credentials.ValidSecretName(s.TunnelTokenEnv) {
		return fmt.Errorf("tunnel.tokenEnv must be a valid environment variable name")
	}
	if s.Exposure == "tcp-relay" {
		p := s.PublishPort
		if p == 0 {
			p = s.Port
		}
		if p == 80 || p == 443 {
			return fmt.Errorf("tcp-relay cannot use reserved Traefik port: %d", p)
		}
	}
	for _, capability := range s.Requirements.Capabilities {
		switch capability {
		case "cloudflare", "tailscale", "egress", "registry":
		default:
			return fmt.Errorf("unsupported requirements.capabilities value: %s", capability)
		}
	}
	for _, action := range s.Requirements.Init {
		switch action {
		case "cloudflare-dns", "cloudflare-tunnel", "tailscale-node", "egress", "registry", "secrets":
		default:
			return fmt.Errorf("unsupported requirements.init action: %s", action)
		}
	}
	return nil
}
