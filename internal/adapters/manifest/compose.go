package manifest

import (
	"fmt"
	"slices"
	"strings"

	"github.com/LiuTianjie/luma/internal/deployment"
	"github.com/LiuTianjie/luma/internal/region"
	"github.com/LiuTianjie/luma/internal/storage"
	"go.yaml.in/yaml/v3"
)

func ComposePath(sidecar []byte) (string, error) {
	raw, err := mapping(sidecar)
	if err != nil {
		return "", err
	}
	r := &reader{}
	path := r.text(raw["compose"], "compose", "docker-compose.yml")
	if path == "" {
		r.fail("compose path must not be empty")
	}
	return path, r.err
}

// Deployment volumes are node-local. Legacy NFS declarations fail before any
// deployment submission, rather than silently starting with an empty local disk.
func DecodeCompose(sidecar, compose []byte) (deployment.Compose, error) {
	return decodeCompose(sidecar, compose, false)
}
func decodeCompose(sidecar, compose []byte, control bool) (deployment.Compose, error) {
	c := deployment.Compose{Volumes: map[string]storage.Volume{}}
	raw, err := mapping(sidecar)
	if err != nil {
		return c, err
	}
	body, err := mapping(compose)
	if err != nil {
		return c, err
	}
	r := &reader{}
	r.fields(raw, "compose sidecar", "name compose region storageClasses volumes services")
	c.Name = r.text(raw["name"], "name", "")
	c.Region, err = region.Parse(r.text(raw["region"], "region", "cn"))
	if err != nil {
		return c, err
	}
	c.ComposePath = r.text(raw["compose"], "compose", "docker-compose.yml")
	if len(r.object(raw["storageClasses"], "storageClasses")) > 0 {
		return c, fmt.Errorf("NFS storageClasses have been removed; migrate data to the deployment node and use local or named volumes")
	}
	for name, value := range r.object(raw["volumes"], "volumes") {
		v := r.object(value, "volumes."+name)
		r.fields(v, "volumes."+name, "storageClass path accessMode local initialize adopted")
		local := r.object(v["local"], "volumes.local")
		r.fields(local, "volumes.local", "node path")
		if len(local) > 0 && local["path"] == nil {
			r.fail("volumes.%s.local requires path", name)
		}
		if v["storageClass"] != nil || v["path"] != nil {
			return c, fmt.Errorf("volume %s uses removed NFS storageClass/path fields; migrate data before switching to local.path or a named volume", name)
		}
		c.Volumes[name] = storage.Volume{Name: name, AccessMode: r.text(v["accessMode"], "accessMode", "ReadWriteOnce"), LocalNode: r.text(local["node"], "local.node", ""), LocalPath: r.text(local["path"], "local.path", ""), Initialize: r.text(v["initialize"], "initialize", ""), Adopted: r.boolean(v["adopted"], "adopted")}
	}
	services := r.object(body["services"], "compose.services")
	overrides := r.object(raw["services"], "sidecar.services")
	for name := range overrides {
		if _, ok := services[name]; !ok {
			r.fail("sidecar references unknown compose service: %s", name)
		}
	}
	for _, name := range serviceOrder(compose, services) {
		body := r.object(services[name], "services."+name)
		override := r.object(overrides[name], "sidecar.services."+name)
		r.fields(override, "sidecar.services."+name, "region node exposure domain port publishPort replicas proxy relay tunnel tcp requirements")
		s, err := decodeComposeService(r, name, c.Region, body, override)
		if err != nil {
			return c, fmt.Errorf("compose service %s: %w", name, err)
		}
		c.Services = append(c.Services, s)
		for _, key := range []string{"entrypoint", "ports", "env_file", "depends_on", "user", "working_dir", "privileged", "devices", "secrets", "configs"} {
			if body[key] != nil {
				c.Warnings = append(c.Warnings, fmt.Sprintf("compose service %s: %s is not translated by the Luma renderer", name, key))
			}
		}
		if s.Replicas != 1 {
			c.Warnings = append(c.Warnings, "Compose runs one shared group; per-service replicas are not applied")
		}
	}
	declared := r.object(body["volumes"], "compose.volumes")
	for name, value := range declared {
		declaration := r.object(value, "compose.volumes."+name)
		driver := r.text(declaration["driver"], "volume.driver", "local")
		if driver != "local" || declaration["driver_opts"] != nil {
			return c, fmt.Errorf("volume %s uses a removed remote/custom volume driver; use a named local volume or local.path on the deployment node", name)
		}
	}
	for _, s := range c.Services {
		for _, mount := range s.Mounts {
			if mount.Kind == "volume" {
				declared[mount.Source] = nil
			}
		}
	}
	for name := range c.Volumes {
		if _, ok := declared[name]; !ok && len(declared) > 0 {
			c.Warnings = append(c.Warnings, fmt.Sprintf("volume %s is declared in Luma sidecar but not in compose top-level volumes", name))
		}
	}
	slices.Sort(c.Warnings)
	if r.err != nil {
		return c, r.err
	}
	if control {
		_, err = c.PlacementForControl()
	} else {
		_, err = c.Placement()
	}
	return c, err
}

