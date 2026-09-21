package controlstore

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"slices"
	"strings"

	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/deployment"
)

type buildInventory struct {
	nodes    map[string]deployment.ImageNode
	statuses map[string]string
}

func readBuildInventory(ctx context.Context, tx *sql.Tx, config document) (buildInventory, error) {
	result := buildInventory{nodes: map[string]deployment.ImageNode{}, statuses: map[string]string{}}
	builders := config.strings("nodes")
	for key, v := range config.object("nodes") {
		if string(v) != "false" && string(v) != "null" {
			builders = append(builders, key)
		}
	}
	if config.text("defaultNode") != "" {
		builders = append(builders, config.text("defaultNode"))
	}
	rows, err := tx.QueryContext(ctx, `SELECT id,CASE WHEN length(CAST(payload AS BLOB))<=262144 THEN payload ELSE NULL END FROM control_entities WHERE kind='nodes' ORDER BY id LIMIT 10001`)
	if err != nil {
		return result, err
	}
	defer rows.Close()
	for rows.Next() {
		var key string
		var raw []byte
		if err := rows.Scan(&key, &raw); err != nil {
			return result, err
		}
		r, err := readDocument(raw)
		if err != nil {
			return result, err
		}
		l := r.object("labels")
		status := nodeAgentStatus(r)
		aliases := append(r.strings("aliases"), r.text("name"), r.text("displayName"), r.text("hostname"), r.text("nodeId"), r.text("nomadNodeId"), l.text("luma.node.name"), l.text("luma_node_name"))
		result.nodes[key] = deploymentImageNode(key, r, config, builders, aliases, deploymentNodeReady(r))
		result.statuses[key] = status
	}
	if len(result.nodes) > 10000 {
		return result, fmt.Errorf("too many registered nodes")
	}
	return result, rows.Err()
}
func (inventory buildInventory) resolve(name string) (string, error) {
	name = strings.TrimSpace(name)
	if name == "" || len(name) > 512 || strings.ContainsAny(name, "\x00\r\n") {
		return "", fmt.Errorf("%w: invalid builder node name", builddomain.ErrInput)
	}
	if _, ok := inventory.nodes[name]; ok {
		return name, nil
	}
	found := ""
	for key, n := range inventory.nodes {
		if slices.Contains(n.Aliases, name) {
			if found != "" {
				return "", fmt.Errorf("%w: ambiguous builder node", builddomain.ErrInput)
			}
			found = key
		}
	}
	if found == "" {
		return "", fmt.Errorf("%w: unknown builder node %s", builddomain.ErrInput, name)
	}
	return found, nil
}
func buildConfigSummary(config document, inventory buildInventory) builddomain.ConfigSummary {
	result := builddomain.ConfigSummary{DefaultNode: config.text("defaultNode"), RegistryHost: config.text("registryHost"), PushHost: config.text("pushHost"), DirectEgressNodes: config.strings("directEgressNodes"), Nodes: []builddomain.ConfigNode{}}
	if result.DefaultNode == "" {
		result.DefaultNode = "builder"
	}
	if result.PushHost == "" {
		result.PushHost = "localhost:5000"
	}
	if result.DirectEgressNodes == nil {
		result.DirectEgressNodes = []string{}
	}
	for key, n := range inventory.nodes {
		if !n.Builder {
			continue
		}
		caps := n.Capabilities
		if caps == nil {
			caps = []string{}
		}
		result.Nodes = append(result.Nodes, builddomain.ConfigNode{Name: key, Region: n.Region, AgentStatus: inventory.statuses[key], AgentOS: n.HostOS, Capabilities: caps, Ready: n.Ready && slices.Contains(caps, "docker-build") && (n.SupportedActions == nil || slices.Contains(*n.SupportedActions, "build-image"))})
	}
	slices.SortFunc(result.Nodes, func(a, b builddomain.ConfigNode) int { return strings.Compare(a.Name, b.Name) })
	return result
}
func (s *Store) BuildConfig(ctx context.Context) (builddomain.ConfigSummary, error) {
	var result builddomain.ConfigSummary
	err := s.snapshot(ctx, func(tx *sql.Tx) error {
		config, err := configDocument(ctx, tx, "build", 1<<20)
		if err != nil {
			return err
		}
		inventory, err := readBuildInventory(ctx, tx, config)
		if err != nil {
			return err
		}
		result = buildConfigSummary(config, inventory)
		return nil
	})
	return result, err
}
func (s *Store) ChangeBuildConfig(ctx context.Context, token string, change builddomain.ConfigChange) (builddomain.ConfigSummary, error) {
	var result builddomain.ConfigSummary
	c, err := change.Validate()
	if err != nil {
		return result, err
	}
	if s.writer == nil {
		return result, ErrReadOnly
	}
	err = s.transaction(ctx, s.writer, false, func(tx *sql.Tx) error {
		if err := checkManagement(ctx, tx, token); err != nil {
			return err
		}
		config, err := configDocument(ctx, tx, "build", 1<<20)
		if err != nil {
			return err
		}
		inventory, err := readBuildInventory(ctx, tx, config)
		if err != nil {
			return err
		}
		resolve := func(values []string) ([]string, error) {
			out := []string{}
			for _, value := range values {
				name, err := inventory.resolve(value)
				if err != nil {
					return nil, err
				}
				if !slices.Contains(out, name) {
					out = append(out, name)
				}
			}
			return out, nil
		}
		nodes, err := resolve(c.Nodes)
		if err != nil {
			return err
		}
		defaultNode := c.DefaultNode
		if defaultNode == "" && len(nodes) > 0 {
			defaultNode = nodes[0]
		}
		if defaultNode != "" {
			defaultNode, err = inventory.resolve(defaultNode)
			if err != nil {
				return err
			}
			if !slices.Contains(nodes, defaultNode) {
				nodes = append([]string{defaultNode}, nodes...)
			}
			if err := config.set("defaultNode", defaultNode); err != nil {
				return err
			}
		}
		if len(nodes) > 0 {
			if err := config.set("nodes", nodes); err != nil {
				return err
			}
		}
		if c.DirectEgressNodes != nil {
			direct, err := resolve(*c.DirectEgressNodes)
			if err != nil {
				return err
			}
			slices.Sort(direct)
			if len(direct) == 0 {
				delete(config, "directEgressNodes")
			} else if err := config.set("directEgressNodes", direct); err != nil {
				return err
			}
		}
		for key, value := range map[string]*string{"registryHost": c.RegistryHost, "pushHost": c.PushHost} {
			if value == nil {
				continue
			}
			if *value == "" {
				delete(config, key)
			} else if err := config.set(key, *value); err != nil {
				return err
			}
		}
		raw, err := json.Marshal(config)
		if err != nil {
			return err
		}
		if len(raw) > 1<<20 {
			return builddomain.ErrInput
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO control_config(key,payload) VALUES('build',?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload`, string(raw)); err != nil {
			return err
		}
		if c.RegistryHost != nil || c.PushHost != nil {
			if err := invalidateSetupCheck(ctx, tx, "registry"); err != nil {
				return err
			}
		}
		inventory, err = readBuildInventory(ctx, tx, config)
		if err != nil {
			return err
		}
		result = buildConfigSummary(config, inventory)
		return nil
	})
	return result, err
}
