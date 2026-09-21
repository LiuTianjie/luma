package controlstore

import (
	"errors"
	"strings"

	"github.com/LiuTianjie/luma/internal/registry"
)

func registryAuth(catalog document, image string) (map[string]string, error) {
	parsed, err := registry.ParseImage(image)
	if err != nil {
		return nil, err
	}
	host := strings.ToLower(parsed.Registry)
	candidates := []string{host}
	if host == "docker.io" {
		candidates = append(candidates, "registry-1.docker.io")
	} else if host == "registry-1.docker.io" {
		candidates = append(candidates, "docker.io")
	}
	for _, candidate := range candidates {
		entry := catalog.object(candidate)
		username, password := entry.text("username"), entry.text("password")
		if username == "" || password == "" {
			continue
		}
		address := strings.ToLower(strings.TrimRight(entry.text("serverAddress"), "/"))
		if address == "" {
			address = candidate
		}
		if address == "docker.io" {
			address = "registry-1.docker.io"
		}
		if !slicesContainsHost(candidates, address) {
			return nil, errors.New("registry credential address does not match image host")
		}
		return map[string]string{"username": username, "password": password, "serveraddress": address}, nil
	}
	return nil, nil
}
func slicesContainsHost(hosts []string, host string) bool {
	for _, candidate := range hosts {
		if candidate == host {
			return true
		}
	}
	return false
}
