// Package manifest translates the existing YAML format into typed domain inputs.
// Dynamic YAML values stay at this boundary; no parser objects escape it.
package manifest

import (
	"bytes"
	"fmt"
	"io"
	"sort"
	"strconv"
	"strings"

	"go.yaml.in/yaml/v3"
)

const MaxBytes = 4 << 20

func mapping(data []byte) (map[string]any, error) {
	if len(data) > MaxBytes {
		return nil, fmt.Errorf("YAML exceeds %d bytes", MaxBytes)
	}
	d := yaml.NewDecoder(bytes.NewReader(data))
	var node yaml.Node
	if err := d.Decode(&node); err != nil {
		return nil, fmt.Errorf("invalid YAML: %w", err)
	}
	var extra yaml.Node
	if err := d.Decode(&extra); err != io.EOF {
		return nil, fmt.Errorf("expected one YAML document")
	}
	if len(node.Content) != 1 || node.Content[0].Kind != yaml.MappingNode {
		return nil, fmt.Errorf("manifest must contain a YAML mapping")
	}
	// PyYAML's safe loader uses YAML 1.1 booleans even in untyped env values.
	// Keep that interpretation during migration; quoted strings remain strings.
	var normalize func(*yaml.Node)
	normalize = func(n *yaml.Node) {
		if n.Kind == yaml.ScalarNode && n.Style == 0 && n.Tag == "!!str" {
			switch strings.ToLower(n.Value) {
			case "yes", "on":
				n.Tag, n.Value = "!!bool", "true"
			case "no", "off":
				n.Tag, n.Value = "!!bool", "false"
			}
		}
		for _, child := range n.Content {
			normalize(child)
		}
	}
	normalize(&node)
	var raw map[string]any
	if err := node.Decode(&raw); err != nil {
		return nil, fmt.Errorf("invalid YAML: %w", err)
	}
	return raw, nil
}

type reader struct{ err error }

func (r *reader) fail(format string, args ...any) {
	if r.err == nil {
		r.err = fmt.Errorf(format, args...)
	}
}
func (r *reader) object(value any, field string) map[string]any {
	if value == nil {
		return map[string]any{}
	}
	v, ok := value.(map[string]any)
	if !ok {
		r.fail("%s must be a mapping", field)
		return map[string]any{}
	}
	return v
}
func (r *reader) text(value any, field, fallback string) string {
	if value == nil {
		return fallback
	}
	v, ok := value.(string)
	if !ok {
		r.fail("%s must be a string", field)
		return ""
	}
	return strings.TrimSpace(v)
}
func (r *reader) integer(value any, field string, fallback int) int {
	if value == nil {
		return fallback
	}
	var n int
	switch v := value.(type) {
	case int:
		n = v
	case string:
		var err error
		n, err = strconv.Atoi(v)
		if err != nil {
			r.fail("%s must be a positive integer", field)
		}
	default:
		r.fail("%s must be a positive integer", field)
	}
	if n < 1 {
		r.fail("%s must be a positive integer", field)
	}
	return n
}
func (r *reader) boolean(value any, field string) bool {
	if value == nil {
		return false
	}
	v, ok := value.(bool)
	if !ok {
		r.fail("%s must be a boolean", field)
	}
	return v
}
func (r *reader) list(value any, field string) []string {
	if value == nil {
		return nil
	}
	v, ok := value.([]any)
	if !ok {
		r.fail("%s must be a list of strings", field)
		return nil
	}
	result := make([]string, 0, len(v))
	for _, item := range v {
		text, ok := item.(string)
		if !ok {
			r.fail("%s must be a list of strings", field)
			return nil
		}
		result = append(result, text)
	}
	return result
}
func (r *reader) fields(raw map[string]any, field, allowed string) {
	known := strings.Fields(allowed)
	unknown := []string{}
	for key := range raw {
		found := false
		for _, name := range known {
			if name == key {
				found = true
				break
			}
		}
		if !found {
			unknown = append(unknown, key)
		}
	}
	if len(unknown) > 0 {
		sort.Strings(unknown)
		r.fail("unsupported %s field(s): %s", field, strings.Join(unknown, ", "))
	}
}
func scalar(value any) (string, error) {
	switch v := value.(type) {
	case nil:
		return "None", nil
	case string:
		return v, nil
	case bool:
		if v {
			return "True", nil
		}
		return "False", nil
	case int:
		return strconv.Itoa(v), nil
	case uint64:
		return strconv.FormatUint(v, 10), nil
	case float64:
		text := strconv.FormatFloat(v, 'g', -1, 64)
		if !strings.ContainsAny(text, ".eE") {
			text += ".0"
		}
		return text, nil
	default:
		return "", fmt.Errorf("expected a scalar value")
	}
}
