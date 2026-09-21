package cli

import (
	"context"
	"fmt"
	"github.com/LiuTianjie/luma/internal/adapters/contexts"
	"github.com/LiuTianjie/luma/internal/adapters/controlhttp"
	"io"
	"strings"
	"time"
)

func contextCommand(args []string, o options, streams IO) error {
	store, err := storeFor(streams)
	if err != nil {
		return err
	}
	if len(args) == 2 && args[1] == "list" {
		items, err := store.List()
		if err != nil {
			return err
		}
		return success(streams, o, items)
	}
	if len(args) == 3 && args[1] == "use" {
		if err := store.Use(args[2]); err != nil {
			return err
		}
		return success(streams, o, map[string]string{"context": args[2]})
	}
	return fmt.Errorf("usage: context list | context use NAME")
}
func login(ctx context.Context, args []string, o options, streams IO) error {
	if len(args) != 2 {
		return fmt.Errorf("usage: login HTTPS_URL --token-stdin")
	}
	if o.tokenStdin {
		if o.token != "" {
			return fmt.Errorf("choose --token or --token-stdin")
		}
		data, err := io.ReadAll(io.LimitReader(streams.In, 65537))
		if err != nil {
			return err
		}
		if len(data) > 65536 {
			return fmt.Errorf("token exceeds limit")
		}
		o.token = strings.TrimSpace(string(data))
	} else if o.token == "" {
		o.token = streams.Getenv("LUMA_DEPLOY_TOKEN")
	}
	client, err := controlhttp.New(controlhttp.Options{Endpoint: args[1], Token: o.token, Insecure: o.insecure, ResolveIP: o.resolveIP})
	if err != nil {
		return err
	}
	defer client.Close()
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	id, err := client.VerifyLogin(ctx)
	if err != nil {
		return err
	}
	store, err := storeFor(streams)
	if err != nil {
		return err
	}
	if err := store.Save(contexts.Context{Endpoint: args[1], ClusterID: id, Token: o.token, Insecure: o.insecure, ResolveIP: o.resolveIP}); err != nil {
		return err
	}
	return success(streams, o, map[string]string{"clusterId": id, "endpoint": args[1]})
}
