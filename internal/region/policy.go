// Package region owns placement names and exposure policy, independent of I/O.
package region

import (
	"fmt"
	"regexp"
	"strings"
)

var namePattern = regexp.MustCompile(`^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$`)

func Parse(value string) (string, error) {
	name := strings.ToLower(strings.TrimSpace(value))
	if name == "" {
		return "", fmt.Errorf("region is required")
	}
	switch name {
	case "all", "any", "default", "manager", "none", "unknown":
		return "", fmt.Errorf("region name %q is reserved", name)
	}
	if len(name) > 32 || !namePattern.MatchString(name) {
		return "", fmt.Errorf("region name must be lowercase alphanumeric with optional hyphens, start with a letter, and be at most 32 characters")
	}
	return name, nil
}

func ValidateExposure(name, exposure string) error {
	if _, err := Parse(name); err != nil {
		return err
	}
	allowed := exposure == "none"
	switch exposure {
	case "cn-edge":
		allowed = name == "cn"
	case "external-edge":
		allowed = name == "global"
	case "tailscale-relay":
		allowed = name == "home"
	case "cloudflare-tunnel", "tcp-relay":
		allowed = name == "cn" || name == "global" || name == "home"
	}
	if !allowed {
		return fmt.Errorf("region %s does not allow exposure=%s", name, exposure)
	}
	return nil
}
