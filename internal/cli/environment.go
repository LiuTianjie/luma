package cli

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"

	"github.com/LiuTianjie/luma/internal/adapters/envfile"
)

func needsConnection(args []string, o options) bool {
	switch args[0] {
	case "validate", "render", "context":
		return false
	case "deploy":
		return !o.dryRun
	case "compose":
		return len(args) > 1 && args[1] == "deploy"
	case "version":
		return !o.local
	default:
		return true
	}
}

func withEnvironment(o options, streams IO) (IO, error) {
	if o.noEnv {
		return streams, nil
	}
	data, err := readYAML(o.envFile)
	if errors.Is(err, os.ErrNotExist) {
		err = nil
	}
	if err != nil {
		return streams, err
	}
	values, err := envfile.Parse(data)
	if err != nil {
		return streams, fmt.Errorf("%s: %w", o.envFile, err)
	}
	getenv := streams.Getenv
	lookup := streams.LookupEnv
	if lookup == nil {
		lookup = func(key string) (string, bool) { value := getenv(key); return value, value != "" }
	}
	local := func(key string) (string, bool) {
		if value, ok := lookup(key); ok {
			return value, true
		}
		value, ok := values[key]
		return value, ok
	}
	configPath, _ := local("LUMA_USER_CONFIG")
	if configPath == "" {
		home := getenv("HOME")
		if home == "" {
			home, _ = os.UserHomeDir()
		}
		if home != "" {
			configPath = filepath.Join(home, ".luma.config.json")
		}
	}
	user := map[string]string{}
	if configPath != "" {
		data, err := readYAML(configPath)
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return streams, err
		}
		if err == nil {
			user, err = envfile.UserConfig(data)
			if err != nil {
				return streams, fmt.Errorf("%s: %w", configPath, err)
			}
		}
	}
	// dotenv preserves explicitly empty process variables; the legacy user
	// config may fill an empty value. Keep that distinction without os.Setenv.
	streams.LookupEnv = func(key string) (string, bool) {
		value, exists := local(key)
		if value == "" {
			if fallback, ok := user[key]; ok {
				return fallback, true
			}
		}
		return value, exists
	}
	streams.Getenv = func(key string) string { value, _ := streams.LookupEnv(key); return value }
	return streams, nil
}

func deploymentSecrets(path string, required []string, texts ...string) (map[string]string, error) {
	if path == "" {
		return nil, nil
	}
	data, err := readYAML(path)
	if err != nil {
		return nil, err
	}
	values, err := envfile.Parse(data)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	selected := envfile.Referenced(values, texts...)
	for _, name := range required {
		if value, ok := values[name]; ok {
			selected[name] = value
		}
	}
	return selected, nil
}
