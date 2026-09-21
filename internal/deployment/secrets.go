package deployment

import (
	"bytes"
	"encoding/json"
	"fmt"
	"slices"

	"github.com/LiuTianjie/luma/internal/credentials"
)

func deploymentSecretNames(r ControlRequest, services []Service) ([]string, []string) {
	names := credentials.SecretReferences(r.Manifest, r.ComposeContent)
	required := RequiredSecretNames(services)
	names = append(names, required...)
	slices.Sort(names)
	return slices.Compact(names), required
}

// RequiredSecretNames includes structured fields that are not ${...} references
// in the source text, including tunnel.tokenEnv and requirements.secrets.
func RequiredSecretNames(services []Service) []string {
	required := []string{}
	for _, service := range services {
		if service.Exposure == "cloudflare-tunnel" {
			required = append(required, service.TunnelTokenEnv)
		}
		required = append(required, service.Requirements.Secrets...)
		for _, value := range service.Environment {
			required = append(required, credentials.SecretReferences(value)...)
		}
	}
	slices.Sort(required)
	return slices.Compact(required)
}

// Only Nomad task Env values are substituted, matching the existing renderer.
// The preview/artifact plan and original manifests keep their placeholders.
func resolveJobSecrets(raw []byte, values map[string]string) (json.RawMessage, error) {
	var envelope map[string]any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if decoder.Decode(&envelope) != nil {
		return nil, fmt.Errorf("invalid compiled Nomad job")
	}
	job, ok := envelope["Job"].(map[string]any)
	if !ok {
		return nil, fmt.Errorf("invalid compiled Nomad job")
	}
	groups, _ := job["TaskGroups"].([]any)
	for _, group := range groups {
		g, _ := group.(map[string]any)
		tasks, _ := g["Tasks"].([]any)
		for _, task := range tasks {
			t, _ := task.(map[string]any)
			env, _ := t["Env"].(map[string]any)
			for name, raw := range env {
				value, ok := raw.(string)
				if !ok {
					return nil, fmt.Errorf("invalid compiled task environment")
				}
				resolved, err := credentials.SubstituteSecrets(value, values)
				if err != nil {
					return nil, err
				}
				env[name] = resolved
			}
		}
	}
	return json.Marshal(envelope)
}
