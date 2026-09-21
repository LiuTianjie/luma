package cli

import (
	"context"
	"fmt"
	"io"
	"strings"
	"time"

	"github.com/LiuTianjie/luma/internal/registry"
	"golang.org/x/term"
)

func registryCommand(ctx context.Context, args []string, o options, streams IO) error {
	if len(args) < 2 {
		return fmt.Errorf("usage: registry list|login|remove")
	}
	command := args[1]
	if command != "list" && command != "login" && command != "remove" {
		return fmt.Errorf("registry %s has not been migrated", command)
	}
	if command == "list" && len(args) != 2 || command != "list" && len(args) != 3 {
		return fmt.Errorf("invalid registry command arguments")
	}
	change := registry.CredentialChange{Username: o.registryUsername}
	if command != "list" {
		change.Host = args[2]
		var err error
		// Validate identity before reading a password or opening a connection.
		if _, err = registry.ValidateCredential(change, true); err != nil {
			return err
		}
		if command == "login" {
			if strings.TrimSpace(change.Username) == "" {
				return fmt.Errorf("registry login requires --username")
			}
			if o.registryPasswordStdin {
				raw, err := io.ReadAll(io.LimitReader(streams.In, (1<<20)+2))
				if err != nil {
					return err
				}
				if len(raw) > (1<<20)+1 {
					return fmt.Errorf("registry password exceeds 1 MiB")
				}
				change.Password = strings.TrimSpace(string(raw))
			} else {
				file, ok := streams.In.(interface{ Fd() uintptr })
				if !ok || !term.IsTerminal(int(file.Fd())) {
					return fmt.Errorf("registry login requires --password-stdin when no terminal is available")
				}
				change.Password, err = promptSecret(ctx, streams, int(file.Fd()), change.Host+" password/token")
				if err != nil {
					return err
				}
			}
		}
		change, err = registry.ValidateCredential(change, command == "remove")
		if err != nil {
			return err
		}
	}
	client, err := clientFor(o, streams)
	if err != nil {
		return err
	}
	defer client.Close()
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	if command == "list" {
		items, err := client.RegistryCredentials(ctx)
		if err != nil {
			return err
		}
		if o.format != "text" {
			return success(streams, o, map[string]any{"registries": items})
		}
		if len(items) == 0 {
			_, err := fmt.Fprintln(streams.Out, "No registry credentials configured")
			return err
		}
		for _, item := range items {
			if _, err := fmt.Fprintf(streams.Out, "%s\t%s\n", item.Host, item.Username); err != nil {
				return err
			}
		}
		return nil
	}
	result, err := client.ChangeRegistryCredential(ctx, change, command == "remove")
	if err != nil {
		return err
	}
	if o.format != "text" {
		return success(streams, o, result)
	}
	verb := "saved"
	if command == "remove" {
		verb = "removed"
		if result.Removed != nil && !*result.Removed {
			verb = "not configured"
		}
	}
	_, err = fmt.Fprintf(streams.Out, "Registry credential %s: %s\n", verb, result.Host)
	return err
}
