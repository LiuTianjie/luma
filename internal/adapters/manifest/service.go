package manifest

import (
	"github.com/LiuTianjie/luma/internal/deployment"
	"github.com/LiuTianjie/luma/internal/region"
	"strings"
)

func DecodeService(data []byte) (deployment.Service, error) {
	raw, err := mapping(data)
	if err != nil {
		return deployment.Service{}, err
	}
	return decodeService(raw)
}

func decodeService(raw map[string]any) (deployment.Service, error) {
	var err error
	r := &reader{}
	r.fields(raw, "service manifest", "build command constraints dns domain engine env environment exposure healthcheck image labels name networks node port proxy public publishPort region relay replicas resources routePath requirements stackPath storage tcp tunnel volumes")
	s := deployment.Service{
		Name: r.text(raw["name"], "name", ""), Image: r.text(raw["image"], "image", ""), Region: r.text(raw["region"], "region", ""),
		Node: r.text(raw["node"], "node", ""), Exposure: r.text(raw["exposure"], "exposure", "none"), Domain: r.text(raw["domain"], "domain", ""), Engine: r.text(raw["engine"], "engine", ""),
		Port: r.integer(raw["port"], "port", 0), PublishPort: r.integer(raw["publishPort"], "publishPort", 0), Replicas: r.integer(raw["replicas"], "replicas", 1),
		Environment: map[string]string{}, Constraints: r.list(raw["constraints"], "constraints"), Labels: r.list(raw["labels"], "labels"), Networks: r.list(raw["networks"], "networks"), Volumes: r.list(raw["volumes"], "volumes"),
		StackPath: r.text(raw["stackPath"], "stackPath", ""), RoutePath: r.text(raw["routePath"], "routePath", ""), Proxy: r.boolean(raw["proxy"], "proxy"),
	}
	if _, exists := raw["node"]; exists && s.Node == "" {
		r.fail("node must be a non-empty string when provided")
	}
	if _, exists := raw["public"]; exists && raw["exposure"] == nil {
		r.fail("public is no longer supported; use exposure")
	}
	s.Region, err = region.Parse(s.Region)
	if err != nil {
		return s, err
	}
	if raw["build"] != nil {
		b := r.object(raw["build"], "build")
		r.fields(b, "build", "context dockerfile platform repo")
		s.Build = &deployment.Build{Context: r.text(b["context"], "build.context", "."), Dockerfile: r.text(b["dockerfile"], "build.dockerfile", "Dockerfile"), Platform: r.text(b["platform"], "build.platform", "linux/amd64"), Repo: r.text(b["repo"], "build.repo", "")}
	}
	switch command := raw["command"].(type) {
	case nil:
	case string:
		s.Command = []string{"sh", "-c", command}
	case []any:
		s.Command = []string{}
		for _, item := range command {
			value, err := scalar(item)
			if err != nil {
				r.fail("command arguments must be scalar")
			}
			s.Command = append(s.Command, value)
		}
	default:
		r.fail("command must be a string or list")
	}
	env := r.object(raw["env"], "env")
	if len(env) == 0 {
		env = r.object(raw["environment"], "environment")
	}
	for key, value := range env {
		text, err := scalar(value)
		if err != nil {
			r.fail("env.%s must be scalar", key)
		}
		s.Environment[key] = text
	}
	resources := r.object(raw["resources"], "resources")
	r.fields(resources, "resources", "limits reservations")
	for name, target := range map[string]*deployment.ResourceValues{"limits": &s.Resources.Limits, "reservations": &s.Resources.Reservations} {
		section := r.object(resources[name], "resources."+name)
		for key, dest := range map[string]**string{"cpus": &target.CPUs, "memory": &target.Memory} {
			if value := section[key]; value != nil {
				if _, ok := value.(bool); ok {
					r.fail("resources.%s.%s must be a string or number", name, key)
				}
				text, err := scalar(value)
				if err != nil {
					r.fail("resources.%s.%s must be a string or number", name, key)
				}
				*dest = &text
			}
		}
	}
	hc := r.object(raw["healthcheck"], "healthcheck")
	s.Healthcheck = deployment.Healthcheck{Present: len(hc) > 0, Interval: "30s", Timeout: "5s"}
	if test, ok := hc["test"].([]any); ok {
		for _, value := range test {
			text, err := scalar(value)
			if err != nil {
				r.fail("healthcheck.test must contain scalar values")
			}
			s.Healthcheck.Test = append(s.Healthcheck.Test, text)
		}
	}
	for key, dest := range map[string]*string{"interval": &s.Healthcheck.Interval, "timeout": &s.Healthcheck.Timeout} {
		if value := hc[key]; value != nil {
			text, err := scalar(value)
			if err != nil {
				r.fail("healthcheck.%s must be scalar", key)
			}
			*dest = text
		}
	}
	if len(r.object(raw["storage"], "storage")) > 0 {
		r.fail("NFS storage metadata has been removed; migrate data to the deployment node and use local or named volumes")
	}
	relay := r.object(raw["relay"], "relay")
	s.Relay = deployment.Relay{URL: r.text(relay["url"], "relay.url", ""), URLs: r.list(relay["urls"], "relay.urls"), Scheme: r.text(relay["scheme"], "relay.scheme", "http"), Host: r.text(relay["host"], "relay.host", ""), Port: r.integer(relay["port"], "relay.port", 0), Priority: r.integer(relay["priority"], "relay.priority", 1000)}
	tcp := r.object(raw["tcp"], "tcp")
	s.TCP = deployment.TCPRelay{Address: r.text(tcp["address"], "tcp.address", ""), Addresses: r.list(tcp["addresses"], "tcp.addresses"), Host: r.text(tcp["host"], "tcp.host", ""), Port: r.integer(tcp["port"], "tcp.port", 0)}
	tunnel := r.object(raw["tunnel"], "tunnel")
	s.TunnelTokenEnv = r.text(tunnel["tokenEnv"], "tunnel.tokenEnv", "CLOUDFLARE_TUNNEL_TOKEN")
	dns := r.object(raw["dns"], "dns")
	s.DNS.Target = r.text(dns["target"], "dns.target", "")
	s.DNS.Type = r.text(dns["type"], "dns.type", "")
	if dns["ttl"] != nil {
		value := r.integer(dns["ttl"], "dns.ttl", 1)
		s.DNS.TTL = &value
	}
	if dns["proxied"] != nil {
		value := r.boolean(dns["proxied"], "dns.proxied")
		s.DNS.Proxied = &value
	}
	requirements := r.object(raw["requirements"], "requirements")
	r.fields(requirements, "requirements", "capabilities secrets init notes")
	for key, dest := range map[string]*[]string{"capabilities": &s.Requirements.Capabilities, "secrets": &s.Requirements.Secrets, "init": &s.Requirements.Init, "notes": &s.Requirements.Notes} {
		*dest = r.list(requirements[key], "requirements."+key)
		for i, value := range *dest {
			(*dest)[i] = strings.TrimSpace(value)
			if (*dest)[i] == "" {
				r.fail("requirements.%s must contain non-empty strings", key)
			}
		}
	}
	if r.err != nil {
		return s, r.err
	}
	return s, s.Validate()
}
