package node

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"regexp"
	"strings"
)

var registryHost = regexp.MustCompile(`^[A-Za-z0-9._-]+(:[0-9]+)?$`)

// AddInsecureRegistry preserves every unrelated daemon setting. Malformed input
// is rejected rather than replacing an administrator's configuration with {}.
func AddInsecureRegistry(current []byte, host string) ([]byte, bool, error) {
	host = strings.TrimSpace(host)
	if !registryHost.MatchString(host) {
		return nil, false, fmt.Errorf("invalid registry host")
	}
	data := map[string]json.RawMessage{}
	if len(bytes.TrimSpace(current)) > 0 {
		decoder := json.NewDecoder(bytes.NewReader(current))
		start, err := decoder.Token()
		if err != nil || start != json.Delim('{') {
			return nil, false, fmt.Errorf("Docker daemon configuration must be a JSON object")
		}
		for decoder.More() {
			token, err := decoder.Token()
			key, ok := token.(string)
			if err != nil || !ok {
				return nil, false, fmt.Errorf("invalid Docker daemon configuration")
			}
			if _, exists := data[key]; exists {
				return nil, false, fmt.Errorf("duplicate Docker daemon configuration key")
			}
			var value json.RawMessage
			if decoder.Decode(&value) != nil {
				return nil, false, fmt.Errorf("invalid Docker daemon configuration")
			}
			data[key] = value
		}
		if end, err := decoder.Token(); err != nil || end != json.Delim('}') {
			return nil, false, fmt.Errorf("invalid Docker daemon configuration")
		}
		if decoder.Decode(new(any)) != io.EOF {
			return nil, false, fmt.Errorf("trailing Docker daemon configuration data")
		}
	}
	var registries []string
	if raw, ok := data["insecure-registries"]; ok {
		if json.Unmarshal(raw, &registries) != nil || string(raw) == "null" {
			return nil, false, fmt.Errorf("Docker insecure-registries must be a string list")
		}
	}
	for _, entry := range registries {
		if entry == host {
			return current, false, nil
		}
	}
	registries = append(registries, host)
	raw, _ := json.Marshal(registries)
	data["insecure-registries"] = raw
	updated, err := json.MarshalIndent(data, "", "  ")
	return append(updated, '\n'), true, err
}

// SystemdEnvironment quotes the whole assignment and escapes unit specifiers.
// Values must not gain extra Environment= or ExecStart= directives.
func SystemdEnvironment(name, value string) (string, error) {
	if name == "" || strings.ContainsAny(name, "=\x00\r\n\" ") || strings.ContainsAny(value, "\x00\r\n") {
		return "", fmt.Errorf("invalid daemon environment value")
	}
	replace := strings.NewReplacer(`\`, `\\`, `"`, `\"`, "%", "%%")
	return `Environment="` + name + "=" + replace.Replace(value) + "\"\n", nil
}
