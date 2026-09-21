package manifest

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strings"

	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/deployment"
	"github.com/LiuTianjie/luma/internal/registry"
	"go.yaml.in/yaml/v3"
)

var builtRevision = regexp.MustCompile(`^[a-fA-F0-9]{4,64}$`)

func (Decoder) BuildName(raw string) (string, error) {
	root, err := mapping([]byte(raw))
	if err != nil {
		return "", err
	}
	name, ok := root["name"].(string)
	if !ok || strings.TrimSpace(name) == "" {
		return "", fmt.Errorf("manifest name is required")
	}
	return name, nil
}

func (Decoder) BuildReplacement(r builddomain.ControlRequest) (string, error) {
	if r.ExpectedName != "" {
		raw, _ := json.Marshal([]string{"manifest", (deployment.Service{Name: r.ExpectedName}).Slug()})
		return string(raw), nil
	}
	if r.ComposeSidecar != "" {
		raw, _ := json.Marshal([]string{"sidecar", r.RepoURL, r.ComposeSidecar})
		return string(raw), nil
	}
	if r.Manifest == "" {
		return "", nil
	}
	root, err := mapping([]byte(r.Manifest))
	if err != nil {
		return "", err
	}
	name, _ := root["name"].(string)
	if strings.TrimSpace(name) == "" {
		return "", fmt.Errorf("manifest name is required")
	}
	raw, _ := json.Marshal([]string{"manifest", (deployment.Service{Name: name}).Slug()})
	return string(raw), nil
}
func (d Decoder) BuiltDeployment(r builddomain.ControlRequest, plan builddomain.ImageRequest, built builddomain.ImageResult) (deployment.ControlRequest, bool, error) {
	return d.builtDeployment(r, plan, built, false)
}
func (d Decoder) LocalBuiltDeployment(r builddomain.ControlRequest, run builddomain.Run, built builddomain.ImageResult) (deployment.ControlRequest, bool, error) {
	if err := builddomain.ValidateLocalResult(run, built); err != nil {
		return deployment.ControlRequest{}, false, err
	}
	return d.builtDeployment(r, builddomain.ImageRequest{RegistryHost: r.RegistryHost, Repo: run.ProjectKey}, built, true)
}
func (Decoder) builtDeployment(r builddomain.ControlRequest, plan builddomain.ImageRequest, built builddomain.ImageResult, local bool) (deployment.ControlRequest, bool, error) {
	out := deployment.ControlRequest{SourceName: r.RepoURL, SkipDNS: r.SkipDNS, EnvSecrets: r.EnvSecrets, Origin: r.Origin}
	compose := built.Kind == "compose" || built.ComposeContent != "" || r.ComposeContent != ""
	if r.ComposeSidecar != "" && (built.Kind != "compose" || built.ComposeSidecar != r.ComposeSidecar) {
		return out, compose, fmt.Errorf("builder did not honor selected Compose sidecar")
	}
	if built.Image == "" && len(built.Images) == 0 {
		return out, compose, fmt.Errorf("build did not return an image")
	}
	check := func(value string) error {
		i, err := registry.ParseImage(value)
		if err != nil {
			return err
		}
		if i.Registry != strings.ToLower(plan.RegistryHost) || i.Tag != built.SHA || !local && !builtRevision.MatchString(built.SHA) {
			return fmt.Errorf("build returned an image outside its registry or commit")
		}
		return nil
	}
	if built.Image != "" {
		if err := check(built.Image); err != nil {
			return out, compose, err
		}
	}
	for _, image := range built.Images {
		if err := check(image); err != nil {
			return out, compose, err
		}
	}
	raw := r.Manifest
	if raw == "" {
		raw = built.Manifest
	}
	root, err := mapping([]byte(raw))
	if err != nil {
		return out, compose, err
	}
	if r.ExpectedName != "" {
		name, _ := root["name"].(string)
		if (deployment.Service{Name: name}).Slug() != (deployment.Service{Name: r.ExpectedName}).Slug() {
			return out, compose, fmt.Errorf("recorded workflow application does not match repository manifest")
		}
	}
	delete(root, "build")
	if r.Region != "" {
		root["region"] = r.Region
	}
	if compose {
		content := built.ComposeContent
		if content == "" {
			content = r.ComposeContent
		}
		source, err := mapping([]byte(content))
		if err != nil {
			return out, compose, err
		}
		services, ok := source["services"].(map[string]any)
		if !ok {
			return out, compose, fmt.Errorf("Compose services are required")
		}
		for name, value := range services {
			service, ok := value.(map[string]any)
			if !ok {
				return out, compose, fmt.Errorf("invalid Compose service")
			}
			original, _ := service["image"].(string)
			image := built.Images[name]
			if image == "" {
				image = built.ImageAliases[original]
			}
			if image != "" {
				if err := check(image); err != nil {
					return out, compose, err
				}
				service["image"] = image
				delete(service, "build")
			}
			if _, exists := service["build"]; exists {
				return out, compose, fmt.Errorf("Compose service %s still requires a build", name)
			}
		}
		encoded, err := yaml.Marshal(source)
		if err != nil {
			return out, compose, err
		}
		out.ComposeContent = string(encoded)
	} else {
		if built.Image == "" {
			return out, false, fmt.Errorf("native build image is required")
		}
		root["image"] = built.Image
		if r.Exposure != "" {
			root["exposure"] = r.Exposure
		}
		if r.Domain != "" {
			root["domain"] = r.Domain
		}
		if r.Port != 0 {
			root["port"] = r.Port
		}
	}
	encoded, err := yaml.Marshal(root)
	out.Manifest = string(encoded)
	return out, compose, err
}
