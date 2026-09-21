package controlapp

import (
	"fmt"
	"io"
	"os"
	"path/filepath"

	"github.com/LiuTianjie/luma/internal/adapters/manifest"
	"github.com/LiuTianjie/luma/internal/deployment"
)

func loadConfig(path string) (deployment.RenderConfig, deployment.DNSConfig, string, error) {
	config, dns := manifest.DefaultConfig(), deployment.DNSConfig{}
	if path == "" {
		return config, dns, "", nil
	}
	absolute, err := filepath.Abs(path)
	if err != nil {
		return config, dns, "", err
	}
	file, err := os.Open(absolute)
	if err != nil {
		return config, dns, "", err
	}
	defer file.Close()
	data, err := io.ReadAll(io.LimitReader(file, (4<<20)+1))
	if err != nil {
		return config, dns, "", err
	}
	if len(data) > 4<<20 {
		return config, dns, "", fmt.Errorf("Control configuration exceeds 4 MiB")
	}
	config, err = manifest.DecodeConfig(data)
	if err != nil {
		return config, dns, "", err
	}
	dns, err = manifest.DecodeDNSConfig(data)
	if err != nil {
		return config, dns, "", err
	}
	base := filepath.Dir(absolute)
	if !filepath.IsAbs(config.StackRoot) {
		config.StackRoot = filepath.Join(base, config.StackRoot)
	}
	if !filepath.IsAbs(config.RoutesRoot) {
		config.RoutesRoot = filepath.Join(base, config.RoutesRoot)
	}
	return config, dns, base, nil
}
