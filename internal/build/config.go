package build

import (
	"context"
	"fmt"
	"strings"

	"github.com/LiuTianjie/luma/internal/registry"
)

type ConfigChange struct {
	Node              string    `json:"node,omitempty"`
	Nodes             []string  `json:"nodes,omitempty"`
	DefaultNode       string    `json:"defaultNode,omitempty"`
	RegistryHost      *string   `json:"registryHost,omitempty"`
	PushHost          *string   `json:"pushHost,omitempty"`
	DirectEgressNodes *[]string `json:"directEgressNodes,omitempty"`
}
type ConfigNode struct {
	Name         string   `json:"name"`
	Region       string   `json:"region"`
	AgentStatus  string   `json:"agentStatus"`
	AgentOS      string   `json:"agentOs"`
	Capabilities []string `json:"storageCapabilities"`
	Ready        bool     `json:"ready"`
}
type ConfigSummary struct {
	DefaultNode       string       `json:"defaultNode"`
	RegistryHost      string       `json:"registryHost"`
	PushHost          string       `json:"pushHost"`
	DirectEgressNodes []string     `json:"directEgressNodes"`
	Nodes             []ConfigNode `json:"nodes"`
}
type ConfigRepository interface {
	BuildConfig(context.Context) (ConfigSummary, error)
	ChangeBuildConfig(context.Context, string, ConfigChange) (ConfigSummary, error)
}

func (c ConfigChange) Validate() (ConfigChange, error) {
	c.Node, c.DefaultNode = strings.TrimSpace(c.Node), strings.TrimSpace(c.DefaultNode)
	if c.Node != "" {
		c.Nodes = []string{c.Node}
	}
	if len(c.Nodes) > 10000 || c.DirectEgressNodes != nil && len(*c.DirectEgressNodes) > 10000 {
		return c, fmt.Errorf("%w: too many builder nodes", ErrInput)
	}
	for _, p := range []**string{&c.RegistryHost, &c.PushHost} {
		if *p == nil {
			continue
		}
		value := strings.TrimSpace(**p)
		if value != "" {
			clean, err := registry.ValidateCredential(registry.CredentialChange{Host: value}, true)
			if err != nil {
				return c, fmt.Errorf("%w: registry must be a host with optional port, without credentials or a URL path", ErrInput)
			}
			value = clean.Host
		}
		*p = &value
	}
	return c, nil
}
