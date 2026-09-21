package cli

import (
	"github.com/LiuTianjie/luma/internal/adapters/manifest"
	"github.com/LiuTianjie/luma/internal/deployment"
	"os"
	"path/filepath"
)

func loadRenderConfig(o options) (deployment.RenderConfig, string, error) {
	config := manifest.DefaultConfig()
	path := o.config
	if path == "" {
		defaultPath := filepath.Join(o.configRoot, "luma.yaml")
		if _, err := os.Stat(defaultPath); err == nil {
			path = defaultPath
		}
	}
	if path != "" {
		data, err := readYAML(path)
		if err != nil {
			return config, path, err
		}
		config, err = manifest.DecodeConfig(data)
		if err != nil {
			return config, path, err
		}
	}
	return config, path, nil
}
