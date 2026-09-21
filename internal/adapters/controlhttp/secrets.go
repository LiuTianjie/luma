package controlhttp

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/LiuTianjie/luma/internal/credentials"
)

func (c *Client) SecretNames(ctx context.Context) ([]string, error) {
	raw, err := c.json(ctx, "GET", "/v1/secrets", nil)
	if err != nil {
		return nil, err
	}
	var result struct {
		Secrets []string `json:"secrets"`
	}
	if json.Unmarshal(raw, &result) != nil {
		return nil, fmt.Errorf("invalid secret names response")
	}
	if result.Secrets == nil {
		result.Secrets = []string{}
	}
	return result.Secrets, nil
}
func (c *Client) ChangeSecret(ctx context.Context, change credentials.SecretChange, remove bool) (credentials.SecretResult, error) {
	route := "/v1/secrets"
	if remove {
		route += "/remove"
	}
	raw, err := c.json(ctx, "POST", route, change)
	if err != nil {
		return credentials.SecretResult{}, err
	}
	var result credentials.SecretResult
	if json.Unmarshal(raw, &result) != nil || result.Name == "" {
		return result, fmt.Errorf("invalid secret update response")
	}
	return result, nil
}
