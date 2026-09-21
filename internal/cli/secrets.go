package cli

import (
	"context"
	"fmt"
	"io"
	"slices"
	"strings"
	"time"

	"github.com/LiuTianjie/luma/internal/adapters/envfile"
	"github.com/LiuTianjie/luma/internal/credentials"
	"golang.org/x/term"
)

func secretCommand(ctx context.Context, args []string, o options, streams IO) error {
	if len(args) < 2 {
		return fmt.Errorf("usage: secret list|set|import|remove")
	}
	command := args[1]
	if command == "list" && len(args) != 2 || command != "list" && len(args) != 3 {
		return fmt.Errorf("invalid secret command arguments")
	}
	if command != "list" && command != "set" && command != "import" && command != "remove" {
		return fmt.Errorf("unknown secret command")
	}
	scope, err := credentials.SecretScope(o.secretScope)
	if err != nil {
		return err
	}
	changes := []credentials.SecretChange{}
	if command == "import" {
		if scope == "" {
			return fmt.Errorf("secret import requires --scope")
		}
		data, err := readYAML(args[2])
		if err != nil {
			return err
		}
		values, err := envfile.Parse(data)
		if err != nil {
			return err
		}
		names := make([]string, 0, len(values))
		for name := range values {
			names = append(names, name)
		}
		slices.Sort(names)
		for _, name := range names {
			value := values[name]
			change, err := credentials.ValidateSecretChange(credentials.SecretChange{Name: name, Scope: scope, Value: &value}, false)
			if err != nil {
				return err
			}
			changes = append(changes, change)
		}
	} else if command != "list" {
		change := credentials.SecretChange{Name: args[2], Scope: scope}
		if command == "set" {
			if o.used["value"] && o.secretValueStdin {
				return fmt.Errorf("--value and --value-stdin cannot be used together")
			}
			value := o.secretValue
			if o.secretValueStdin {
				raw, err := io.ReadAll(io.LimitReader(streams.In, (1<<20)+2))
				if err != nil {
					return err
				}
				value = strings.TrimSuffix(string(raw), "\n")
			} else if !o.used["value"] {
				file, ok := streams.In.(interface{ Fd() uintptr })
				if !ok || !term.IsTerminal(int(file.Fd())) {
					return fmt.Errorf("secret set requires --value-stdin or --value when no terminal is available")
				}
				value, err = promptSecret(ctx, streams, int(file.Fd()), change.Name)
				if err != nil {
					return err
				}
			}
			change.Value = &value
		}
		change, err = credentials.ValidateSecretChange(change, command == "remove")
		if err != nil {
			return err
		}
		changes = append(changes, change)
	}
	client, err := clientFor(o, streams)
	if err != nil {
		return err
	}
	defer client.Close()
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	if command == "list" {
		names, err := client.SecretNames(ctx)
		if err != nil {
			return err
		}
		if o.format == "text" {
			if len(names) == 0 {
				_, err = fmt.Fprintln(streams.Out, "No deployment secrets configured")
				return err
			}
			for _, name := range names {
				if _, err := fmt.Fprintln(streams.Out, name); err != nil {
					return err
				}
			}
			return nil
		}
		return success(streams, o, map[string]any{"secrets": names})
	}
	var result credentials.SecretResult
	for i, change := range changes {
		result, err = client.ChangeSecret(ctx, change, command == "remove")
		if err != nil {
			if command == "import" {
				return fmt.Errorf("secret import stopped after %d saved entries: %w", i, err)
			}
			return err
		}
	}
	if command == "import" {
		return success(streams, o, map[string]any{"imported": len(changes), "scope": scope})
	}
	if o.format == "text" {
		label := result.Name
		if result.Scope != "" {
			label = result.Scope + "/" + label
		}
		verb := "saved"
		if command == "remove" {
			verb = "removed"
			if result.Removed != nil && !*result.Removed {
				verb = "not found"
			}
		}
		_, err := fmt.Fprintf(streams.Out, "Secret %s: %s\n", verb, label)
		return err
	}
	return success(streams, o, result)
}

type secretTerminal struct {
	io.Reader
	io.Writer
}

func promptSecret(ctx context.Context, streams IO, fd int, name string) (string, error) {
	state, err := term.MakeRaw(fd)
	if err != nil {
		return "", fmt.Errorf("could not open secret terminal")
	}
	defer term.Restore(fd, state)
	terminal := term.NewTerminal(secretTerminal{streams.In, streams.Err}, "")
	type reply struct {
		value string
		err   error
	}
	done := make(chan reply, 1)
	go func() { value, err := terminal.ReadPassword(name + ": "); done <- reply{value, err} }()
	select {
	case <-ctx.Done():
		return "", ctx.Err()
	case result := <-done:
		if result.err != nil {
			return "", fmt.Errorf("secret input canceled")
		}
		return result.value, nil
	}
}
