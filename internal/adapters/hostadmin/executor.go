// Package hostadmin owns privileged host configuration, including atomic file
// replacement and service activation. All filesystem paths are confined beneath
// a composition-time host root; leased tasks cannot choose that root.
package hostadmin

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"time"

	"github.com/LiuTianjie/luma/internal/node"
)

type Executor struct {
	Runner                 node.CommandRunner
	Platform, Root, Docker string
	Privileged             bool
	Lookup                 func(string) string
}

func (e Executor) binary(name string) string {
	if e.Lookup != nil {
		return e.Lookup(name)
	}
	if found, err := exec.LookPath(name); err == nil {
		return found
	}
	for _, directory := range []string{"/usr/sbin/", "/sbin/", "/usr/bin/", "/bin/"} {
		path := directory + name
		if info, err := os.Stat(path); err == nil && !info.IsDir() && info.Mode()&0111 != 0 {
			return path
		}
	}
	return ""
}
func (e Executor) Capabilities() []string {
	var caps []string
	if !e.Privileged || e.Runner == nil {
		return caps
	}
	if e.Platform == "linux" {
		if e.Docker != "" && e.binary("iptables") != "" {
			caps = append(caps, "nomad-cni-repair")
		}
		if e.Docker != "" && e.binary("systemctl") != "" {
			caps = append(caps, "docker-egress-proxy")
		}
	}
	return caps
}
func (e Executor) SupportedActions() []string {
	var actions []string
	for _, capability := range e.Capabilities() {
		switch capability {
		case "nomad-cni-repair":
			actions = append(actions, "repair-nomad-cni-hostports")
		case "docker-egress-proxy":
			actions = append(actions, "configure-insecure-registry", "configure-docker-egress-proxy")
		}
	}
	return actions
}
func (e Executor) Execute(ctx context.Context, task node.Task, emit func(node.Event)) (node.Outcome, error) {
	if err := ctx.Err(); err != nil {
		return node.Outcome{}, err
	}
	if !e.Privileged || e.Runner == nil {
		return node.Outcome{}, fmt.Errorf("host administration requires a privileged agent")
	}
	if e.Platform != "linux" && e.Platform != "darwin" {
		return node.Outcome{}, fmt.Errorf("unsupported host platform")
	}
	if e.Root == "" {
		return node.Outcome{}, fmt.Errorf("host filesystem root is not configured")
	}
	data := strings.TrimSpace(string(task.Payload))
	if len(data) == 0 || len(data) > 4<<20 || data[0] != '{' || !json.Valid(task.Payload) {
		return node.Outcome{}, fmt.Errorf("invalid host administration payload")
	}
	root, err := os.OpenRoot(e.Root)
	if err != nil {
		return node.Outcome{}, err
	}
	defer root.Close()
	files := hostFiles{root: root}
	switch task.Action {
	case "repair-nomad-cni-hostports":
		return e.repairCNI(ctx, task)
	case "configure-insecure-registry", "configure-docker-egress-proxy":
		return e.dockerConfig(ctx, files, task)
	default:
		return node.Outcome{}, fmt.Errorf("unsupported host administration action")
	}
}
func (e Executor) run(ctx context.Context, name string, args ...string) (node.CommandResult, error) {
	binary := e.binary(name)
	if binary == "" {
		return node.CommandResult{}, fmt.Errorf("required host tool is unavailable: %s", name)
	}
	return e.Runner.Run(ctx, node.Command{Args: append([]string{binary}, args...), Timeout: 2 * time.Minute}, nil)
}
func (e Executor) require(ctx context.Context, name string, args ...string) error {
	result, err := e.run(ctx, name, args...)
	if err != nil {
		return err
	}
	if result.Code != 0 {
		return fmt.Errorf("host tool %s failed", name)
	}
	return nil
}
func result(value any, message string) (node.Outcome, error) {
	data, err := json.Marshal(value)
	return node.Outcome{Result: data, Message: message}, err
}
