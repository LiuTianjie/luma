package registry

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"regexp"
	"strings"
)

type CacheConfig struct {
	PullHost, PushHost, Builder string
	Insecure                    *bool
}
type CachePlan struct {
	Source, Deployed, Push, Destination, Builder, Platform string
	Needed, Insecure                                       bool
}

var cachePart = regexp.MustCompile(`[^a-z0-9._-]+`)

func RegistryHost(value string) (string, error) {
	value = strings.ToLower(strings.TrimRight(strings.TrimSpace(value), "/"))
	if value == "" || strings.ContainsAny(value, "/@ \t\r\n") || strings.Contains(value, "://") {
		return "", fmt.Errorf("registry must be a host with optional port")
	}
	i, err := ParseImage(value + "/luma-check:check")
	if err != nil || i.Registry != value {
		return "", fmt.Errorf("invalid registry host")
	}
	return value, nil
}

func PlanCache(image, platform string, config CacheConfig) (CachePlan, error) {
	i, err := ParseImage(image)
	p := CachePlan{Source: image, Deployed: image, Platform: platform}
	if err != nil {
		return p, err
	}
	if config.PullHost == "" && config.PushHost == "" {
		return p, nil
	}
	pull, err := RegistryHost(config.PullHost)
	if err != nil {
		return p, fmt.Errorf("invalid Builder pull registry: %w", err)
	}
	push, err := RegistryHost(config.PushHost)
	if err != nil {
		return p, fmt.Errorf("invalid Builder push registry: %w", err)
	}
	p.Builder = config.Builder
	if p.Builder == "" {
		p.Builder = "builder"
	}
	if strings.EqualFold(i.Registry, pull) || strings.EqualFold(i.Registry, push) {
		if strings.EqualFold(i.Registry, push) {
			p.Deployed = pull + strings.TrimPrefix(image, i.Registry)
		}
		return p, nil
	}
	if platform != "" && platform != "linux/amd64" && platform != "linux/arm64" {
		return p, fmt.Errorf("unsupported runtime image platform")
	}
	if config.Insecure == nil {
		return p, fmt.Errorf("LUMA_LAE_BUILDER_REGISTRY_INSECURE must be explicitly set to 0 or 1")
	}
	parts := strings.Split(i.Path, "/")
	if (i.Registry == "docker.io" || i.Registry == "registry-1.docker.io") && len(parts) == 1 {
		parts = append([]string{"library"}, parts...)
	}
	parts = append([]string{i.Registry}, parts...)
	for n, part := range parts {
		parts[n] = strings.Trim(cachePart.ReplaceAllString(strings.ToLower(part), "-"), "-.")
		if parts[n] == "" {
			return p, fmt.Errorf("invalid image cache repository")
		}
	}
	keyPlatform := platform
	if keyPlatform == "" {
		keyPlatform = "multi-platform"
	}
	hash := sha256.Sum256([]byte(image + "\n" + keyPlatform))
	repository := "luma-cache/" + strings.Join(parts, "/") + ":" + hex.EncodeToString(hash[:])[:20]
	p.Push, p.Destination = push+"/"+repository, pull+"/"+repository
	p.Needed, p.Insecure = true, *config.Insecure
	return p, nil
}

type RuntimeAuth struct {
	Username      string `json:"username"`
	Password      string `json:"password"`
	ServerAddress string `json:"server_address"`
}
