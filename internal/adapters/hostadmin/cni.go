package hostadmin

import (
	"context"
	"encoding/json"
	"fmt"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/LiuTianjie/luma/internal/node"
)

var cniAllocation = regexp.MustCompile(`id:\s*"([^"]+)"`)

type cniRule struct {
	args                 []string
	allocation, protocol string
	ports                []int
}

// iptables -S quotes comments using shell syntax. Parse words only: no shell is
// ever executed, and metacharacters remain literal arguments.
func ruleWords(line string) ([]string, error) {
	words := []string{}
	var word strings.Builder
	var quote rune
	escaped, started := false, false
	for _, ch := range line {
		if escaped {
			word.WriteRune(ch)
			escaped = false
			started = true
			continue
		}
		if ch == '\\' && quote != '\'' {
			escaped = true
			started = true
			continue
		}
		if quote != 0 {
			if ch == quote {
				quote = 0
			} else {
				word.WriteRune(ch)
			}
			started = true
			continue
		}
		if ch == '\'' || ch == '"' {
			quote = ch
			started = true
			continue
		}
		if ch == ' ' || ch == '\t' {
			if started {
				words = append(words, word.String())
				word.Reset()
				started = false
			}
			continue
		}
		word.WriteRune(ch)
		started = true
	}
	if quote != 0 || escaped {
		return nil, fmt.Errorf("malformed iptables rule")
	}
	if started {
		words = append(words, word.String())
	}
	return words, nil
}
func parseCNIRule(line string) (cniRule, bool) {
	args, err := ruleWords(line)
	if err != nil || len(args) < 2 || args[0] != "-A" || args[1] != "CNI-HOSTPORT-DNAT" {
		return cniRule{}, false
	}
	value := func(key string) string {
		for i := 2; i+1 < len(args); i++ {
			if args[i] == key {
				return args[i+1]
			}
		}
		return ""
	}
	r := cniRule{args: args, protocol: value("-p")}
	match := cniAllocation.FindStringSubmatch(value("--comment"))
	if len(match) != 2 || value("-j") == "" {
		return r, false
	}
	r.allocation = match[1]
	for _, raw := range strings.Split(value("--dports"), ",") {
		p, err := strconv.Atoi(raw)
		if err != nil || p < 1 || p > 65535 {
			return r, false
		}
		r.ports = append(r.ports, p)
	}
	return r, r.protocol == "tcp" || r.protocol == "udp"
}
func (e Executor) repairCNI(ctx context.Context, task node.Task) (node.Outcome, error) {
	if e.Platform != "linux" {
		return node.Outcome{}, fmt.Errorf("Nomad CNI hostport repair requires Linux")
	}
	var request struct {
		Ports []int `json:"ports"`
	}
	if json.Unmarshal(task.Payload, &request) != nil {
		return node.Outcome{}, fmt.Errorf("invalid CNI ports")
	}
	ports := []int{}
	for _, p := range request.Ports {
		if p < 1 || p > 65535 {
			return node.Outcome{}, fmt.Errorf("invalid CNI port")
		}
		if !slices.Contains(ports, p) {
			ports = append(ports, p)
		}
	}
	slices.Sort(ports)
	active, stale := []string{}, []string{}
	deleted := 0
	outcome := func(message string) (node.Outcome, error) {
		return result(map[string]any{"deleted": deleted, "staleAllocIds": stale, "activeAllocIds": active, "hostPorts": ports, "message": message}, message)
	}
	if len(ports) == 0 {
		return outcome("No Nomad CNI host ports requested")
	}
	if e.Docker == "" {
		return node.Outcome{}, fmt.Errorf("Docker is unavailable")
	}
	containers, err := e.Runner.Run(ctx, node.Command{Args: []string{e.Docker, "ps", "--format", `{{.Label "com.hashicorp.nomad.alloc_id"}}`}, Timeout: 10 * time.Second}, nil)
	if err != nil || containers.Code != 0 {
		return node.Outcome{}, fmt.Errorf("could not inspect active Nomad allocations")
	}
	for _, id := range strings.Fields(containers.Output) {
		if id != "<no" && id != "value>" && id != "-" && !slices.Contains(active, id) {
			active = append(active, id)
		}
	}
	slices.Sort(active)
	if len(active) == 0 {
		return outcome("No active Nomad Docker allocations found")
	}
	saved, err := e.run(ctx, "iptables", "-w", "5", "-t", "nat", "-S", "CNI-HOSTPORT-DNAT")
	if err != nil || saved.Code != 0 {
		return node.Outcome{}, fmt.Errorf("could not inspect CNI hostport rules")
	}
	rules := []cniRule{}
	for _, line := range strings.Split(saved.Output, "\n") {
		if r, ok := parseCNIRule(line); ok {
			rules = append(rules, r)
		}
	}
	for i, r := range rules {
		if slices.Contains(active, r.allocation) {
			continue
		}
		shadowing := false
		for _, p := range r.ports {
			if !slices.Contains(ports, p) {
				continue
			}
			for _, later := range rules[i+1:] {
				if later.protocol == r.protocol && slices.Contains(later.ports, p) && slices.Contains(active, later.allocation) {
					shadowing = true
				}
			}
		}
		if !shadowing {
			continue
		}
		args := append([]string{"-w", "5", "-t", "nat", "-D"}, r.args[1:]...)
		if err := e.require(ctx, "iptables", args...); err != nil {
			return node.Outcome{}, err
		}
		deleted++
		if !slices.Contains(stale, r.allocation) {
			stale = append(stale, r.allocation)
		}
	}
	slices.Sort(stale)
	return outcome(fmt.Sprintf("Deleted %d stale Nomad CNI hostport rule(s)", deleted))
}
