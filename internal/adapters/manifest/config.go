package manifest

import (
	"github.com/LiuTianjie/luma/internal/deployment"
	"net/url"
	"strconv"
	"strings"
)

func DefaultConfig() deployment.RenderConfig {
	return deployment.RenderConfig{StackRoot: "stacks", RoutesRoot: "routes", Entrypoint: "websecure", CertResolver: "letsencrypt", Nodes: map[string]deployment.NodeAddress{}, ImageMirrors: []string{"docker.1panel.live", "docker.1ms.run", "docker.m.daocloud.io"}}
}

func DecodeConfig(data []byte) (deployment.RenderConfig, error) {
	c := DefaultConfig()
	raw, err := mapping(data)
	if err != nil {
		return c, err
	}
	r := &reader{}
	d := r.object(raw["defaults"], "defaults")
	c.StackRoot = r.text(d["stackRoot"], "defaults.stackRoot", c.StackRoot)
	c.RoutesRoot = r.text(d["routesRoot"], "defaults.routesRoot", c.RoutesRoot)
	c.Entrypoint = r.text(d["entrypoint"], "defaults.entrypoint", c.Entrypoint)
	c.CertResolver = r.text(d["certResolver"], "defaults.certResolver", c.CertResolver)
	c.Engine = r.text(d["engine"], "defaults.engine", "")
	c.NomadServer = r.text(d["nomadServer"], "defaults.nomadServer", "")
	c.NomadAddr = r.text(d["nomadAddr"], "defaults.nomadAddr", "")
	if d["imageMirrors"] != nil {
		c.ImageMirrors = r.list(d["imageMirrors"], "defaults.imageMirrors")
	}
	providers := r.object(raw["providers"], "providers")
	dns := r.object(providers["dns"], "providers.dns")
	if len(dns) == 0 {
		dns = r.object(raw["dns"], "dns")
	}
	zone := strings.Trim(r.text(dns["zone"], "dns.zone", ""), ".")
	provider := r.text(dns["provider"], "dns.provider", r.text(dns["type"], "dns.type", "cloudflare"))
	acme := r.text(d["acmeDnsProvider"], "acmeDnsProvider", "")
	if _, exists := d["acmeDnsProvider"]; !exists && provider == "cloudflare" && zone != "" && zone != "example.com" {
		acme = "cloudflare"
	}
	if d["acmeDomains"] != nil {
		c.ACMEDomains = r.list(d["acmeDomains"], "acmeDomains")
	} else if acme != "" && zone != "" && zone != "example.com" {
		c.ACMEDomains = []string{zone}
	}
	for name, value := range r.object(raw["nodes"], "nodes") {
		n, ok := value.(map[string]any)
		if !ok {
			continue
		}
		address := deployment.NodeAddress{LocalIngress: r.boolean(n["lumaLocalIngress"], "lumaLocalIngress") || r.boolean(n["localIngress"], "localIngress")}
		for _, key := range []string{"tailscaleIP", "tailscaleIp", "tailscaleName", "advertiseAddr", "publicIp", "public_ip"} {
			if text, ok := n[key].(string); ok && strings.TrimSpace(text) != "" {
				address.Address = cleanAddress(text)
				break
			}
		}
		address.Region = r.text(n["region"], "region", "cn")
		address.Roles = r.list(n["roles"], "roles")
		address.PublicIP = r.text(n["publicIp"], "publicIp", r.text(n["public_ip"], "public_ip", ""))
		c.Nodes[name] = address
	}
	return c, r.err
}

func cleanAddress(value string) string {
	text := strings.TrimSpace(value)
	if strings.Contains(text, "://") {
		if parsed, err := url.Parse(text); err == nil {
			text = parsed.Host
			if text == "" {
				text = parsed.Path
			}
		}
	}
	if strings.HasPrefix(text, "[") {
		if end := strings.Index(text, "]"); end >= 0 {
			return text[1:end]
		}
	}
	if strings.Count(text, ":") == 1 {
		parts := strings.SplitN(text, ":", 2)
		if _, err := strconv.Atoi(parts[1]); err == nil {
			return parts[0]
		}
	}
	return text
}

// DecodeDNSConfig reads the same providers.dns/legacy dns keys as Python Control.
func DecodeDNSConfig(data []byte) (deployment.DNSConfig, error) {
	raw, err := mapping(data)
	if err != nil {
		return deployment.DNSConfig{}, err
	}
	r := &reader{}
	providers := r.object(raw["providers"], "providers")
	dns := r.object(providers["dns"], "providers.dns")
	if len(dns) == 0 {
		dns = r.object(raw["dns"], "dns")
	}
	provider := ""
	if len(dns) > 0 {
		provider = "cloudflare"
	}
	c := deployment.DNSConfig{
		Provider:  r.text(dns["provider"], "dns.provider", r.text(dns["type"], "dns.type", provider)),
		TokenEnv:  r.text(dns["apiTokenEnv"], "dns.apiTokenEnv", "CLOUDFLARE_API_TOKEN"),
		ZoneIDEnv: r.text(dns["zoneIdEnv"], "dns.zoneIdEnv", "CLOUDFLARE_ZONE_ID"),
		ZoneID:    r.text(dns["zoneId"], "dns.zoneId", ""), EdgeTarget: r.text(dns["edgeTarget"], "dns.edgeTarget", ""),
		RecordType: r.text(dns["recordType"], "dns.recordType", "A"), Proxied: r.boolean(dns["proxied"], "dns.proxied"), TTL: r.integer(dns["ttl"], "dns.ttl", 1),
	}
	return c, r.err
}
