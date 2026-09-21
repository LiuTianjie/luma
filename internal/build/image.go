package build

import (
	"fmt"
	"net/url"
	"regexp"
	"slices"
	"strings"

	"github.com/LiuTianjie/luma/internal/registry"
)

type RegistryAuth struct {
	Username      string `json:"username"`
	Password      string `json:"password"`
	ServerAddress string `json:"serveraddress"`
}

type ImageRequest struct {
	RepoURL                 string              `json:"repoUrl"`
	Ref                     string              `json:"ref"`
	RegistryHost            string              `json:"registryHost"`
	PushHost                string              `json:"pushHost"`
	Repo                    string              `json:"repo"`
	Proxy                   string              `json:"proxy"`
	GitProviderID           string              `json:"gitProviderId,omitempty"`
	GitToken                string              `json:"gitToken,omitempty"`
	GitUsername             string              `json:"gitUsername,omitempty"`
	RegistryAuth            *RegistryAuth       `json:"registryAuth,omitempty"`
	BuildTimeout            int                 `json:"buildTimeout"`
	Context                 string              `json:"context"`
	Dockerfile              string              `json:"dockerfile"`
	Platform                string              `json:"platform"`
	Region                  string              `json:"region"`
	ComposeSidecar          string              `json:"composeSidecar"`
	TargetPlatformsByNode   map[string]string   `json:"targetPlatformsByNode"`
	TargetPlatformsByRegion map[string][]string `json:"targetPlatformsByRegion"`
}

type ImageResult struct {
	Kind           string            `json:"kind"`
	Image          string            `json:"image"`
	SHA            string            `json:"sha"`
	Manifest       string            `json:"manifest"`
	ComposeContent string            `json:"composeContent,omitempty"`
	ComposeSidecar string            `json:"composeSidecar,omitempty"`
	Images         map[string]string `json:"images,omitempty"`
	ImageAliases   map[string]string `json:"imageAliases,omitempty"`
	Message        string            `json:"message"`
}

var scpRepository = regexp.MustCompile(`^git@[A-Za-z0-9.-]+:[A-Za-z0-9_./-]+$`)
var fullCommit = regexp.MustCompile(`^[a-fA-F0-9]{40}(?:[a-fA-F0-9]{24})?$`)

func IsFullCommit(s string) bool { return fullCommit.MatchString(s) }
func NormalizeRepository(value string) (string, error) {
	value = strings.TrimSpace(value)
	if len(value) == 0 || len(value) > 4096 || strings.ContainsAny(value, "\x00\r\n\t ") || strings.HasPrefix(value, "-") {
		return "", fmt.Errorf("invalid Git repository URL")
	}
	if scpRepository.MatchString(value) {
		return value, nil
	}
	if !strings.Contains(value, "://") {
		value = "https://" + strings.TrimLeft(value, "/")
	}
	u, err := url.Parse(value)
	if err != nil || (u.Scheme != "https" && u.Scheme != "http" && u.Scheme != "ssh") || u.Hostname() == "" || u.Path == "" || u.RawQuery != "" || u.Fragment != "" {
		return "", fmt.Errorf("invalid Git repository URL")
	}
	if u.User != nil {
		return "", fmt.Errorf("Git repository URL must not contain credentials; use a provider credential")
	}
	return value, nil
}
func ValidateImageRepository(host, repo string) error {
	if _, err := registry.ValidateCredential(registry.CredentialChange{Host: host}, true); err != nil {
		return err
	}
	i, err := registry.ParseImage(host + "/" + repo + ":validation")
	if err != nil || i.Path != repo || i.Tag != "validation" || strings.ToLower(repo) != repo {
		return fmt.Errorf("invalid build image repository")
	}
	return nil
}
func (r ImageRequest) Validate() (ImageRequest, error) {
	var err error
	r.RepoURL, err = NormalizeRepository(r.RepoURL)
	if err != nil {
		return r, err
	}
	if len(r.Ref) > 1024 || strings.HasPrefix(r.Ref, "-") || strings.ContainsAny(r.Ref, "\x00\r\n \t") {
		return r, fmt.Errorf("invalid Git ref")
	}
	if r.PushHost == "" {
		r.PushHost = "localhost:5000"
	}
	for _, host := range []string{r.RegistryHost, r.PushHost} {
		if err := ValidateImageRepository(host, r.Repo); err != nil {
			return r, err
		}
	}
	if r.Proxy != "" {
		u, err := url.Parse(r.Proxy)
		if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Hostname() == "" || strings.ContainsAny(r.Proxy, "\x00\r\n") {
			return r, fmt.Errorf("build proxy must be an HTTP(S) URL")
		}
	}
	if a := r.RegistryAuth; a != nil && (len(a.Username) > 4096 || len(a.Password) > 64<<10) {
		return r, fmt.Errorf("build registry credential exceeds limit")
	}
	if r.BuildTimeout == 0 {
		r.BuildTimeout = 7200
	}
	if r.BuildTimeout < 1 || r.BuildTimeout > 86400 {
		return r, fmt.Errorf("build timeout must be between 1 and 86400 seconds")
	}
	if len(r.GitToken) > 64<<10 || strings.ContainsAny(r.GitToken, "\x00\r\n") || strings.ContainsAny(r.GitUsername, "\x00\r\n") {
		return r, fmt.Errorf("invalid Git credential")
	}
	return r, nil
}
func TargetPlatform(r ImageRequest, node, region, requested string) (string, error) {
	parts := func(s string) ([]string, error) {
		values := []string{}
		for _, p := range strings.Split(s, ",") {
			p = strings.ToLower(strings.TrimSpace(p))
			if p == "" {
				continue
			}
			if p != "linux/amd64" && p != "linux/arm64" {
				return nil, fmt.Errorf("unsupported build platform: %s", p)
			}
			if !slices.Contains(values, p) {
				values = append(values, p)
			}
		}
		slices.Sort(values)
		return values, nil
	}
	allowed, err := parts(requested)
	if err != nil {
		return "", err
	}
	if len(r.TargetPlatformsByNode) == 0 && len(r.TargetPlatformsByRegion) == 0 {
		if len(allowed) == 0 {
			return "linux/amd64", nil
		}
		return strings.Join(allowed, ","), nil
	}
	values := ""
	if node != "" {
		values = r.TargetPlatformsByNode[node]
	} else if region != "" {
		values = strings.Join(r.TargetPlatformsByRegion[region], ",")
	}
	target, err := parts(values)
	if err != nil {
		return "", err
	}
	if len(target) == 0 {
		return "", fmt.Errorf("cannot resolve a ready deployment target architecture for node %s region %s", node, region)
	}
	for _, p := range target {
		if len(allowed) > 0 && !slices.Contains(allowed, p) {
			return "", fmt.Errorf("requested build platform does not cover target %s", p)
		}
	}
	return strings.Join(target, ","), nil
}
