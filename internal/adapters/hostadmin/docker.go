package hostadmin

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"sort"
	"strings"
	"time"

	"github.com/LiuTianjie/luma/internal/node"
)

const defaultNoProxy = "localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,docker.1panel.live,docker.m.daocloud.io,docker.1ms.run"

func (e Executor) dockerConfig(ctx context.Context, files hostFiles, task node.Task) (node.Outcome, error) {
	if e.Platform != "linux" {
		return node.Outcome{}, fmt.Errorf("Docker daemon configuration requires Linux")
	}
	if e.Docker == "" || e.binary("systemctl") == "" {
		return node.Outcome{}, fmt.Errorf("Docker and systemctl are required")
	}
	var p struct {
		Registry string `json:"registry"`
		Proxy    string `json:"proxy"`
		NoProxy  string `json:"noProxy"`
	}
	if json.Unmarshal(task.Payload, &p) != nil {
		return node.Outcome{}, fmt.Errorf("invalid Docker daemon task field types")
	}
	path := "etc/docker/daemon.json"
	proxyTask := task.Action == "configure-docker-egress-proxy"
	if proxyTask {
		path = "etc/systemd/system/docker.service.d/http-proxy.conf"
	}
	old, err := files.read(path)
	if err != nil {
		return node.Outcome{}, err
	}
	desired := old
	desired.exists = true
	if !old.exists {
		desired.mode = 0644
		desired.uid, desired.gid = -1, -1
	}
	values := map[string]any{}
	if proxyTask {
		if p.Proxy == "" {
			p.Proxy = "http://127.0.0.1:7890"
		}
		if p.NoProxy == "" {
			p.NoProxy = defaultNoProxy
		}
		parsed, err := url.Parse(p.Proxy)
		if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Hostname() == "" || parsed.RawQuery != "" || parsed.Fragment != "" || (parsed.Path != "" && parsed.Path != "/") {
			return node.Outcome{}, fmt.Errorf("Docker daemon proxy must be an HTTP(S) proxy URL")
		}
		var config strings.Builder
		config.WriteString("[Service]\n")
		for _, item := range [][2]string{{"HTTP_PROXY", p.Proxy}, {"HTTPS_PROXY", p.Proxy}, {"NO_PROXY", p.NoProxy}} {
			line, err := node.SystemdEnvironment(item[0], item[1])
			if err != nil {
				return node.Outcome{}, err
			}
			config.WriteString(line)
		}
		desired.data = []byte(config.String())
		desired.mode = 0600
		// Proxy passwords belong only in the daemon config, never task receipts.
		values["proxy"], values["noProxy"] = parsed.Redacted(), p.NoProxy
	} else {
		updated, _, err := node.AddInsecureRegistry(old.data, p.Registry)
		if err != nil {
			return node.Outcome{}, err
		}
		desired.data = updated
		values["registry"] = strings.TrimSpace(p.Registry)
	}
	pending, err := files.read(path + ".luma-go-apply")
	if err != nil {
		return node.Outcome{}, err
	}
	needsActivation := !old.exists || string(old.data) != string(desired.data) || old.mode != desired.mode || pending.exists
	affected := []string{}
	if needsActivation {
		active, err := e.Runner.Run(ctx, node.Command{Args: []string{e.Docker, "ps", "--format", `{{.Label "com.hashicorp.nomad.alloc_id"}}`}, Timeout: 10 * time.Second}, nil)
		if err != nil || active.Code != 0 {
			return node.Outcome{}, fmt.Errorf("could not snapshot Nomad allocations before Docker restart; configuration unchanged")
		}
		seen := map[string]bool{}
		for _, line := range strings.Split(active.Output, "\n") {
			line = strings.TrimSpace(line)
			if line != "" && line != "<no value>" && line != "-" {
				seen[line] = true
			}
		}
		for id := range seen {
			affected = append(affected, id)
		}
		sort.Strings(affected)
	}
	changed, err := files.apply(ctx, path, old, desired, false, func(ctx context.Context, _ bool) error {
		if proxyTask {
			if err := e.require(ctx, "systemctl", "daemon-reload"); err != nil {
				return err
			}
		}
		if err := e.require(ctx, "systemctl", "restart", "docker"); err != nil {
			return err
		}
		return e.require(ctx, "systemctl", "is-active", "--quiet", "docker")
	})
	if err != nil {
		return node.Outcome{}, err
	}
	message := "insecure-registry already configured: " + p.Registry
	if changed {
		message = "insecure-registry configured: " + p.Registry
	}
	if proxyTask {
		message = "Docker daemon egress proxy already configured"
		if changed {
			message = "Docker daemon egress proxy configured"
		}
	}
	values["changed"], values["dockerRestarted"], values["affectedAllocationIds"], values["message"] = changed, changed, affected, message
	return result(values, message)
}
