package buildexec

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"

	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/deployment"
	"go.yaml.in/yaml/v3"
)

type imagePlan struct {
	name, repo, context, dockerfile, platform, sourceImage string
	args                                                   map[string]string
}
type buildPlan struct {
	kind, manifest, sidecar, registry string
	compose                           map[string]any
	images                            []imagePlan
}

func sortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for key := range m {
		keys = append(keys, key)
	}
	slices.Sort(keys)
	return keys
}

var argName = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

func buildArgs(raw any) (map[string]string, error) {
	values := map[string]any{}
	switch v := raw.(type) {
	case nil:
	case map[string]any:
		values = v
	case []any:
		for _, item := range v {
			value, ok := item.(string)
			key, text, found := strings.Cut(value, "=")
			if !ok || !found {
				return nil, fmt.Errorf("build args must be literal NAME=value entries")
			}
			values[key] = text
		}
	default:
		return nil, fmt.Errorf("build args must be a mapping or list")
	}
	if len(values) > 64 {
		return nil, fmt.Errorf("too many build args")
	}
	result := map[string]string{}
	for key, value := range values {
		if !argName.MatchString(key) {
			return nil, fmt.Errorf("invalid build arg name")
		}
		var s string
		switch v := value.(type) {
		case string:
			s = v
		case bool:
			s = strconv.FormatBool(v)
		case int:
			s = strconv.Itoa(v)
		case float64:
			s = strconv.FormatFloat(v, 'g', -1, 64)
		default:
			return nil, fmt.Errorf("build arg %s must have a literal scalar value", key)
		}
		if len(s) > 4096 || strings.ContainsAny(s, "\x00\r\n") {
			return nil, fmt.Errorf("invalid build arg value")
		}
		result[key] = s
	}
	return result, nil
}
func (s *session) plan(root, sha, kind, path string) (buildPlan, error) {
	r := s.request
	p := buildPlan{kind: kind, registry: r.RegistryHost, sidecar: r.ComposeSidecar}
	raw := map[string]any{}
	var err error
	if path != "" {
		raw, p.manifest, err = readYAML(path)
		if err != nil {
			return p, err
		}
	}
	if kind != "compose" {
		p.kind = "service"
		block := object(raw, "build")
		image := imagePlan{repo: first(text(block, "repo"), r.Repo), name: "service"}
		if s.reservedRepository {
			image.repo = r.Repo
		}
		image.context, err = repoPath(root, root, first(r.Context, text(block, "context"), "."))
		if err != nil {
			return p, err
		}
		image.dockerfile, err = repoPath(root, root, first(r.Dockerfile, text(block, "dockerfile"), "Dockerfile"))
		if err != nil {
			return p, err
		}
		image.platform, err = builddomain.TargetPlatform(r, text(raw, "node"), first(r.Region, text(raw, "region")), first(r.Platform, text(block, "platform")))
		if err != nil {
			return p, err
		}
		p.images = []imagePlan{image}
	} else {
		composeFile, err := repoPath(root, filepath.Dir(path), first(text(raw, "compose"), "docker-compose.yml"))
		if err != nil {
			return p, err
		}
		p.compose, _, err = readYAML(composeFile)
		if err != nil {
			return p, err
		}
		services := object(p.compose, "services")
		if len(services) == 0 {
			return p, fmt.Errorf("Compose requires nonempty services")
		}
		specs := map[string]map[string]any{}
		for _, name := range sortedKeys(services) {
			body, ok := services[name].(map[string]any)
			if !ok {
				return p, fmt.Errorf("invalid Compose service: %s", name)
			}
			value := body["build"]
			if value == nil {
				if text(body, "image") == "" {
					return p, fmt.Errorf("Compose service needs image or build: %s", name)
				}
				continue
			}
			switch v := value.(type) {
			case string:
				specs[name] = map[string]any{"context": v}
			case map[string]any:
				specs[name] = v
			default:
				return p, fmt.Errorf("invalid Compose build for %s", name)
			}
		}
		for _, name := range sortedKeys(specs) {
			body := services[name].(map[string]any)
			spec := specs[name]
			override := object(object(raw, "services"), name)
			image := imagePlan{name: name, sourceImage: text(body, "image")}
			image.context, err = repoPath(root, filepath.Dir(composeFile), first(r.Context, text(spec, "context"), "."))
			if err != nil {
				return p, err
			}
			image.dockerfile, err = repoPath(root, image.context, first(r.Dockerfile, text(spec, "dockerfile"), "Dockerfile"))
			if err != nil {
				return p, err
			}
			image.platform, err = builddomain.TargetPlatform(r, first(text(override, "node"), text(raw, "node")), first(text(override, "region"), r.Region, text(raw, "region")), first(r.Platform, text(spec, "platform"), text(body, "platform")))
			if err != nil {
				return p, err
			}
			fallback := r.Repo
			if len(specs) > 1 {
				fallback += "/" + (deployment.Service{Name: name}).Slug()
			}
			image.repo = first(text(spec, "repo"), text(spec, "x-luma-repo"), fallback)
			if s.reservedRepository {
				image.repo = fallback
			}
			image.args, err = buildArgs(spec["args"])
			if err != nil {
				return p, err
			}
			p.images = append(p.images, image)
		}
	}
	repos := map[string]bool{}
	for _, image := range p.images {
		if err := builddomain.ValidateImageRepository(r.RegistryHost, image.repo); err != nil {
			return p, err
		}
		if repos[image.repo] {
			return p, fmt.Errorf("multiple builds resolve to the same image repository")
		}
		repos[image.repo] = true
		info, err := os.Stat(image.context)
		if err != nil || !info.IsDir() {
			return p, fmt.Errorf("build context must be a directory")
		}
		info, err = os.Stat(image.dockerfile)
		if err != nil || !info.Mode().IsRegular() {
			return p, fmt.Errorf("Dockerfile must be a regular file")
		}
	}
	return p, nil
}
func (p buildPlan) result(sha string) (builddomain.ImageResult, error) {
	r := builddomain.ImageResult{Kind: p.kind, SHA: sha, Manifest: p.manifest, ComposeSidecar: p.sidecar}
	if p.kind == "service" {
		r.Image = p.registry + "/" + p.images[0].repo + ":" + sha
		r.Message = "Built and pushed " + r.Image
		return r, nil
	}
	r.Images = map[string]string{}
	r.ImageAliases = map[string]string{}
	ambiguous := map[string]bool{}
	services := object(p.compose, "services")
	for _, image := range p.images {
		reference := p.registry + "/" + image.repo + ":" + sha
		r.Images[image.name] = reference
		if r.Image == "" {
			r.Image = reference
		}
		body := services[image.name].(map[string]any)
		body["image"] = reference
		delete(body, "build")
		if image.sourceImage != "" {
			if old := r.ImageAliases[image.sourceImage]; old != "" && old != reference {
				ambiguous[image.sourceImage] = true
			}
			r.ImageAliases[image.sourceImage] = reference
		}
	}
	for source := range ambiguous {
		delete(r.ImageAliases, source)
	}
	for name, value := range services {
		if _, built := r.Images[name]; built {
			continue
		}
		body := value.(map[string]any)
		if replacement := r.ImageAliases[text(body, "image")]; replacement != "" {
			body["image"] = replacement
		}
	}
	raw, err := yaml.Marshal(p.compose)
	r.ComposeContent = string(raw)
	r.Message = fmt.Sprintf("Built and pushed %d compose image(s)", len(p.images))
	if len(p.images) == 0 {
		r.Message = "Compose images already declared"
	}
	return r, err
}
