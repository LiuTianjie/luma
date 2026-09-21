package cli

import (
	"context"
	"fmt"
	"time"

	builddomain "github.com/LiuTianjie/luma/internal/build"
)

func buildConfigCommand(ctx context.Context, args []string, o options, streams IO) error {
	if len(args) != 2 {
		return fmt.Errorf("usage: build config [--node NAME] [--default-node NAME] [--registry-host HOST] [--push-host HOST] [--direct-egress-node NAME | --clear-direct-egress]")
	}
	if o.clearDirectEgress && o.used["direct-egress-node"] {
		return fmt.Errorf("--clear-direct-egress cannot be combined with --direct-egress-node")
	}
	c := builddomain.ConfigChange{Nodes: o.buildNodes, DefaultNode: o.defaultBuildNode}
	if o.used["registry-host"] {
		c.RegistryHost = &o.registryHost
	}
	if o.used["push-host"] {
		c.PushHost = &o.pushHost
	}
	if o.clearDirectEgress {
		c.DirectEgressNodes = &[]string{}
	} else if o.used["direct-egress-node"] {
		c.DirectEgressNodes = &o.directEgressNodes
	}
	c, err := c.Validate()
	if err != nil {
		return err
	}
	write := len(c.Nodes) > 0 || c.DefaultNode != "" || c.RegistryHost != nil || c.PushHost != nil || c.DirectEgressNodes != nil
	client, err := clientFor(o, streams)
	if err != nil {
		return err
	}
	defer client.Close()
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	result, err := client.BuildConfig(ctx, c, write)
	if err != nil {
		return err
	}
	return success(streams, o, result)
}
