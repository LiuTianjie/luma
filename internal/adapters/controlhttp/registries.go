package controlhttp

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/LiuTianjie/luma/internal/registry"
)

func (c *Client) RegistryCredentials(ctx context.Context) ([]registry.CredentialInfo, error) {
	raw, err := c.json(ctx, "GET", "/v1/registries", nil)
	if err != nil {
		return nil, err
	}
	var result struct {
		Registries []registry.CredentialInfo `json:"registries"`
	}
	if json.Unmarshal(raw, &result) != nil {
		return nil, fmt.Errorf("invalid registry list response")
	}
	if result.Registries == nil {
		result.Registries = []registry.CredentialInfo{}
	}
	return result.Registries, nil
}
func (c *Client) ChangeRegistryCredential(ctx context.Context, change registry.CredentialChange, remove bool) (registry.CredentialResult, error) {
	route := "/v1/registries"
	if remove {
		route += "/remove"
	}
	raw, err := c.json(ctx, "POST", route, change)
	if err != nil {
		return registry.CredentialResult{}, err
	}
	var result registry.CredentialResult
	if json.Unmarshal(raw, &result) != nil || result.Host == "" {
		return result, fmt.Errorf("invalid registry update response")
	}
	return result, nil
}
