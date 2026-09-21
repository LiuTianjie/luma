package imageexec

import (
	"context"
	"encoding/base64"
	"fmt"
	"net/url"
	"strings"
	"time"

	"github.com/LiuTianjie/luma/internal/node"
	"github.com/LiuTianjie/luma/internal/registry"
)

type mirrorPayload struct {
	Source          string        `json:"sourceImage"`
	Push            string        `json:"pushImage"`
	Destination     string        `json:"destinationImage"`
	Platform        string        `json:"platform"`
	Proxy           string        `json:"proxy"`
	Insecure        bool          `json:"insecure"`
	Timeout         int           `json:"timeout"`
	RegistryAuth    *registryAuth `json:"registryAuth"`
	SourceAuth      *registryAuth `json:"sourceRegistryAuth"`
	DestinationAuth *registryAuth `json:"destinationRegistryAuth"`
}

func (e Executor) mirror(ctx context.Context, action string, p mirrorPayload, emit func(node.Event)) (node.Outcome, error) {
	if e.Crane == "" {
		return node.Outcome{}, fmt.Errorf("image mirror requires crane on the builder node")
	}
	plan, err := registry.PlanMirror(action, p.Source, p.Push, p.Destination, p.Platform)
	if err != nil {
		return node.Outcome{}, err
	}
	proxy := strings.TrimSpace(p.Proxy)
	var proxySecrets []string
	if proxy != "" {
		parsed, err := url.Parse(proxy)
		if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Hostname() == "" || parsed.RawQuery != "" || parsed.Fragment != "" || (parsed.Path != "" && parsed.Path != "/") {
			return node.Outcome{}, fmt.Errorf("image mirror proxy must be an HTTP(S) proxy URL")
		}
		if parsed.User != nil {
			password, _ := parsed.User.Password()
			pair := parsed.User.Username() + ":" + password
			proxySecrets = []string{proxy, parsed.User.String(), password, pair, base64.StdEncoding.EncodeToString([]byte(pair))}
		}
	}
	s, err := newAuthSession(e.Scratch, p.SourceAuth, p.DestinationAuth, p.RegistryAuth)
	if err != nil {
		return node.Outcome{}, err
	}
	defer s.close()
	s.secrets = append(s.secrets, proxySecrets...)
	env := map[string]string{"DOCKER_CONFIG": s.path}
	if proxy != "" {
		env["HTTP_PROXY"], env["HTTPS_PROXY"], env["http_proxy"], env["https_proxy"] = proxy, proxy, proxy, proxy
	}
	noProxy := []string{"localhost", "127.0.0.1", "::1", plan.Push.Registry}
	if plan.Destination.Registry != plan.Push.Registry {
		noProxy = append(noProxy, plan.Destination.Registry)
	}
	env["NO_PROXY"], env["no_proxy"] = strings.Join(noProxy, ","), strings.Join(noProxy, ",")
	args := []string{e.Crane, "copy", plan.Source.Reference, plan.Push.Reference}
	if plan.Platform != "" {
		args = append(args, "--platform", plan.Platform)
	}
	if p.Insecure {
		args = append(args, "--insecure")
	}
	fallback := 900
	if action == "cache-runtime-image" {
		fallback = 1800
	}
	timeout := boundedTimeout(p.Timeout, fallback, 60, 1800)
	emitLine(emit, "status", "Caching "+plan.Source.Reference+" on the internal Builder registry.")
	for attempt := 0; ; attempt++ {
		copied, err := e.Runner.Run(ctx, node.Command{Args: args, Env: env, Secrets: s.secrets, Timeout: timeout}, func(line string) { emitLine(emit, "status", s.clean(line)) })
		if err != nil {
			return node.Outcome{}, err
		}
		if copied.Code == 0 {
			break
		}
		if attempt >= 2 || !transient(copied.Output) {
			return node.Outcome{}, fmt.Errorf("image mirror failed: %s", tail(s.clean(copied.Output), 1200))
		}
		emitLine(emit, "status", fmt.Sprintf("Registry transfer was interrupted; retrying (%d/3).", attempt+2))
		timer := time.NewTimer(time.Duration(2<<attempt) * time.Second)
		select {
		case <-ctx.Done():
			timer.Stop()
			return node.Outcome{}, ctx.Err()
		case <-timer.C:
		}
	}
	args = []string{e.Crane, "digest", plan.Push.Reference}
	if p.Insecure {
		args = append(args, "--insecure")
	}
	verified, err := e.Runner.Run(ctx, node.Command{Args: args, Env: env, Secrets: s.secrets, Timeout: 2 * time.Minute}, nil)
	if err != nil {
		return node.Outcome{}, err
	}
	lines := strings.Split(strings.TrimSpace(verified.Output), "\n")
	digest := strings.TrimSpace(lines[len(lines)-1])
	if verified.Code != 0 || !registry.ValidDigest(digest) {
		return node.Outcome{}, fmt.Errorf("image mirror digest verification failed")
	}
	emitLine(emit, "status", "Internal "+plan.Kind+" image verified at "+digest+".")
	return result(map[string]string{"sourceImage": plan.Source.Reference, "pushImage": plan.Push.Reference, "destinationImage": plan.Destination.Reference, "digest": digest, "message": plan.Message}, plan.Message)
}
func transient(output string) bool {
	lower := strings.ToLower(output)
	for _, marker := range []string{"eof", "timeout", "connection reset", "temporary", "tls handshake", "unexpected status code 5"} {
		if strings.Contains(lower, marker) {
			return true
		}
	}
	return false
}
