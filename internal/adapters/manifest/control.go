package manifest

import (
	"github.com/LiuTianjie/luma/internal/deployment"
	"go.yaml.in/yaml/v3"
)

type Decoder struct{}

func (Decoder) Service(raw string) (deployment.Service, error) { return DecodeService([]byte(raw)) }
func (Decoder) Compose(raw, compose string) (deployment.Compose, error) {
	return decodeCompose([]byte(raw), []byte(compose), true)
}
func (Decoder) Pin(raw, node string, compose bool) (string, error) {
	root, err := mapping([]byte(raw))
	if err != nil {
		return "", err
	}
	if !compose {
		root["node"] = node
	} else {
		services, _ := root["services"].(map[string]any)
		if services == nil {
			services = map[string]any{}
		}
		// The coordinator supplies all service names separately through Compose
		// pinning below; existing local volume declarations get the same owner.
		for key, value := range services {
			v, _ := value.(map[string]any)
			if v == nil {
				v = map[string]any{}
			}
			v["node"] = node
			services[key] = v
		}
		root["services"] = services
		volumes, _ := root["volumes"].(map[string]any)
		for _, value := range volumes {
			v, _ := value.(map[string]any)
			local, _ := v["local"].(map[string]any)
			if local != nil {
				local["node"] = node
			}
		}
	}
	data, err := yaml.Marshal(root)
	return string(data), err
}
func (Decoder) PinCompose(raw, node string, names []string) (string, error) {
	root, err := mapping([]byte(raw))
	if err != nil {
		return "", err
	}
	services, _ := root["services"].(map[string]any)
	if services == nil {
		services = map[string]any{}
	}
	for _, name := range names {
		v, _ := services[name].(map[string]any)
		if v == nil {
			v = map[string]any{}
		}
		v["node"] = node
		services[name] = v
	}
	root["services"] = services
	data, err := yaml.Marshal(root)
	if err != nil {
		return "", err
	}
	return (Decoder{}).Pin(string(data), node, true)
}
