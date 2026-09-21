package deployment

import (
	"context"
	"fmt"
	"path"
	"slices"
	"strings"
)

// DNS values are configuration, never credentials. Credentials are resolved only
// by the DNS adapter when executing a planned record change.
type DNSOptions struct {
	Target, Type string
	Proxied      *bool
	TTL          *int
}
type DNSConfig struct {
	Provider, TokenEnv, ZoneIDEnv, ZoneID, EdgeTarget, RecordType string
	Proxied                                                       bool
	TTL                                                           int
}
type DNSRecord struct {
	Type    string `json:"type"`
	Name    string `json:"name"`
	Content string `json:"content"`
	TTL     int    `json:"ttl"`
	Proxied bool   `json:"proxied"`
	Comment string `json:"comment"`
}
type DNSManager interface {
	Sync(context.Context, DNSConfig, []DNSRecord) ([]string, error)
}
type DeploymentFiles interface {
	Validate(Plan, []string) error
	WriteJobs(context.Context, Plan) ([]string, error)
	ReconcileRoutes(context.Context, Plan, []string) ([]string, error)
}
type RouteHealth struct {
	Status    string `json:"status"`
	Kind      string `json:"kind"`
	Target    string `json:"target"`
	Message   string `json:"message"`
	CheckedAt int64  `json:"checkedAt"`
}
type RouteProber interface {
	Probe(context.Context, Service) (RouteHealth, error)
}

func PlanDNS(services []Service, c DNSConfig, nodes map[string]NodeAddress, skip bool) ([]DNSRecord, error) {
	records := []DNSRecord{}
	if skip || c.Provider != "cloudflare" {
		return records, nil
	}
	keys := make([]string, 0, len(nodes))
	for name := range nodes {
		keys = append(keys, name)
	}
	slices.Sort(keys)
	find := func(region string, edge bool) string {
		for _, key := range keys {
			n := nodes[key]
			if (region == "" || n.Region == region) && (!edge || slices.Contains(n.Roles, "edge")) {
				return n.PublicIP
			}
		}
		return ""
	}
	seen := map[string]DNSRecord{}
	for _, s := range services {
		if s.Exposure == "none" || s.Exposure == "cloudflare-tunnel" {
			continue
		}
		target := s.DNS.Target
		if target == "" {
			target = c.EdgeTarget
		}
		if target == "" {
			if s.Exposure == "external-edge" {
				target = find(s.Region, true)
				if target == "" {
					target = find(s.Region, false)
				}
			} else {
				target = find("cn", true)
				if target == "" {
					target = find("", true)
				}
			}
		}
		if target == "" {
			return nil, inputError("missing DNS target for " + s.Name)
		}
		kind := s.DNS.Type
		if kind == "" {
			kind = c.RecordType
		}
		if kind == "" {
			kind = "A"
		}
		ttl := c.TTL
		if ttl == 0 {
			ttl = 1
		}
		if s.DNS.TTL != nil {
			ttl = *s.DNS.TTL
		}
		proxied := c.Proxied
		if s.DNS.Proxied != nil {
			proxied = *s.DNS.Proxied
		}
		record := DNSRecord{Type: strings.ToUpper(kind), Name: s.Domain, Content: target, TTL: ttl, Proxied: proxied, Comment: "managed by Luma for " + s.Name}
		if record.Name == "" || ttl < 1 {
			return nil, inputError("DNS record requires a hostname and positive TTL")
		}
		key := record.Type + "\x00" + record.Name
		if old, ok := seen[key]; ok {
			if old.Content != record.Content || old.TTL != record.TTL || old.Proxied != record.Proxied {
				return nil, inputError("conflicting DNS records for " + s.Domain)
			}
			continue
		}
		seen[key] = record
		records = append(records, record)
	}
	return records, nil
}

func ShadowRoute(s Service, c RenderConfig) string {
	if s.Exposure != "cn-edge" && s.Exposure != "external-edge" {
		return ""
	}
	if s.RoutePath != "" {
		return s.RoutePath
	}
	return path.Join(c.RoutesRoot, s.Slug()+".yml")
}
func publicIntegrationError() error {
	return fmt.Errorf("%w: public deployments require Control config, DNS, route files and probe adapters", ErrNotMigrated)
}
