package cli

import (
	"fmt"
	"github.com/LiuTianjie/luma/internal/adapters/contexts"
	"github.com/LiuTianjie/luma/internal/adapters/controlhttp"
	"strings"
)

func storeFor(streams IO) (contexts.Store, error) {
	return contexts.New(streams.Getenv("LUMA_CONFIG_HOME"))
}
func clientFor(o options, streams IO) (*controlhttp.Client, error) {
	connection, err := connectionOptions(o, streams)
	if err != nil {
		return nil, err
	}
	return controlhttp.New(connection)
}
func connectionOptions(o options, streams IO) (controlhttp.Options, error) {
	if o.lockedConnection != nil {
		return *o.lockedConnection, nil
	}
	store, err := storeFor(streams)
	if err != nil {
		return controlhttp.Options{}, err
	}
	if o.contextName == "" {
		o.contextName = streams.Getenv("LUMA_CONTROL_CONTEXT")
	}
	saved, loadErr := store.Load(o.contextName)
	if loadErr != nil && o.contextName != "" {
		return controlhttp.Options{}, loadErr
	}
	if o.endpoint == "" {
		o.endpoint = streams.Getenv("LUMA_CONTROL_URL")
	}
	if o.token == "" {
		o.token = streams.Getenv("LUMA_DEPLOY_TOKEN")
	}
	if o.resolveIP == "" {
		o.resolveIP = streams.Getenv("LUMA_RESOLVE_IP")
	}
	if o.endpoint != "" && strings.TrimRight(o.endpoint, "/") != strings.TrimRight(saved.Endpoint, "/") {
		saved = contexts.Context{}
	}
	if o.endpoint == "" {
		o.endpoint = saved.Endpoint
	}
	if o.token == "" {
		o.token = saved.Token
	}
	if o.resolveIP == "" {
		o.resolveIP = saved.ResolveIP
	}
	insecure := saved.Insecure
	if value := streams.Getenv("LUMA_INSECURE"); value != "" {
		switch strings.ToLower(value) {
		case "1", "true", "yes", "on":
			insecure = true
		case "0", "false", "no", "off":
			insecure = false
		default:
			return controlhttp.Options{}, fmt.Errorf("invalid LUMA_INSECURE")
		}
	}
	if o.insecure {
		insecure = true
	}
	return controlhttp.Options{Endpoint: o.endpoint, Token: o.token, ResolveIP: o.resolveIP, Insecure: insecure}, nil
}
