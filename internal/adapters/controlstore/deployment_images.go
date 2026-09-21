package controlstore

import (
	"context"
	"database/sql"
	"slices"
	"strings"

	"github.com/LiuTianjie/luma/internal/deployment"
	"github.com/LiuTianjie/luma/internal/registry"
)

func deploymentImageNode(name string, r, build document, builders, aliases []string, ready bool) deployment.ImageNode {
	agent, labels := r.object("agent"), r.object("labels")
	first := func(values ...string) string {
		for _, v := range values {
			if strings.TrimSpace(v) != "" {
				return strings.TrimSpace(v)
			}
		}
		return ""
	}
	truth := func(v string) bool {
		return slices.Contains([]string{"1", "true", "yes", "on"}, strings.ToLower(strings.TrimSpace(v)))
	}
	n := deployment.ImageNode{Name: name, Aliases: aliases, Region: r.text("region"), Ready: ready, Capabilities: agent.strings("capabilities")}
	if _, exists := agent["supportedActions"]; exists {
		values := agent.strings("supportedActions")
		if values == nil {
			values = []string{}
		}
		n.SupportedActions = &values
	}
	matches := func(values []string) bool {
		for _, value := range values {
			value = strings.TrimSpace(value)
			if value == "" {
				continue
			}
			if value == name || slices.Contains(aliases, value) {
				return true
			}
		}
		return false
	}
	n.Builder = matches(builders) || r.boolean("builder") || slices.Contains(r.strings("roles"), "builder") || slices.Contains(r.strings("roles"), "build")
	for _, value := range []string{r.text("role"), labels.text("role"), labels.text("luma.role"), labels.text("luma.node.role")} {
		n.Builder = n.Builder || value == "builder" || value == "build"
	}
	n.Builder = n.Builder || truth(labels.text("role.builder")) || truth(labels.text("luma.builder"))
	if len(builders) == 0 && name == "builder" {
		n.Builder = true
	}
	n.DirectEgress = matches(build.strings("directEgressNodes")) || truth(first(labels.text("luma.egress.direct"), labels.text("egress.direct")))
	attrs := r.object("nomadAttributes")
	if len(attrs) == 0 {
		attrs = r.object("attributes")
	}
	osName := first(r.text("os"), r.text("operatingSystem"), agent.text("os"), attrs.text("os.name"), attrs.text("kernel.name"), attrs.text("driver.docker.os_type"))
	arch := first(r.text("arch"), r.text("architecture"), agent.text("arch"), attrs.text("cpu.arch"), attrs.text("unique.platform.arch"), attrs.text("driver.docker.arch"))
	if direct := first(r.text("nodePlatform"), r.text("platform")); strings.Contains(direct, "/") {
		osName, arch, _ = strings.Cut(direct, "/")
	} else {
		for _, key := range []string{"nodePlatform", "platform"} {
			value := r.object(key)
			directOS := first(value.text("os"), value.text("OS"))
			directArch := first(value.text("arch"), value.text("architecture"), value.text("Architecture"))
			if directOS != "" && directArch != "" {
				osName, arch = directOS, directArch
				break
			}
		}
	}
	n.HostOS = strings.ToLower(strings.TrimSpace(osName))
	if n.HostOS == "macos" || n.HostOS == "mac os x" {
		n.HostOS = "darwin"
	}
	n.Platform = deployment.RuntimeImagePlatform(osName, arch)
	return n
}
func (s *Store) DeploymentImageAuth(ctx context.Context, images map[string]string) (map[string]registry.RuntimeAuth, error) {
	result := map[string]registry.RuntimeAuth{}
	err := s.snapshot(ctx, func(tx *sql.Tx) error {
		catalog, err := configDocument(ctx, tx, "registries", 4<<20)
		if err != nil {
			return err
		}
		for task, image := range images {
			auth, err := registryAuth(catalog, image)
			if err != nil {
				return err
			}
			if auth != nil {
				result[task] = registry.RuntimeAuth{Username: auth["username"], Password: auth["password"], ServerAddress: auth["serveraddress"]}
			}
		}
		return nil
	})
	return result, err
}
