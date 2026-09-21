package deployment

import (
	"fmt"
	"maps"
	"net/url"
	"strings"

	"github.com/LiuTianjie/luma/internal/region"
)

// EgressSettings contains only routing metadata from the Control snapshot.
type EgressSettings struct {
	Regions                                      map[string]string
	NomadRPCAddr, NomadServerAddr, NomadHTTPAddr string
	ManagerHosts                                 []string
}

func needsEgressDefaults(s Service) bool {
	_, http := s.Environment["HTTP_PROXY"]
	_, https := s.Environment["HTTPS_PROXY"]
	return s.Proxy && (!http || !https)
}

func proxyHost(raw string) (string, error) {
	value := strings.TrimSpace(raw)
	if value == "" {
		return "", nil
	}
	if !strings.Contains(value, "://") {
		if strings.Count(value, ":") > 1 && !strings.HasPrefix(value, "[") {
			value = "[" + value + "]"
		}
		value = "http://" + value
	}
	u, err := url.Parse(value)
	if err != nil || u.User != nil || u.Hostname() == "" || u.RawQuery != "" || u.Fragment != "" || u.Path != "" && u.Path != "/" || (u.Scheme != "http" && u.Scheme != "https") {
		return "", fmt.Errorf("invalid Nomad gateway address in Control configuration")
	}
	return u.Hostname(), nil
}

func ResolveEgressProxy(name string, settings EgressSettings, config RenderConfig) (string, error) {
	mode, err := region.EgressMode(name, settings.Regions)
	if err != nil || mode == "direct" {
		return "", err
	}
	host := ""
	for _, value := range []string{settings.NomadRPCAddr, settings.NomadServerAddr, config.NomadServer} {
		if strings.TrimSpace(value) != "" {
			host, err = proxyHost(value)
			if err != nil {
				return "", err
			}
			break
		}
	}
	if host == "" {
		for _, value := range settings.ManagerHosts {
			host, err = proxyHost(value)
			if err != nil {
				return "", err
			}
			if host != "" {
				break
			}
		}
	}
	if host == "" {
		value := settings.NomadHTTPAddr
		if value == "" {
			value = config.NomadAddr
		}
		host, err = proxyHost(value)
		if err != nil {
			return "", err
		}
		if host == "127.0.0.1" || host == "localhost" || host == "::1" {
			host = ""
		}
	}
	if host == "" {
		return "", fmt.Errorf("proxy requested in region %s but no manager gateway address is configured; set defaults.nomadServer or register a manager address", name)
	}
	if strings.Contains(host, ":") {
		host = "[" + host + "]"
	}
	return "http://" + host + ":7890", nil
}

// RuntimeEnvironment preserves explicit values, including an explicit empty value.
// The compiler remains deterministic and never reads process environment.
func RuntimeEnvironment(service Service, proxy string) map[string]string {
	env := maps.Clone(service.Environment)
	if !service.Proxy || proxy == "" {
		return env
	}
	if env == nil {
		env = map[string]string{}
	}
	for _, name := range []string{"HTTP_PROXY", "HTTPS_PROXY"} {
		if _, exists := env[name]; !exists {
			env[name] = proxy
		}
	}
	return env
}
