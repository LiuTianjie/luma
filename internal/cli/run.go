// Package cli is the composition boundary. Command handlers load inputs, call
// application services, and format output; deployment policy belongs elsewhere.
package cli

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"time"
)

type IO struct {
	In        io.Reader
	Out, Err  io.Writer
	Getenv    func(string) string
	LookupEnv func(string) (string, bool)
}

func Run(ctx context.Context, args []string, streams IO, version string) int {
	o, pos, err := parse(args, streams.Err)
	if errors.Is(err, flag.ErrHelp) || err == nil && len(pos) == 0 {
		_, _ = io.WriteString(streams.Out, help)
		return 0
	}
	if err == nil {
		err = run(ctx, pos, o, streams, version)
	}
	if err == nil {
		return 0
	}
	if o.format == "json" || o.format == "ndjson" {
		_ = json.NewEncoder(streams.Out).Encode(map[string]any{"ok": false, "error": map[string]string{"code": "luma_error", "message": err.Error()}})
	} else {
		fmt.Fprintln(streams.Err, "Error:", err)
	}
	return 1
}
func run(ctx context.Context, args []string, o options, streams IO, version string) error {
	if err := validateOptions(args, o); err != nil {
		return err
	}
	if needsConnection(args, o) {
		var err error
		streams, err = withEnvironment(o, streams)
		if err != nil {
			return err
		}
	}
	command := args[0]
	switch command {
	case "version":
		if len(args) != 1 {
			return fmt.Errorf("usage: version [--local]")
		}
		if o.local {
			return success(streams, o, map[string]string{"version": version, "implementation": "go"})
		}
		client, err := clientFor(o, streams)
		if err != nil {
			return err
		}
		defer client.Close()
		ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
		defer cancel()
		data, err := client.Health(ctx)
		if err != nil {
			return err
		}
		return success(streams, o, map[string]any{"version": version, "implementation": "go", "control": data})
	case "context":
		return contextCommand(args, o, streams)
	case "login":
		return login(ctx, args, o, streams)
	case "status":
		if len(args) != 1 {
			return fmt.Errorf("usage: status")
		}
		client, err := clientFor(o, streams)
		if err != nil {
			return err
		}
		defer client.Close()
		ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
		defer cancel()
		data, err := client.Status(ctx)
		if err != nil {
			return err
		}
		return success(streams, o, data)
	case "validate", "render", "deploy":
		return serviceCommand(ctx, command, args, o, streams)
	case "compose":
		return composeCommand(ctx, args, o, streams)
	case "secret":
		return secretCommand(ctx, args, o, streams)
	case "registry":
		return registryCommand(ctx, args, o, streams)
	case "import":
		return importCommand(ctx, args, o, streams)
	case "build":
		return buildCommand(ctx, args, o, streams)
	case "workflow":
		return workflowCommand(ctx, args, o, streams)
	default:
		return fmt.Errorf("command %q has not been migrated; use the Python luma CLI for this command", command)
	}
}
func success(streams IO, o options, value any) error {
	encoder := json.NewEncoder(streams.Out)
	encoder.SetEscapeHTML(false)
	if o.format == "text" {
		encoder.SetIndent("", "  ")
		return encoder.Encode(value)
	}
	payload := map[string]any{"ok": true, "result": value}
	if o.format == "ndjson" {
		payload["type"] = "result"
	}
	return encoder.Encode(payload)
}