func decodeComposeService(r *reader, name, defaultRegion string, body, override map[string]any) (deployment.ComposeService, error) {
	input := map[string]any{"name": name, "region": defaultRegion}
	for _, key := range []string{"image", "command", "healthcheck"} {
		input[key] = body[key]
	}
	for key, value := range override {
		input[key] = value
	}
	input["resources"] = r.object(body["deploy"], "deploy")["resources"]
	env := map[string]any{}
	if list, ok := body["environment"].([]any); ok {
		for _, value := range list {
			text, err := scalar(value)
			if err != nil {
				r.fail("environment entries must be scalar")
			}
			key, value, _ := strings.Cut(text, "=")
			env[key] = value
		}
	} else {
		env = r.object(body["environment"], "environment")
	}
	input["env"] = env
	s, err := decodeService(input)
	if err != nil {
		return deployment.ComposeService{}, err
	}
	hc := r.object(body["healthcheck"], "healthcheck")
	s.Healthcheck.Present = len(s.Healthcheck.Test) > 0 && !r.boolean(hc["disable"], "healthcheck.disable") && !strings.EqualFold(s.Healthcheck.Test[0], "NONE")
	result := deployment.ComposeService{Service: s, RegionOverride: r.text(override["region"], "region", ""), NetworkMode: r.text(body["network_mode"], "network_mode", "bridge")}
	if result.NetworkMode == "" {
		result.NetworkMode = "bridge"
	}
	if body["stop_grace_period"] != nil {
		result.StopGracePeriod, err = scalar(body["stop_grace_period"])
		if err != nil {
			return result, err
		}
	}
	result.Mounts, result.HasVolumes = decodeMounts(r, body["volumes"])
	return result, nil
}

// The declaration order controls task order and sibling extra_hosts. YAML merges
// are already validated by mapping; collect inherited keys before explicit keys.
func serviceOrder(data []byte, services map[string]any) []string {
	var document yaml.Node
	_ = yaml.Unmarshal(data, &document)
	var result []string
	seen := map[*yaml.Node]bool{}
	var visit func(*yaml.Node)
	visit = func(n *yaml.Node) {
		if n == nil || seen[n] {
			return
		}
		seen[n] = true
		if n.Kind == yaml.AliasNode {
			visit(n.Alias)
			return
		}
		if n.Kind == yaml.SequenceNode {
			for _, child := range n.Content {
				visit(child)
			}
			return
		}
		if n.Kind != yaml.MappingNode {
			return
		}
		for i := 0; i < len(n.Content); i += 2 {
			key := n.Content[i].Value
			if key == "<<" {
				visit(n.Content[i+1])
			} else if _, ok := services[key]; ok && !slices.Contains(result, key) {
				result = append(result, key)
			}
		}
	}
	if len(document.Content) > 0 {
		for i := 0; i < len(document.Content[0].Content); i += 2 {
			if document.Content[0].Content[i].Value == "services" {
				visit(document.Content[0].Content[i+1])
			}
		}
	}
	var missing []string
	for key := range services {
		if !slices.Contains(result, key) {
			missing = append(missing, key)
		}
	}
	slices.Sort(missing)
	return append(result, missing...)
}
