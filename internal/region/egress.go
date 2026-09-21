package region

import (
	"fmt"
	"strings"
)

// Built-in policy is fixed; only custom regions read their configured mode.
// A custom region must be registered before an online deployment can use it.
func EgressMode(name string, custom map[string]string) (string, error) {
	if _, err := Parse(name); err != nil {
		return "", err
	}
	switch name {
	case "cn", "home":
		return "proxy", nil
	case "global":
		return "direct", nil
	}
	mode, ok := custom[name]
	if !ok {
		return "", fmt.Errorf("unknown region %q; register it before deploying", name)
	}
	mode = strings.ToLower(strings.TrimSpace(mode))
	if mode == "" {
		mode = "proxy"
	}
	if mode != "proxy" && mode != "direct" {
		return "", fmt.Errorf("region %s egress must be proxy or direct", name)
	}
	return mode, nil
}
