// Package cloudflare owns DNS API transport. Planning and target selection live
// in deployment; secrets are read per operation and never copied into records.
package cloudflare

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/LiuTianjie/luma/internal/deployment"
)

const API = "https://api.cloudflare.com/client/v4"

type Secrets interface {
	GlobalSecrets(context.Context, []string) (map[string]string, error)
}
type Client struct {
	base        string
	http        *http.Client
	secrets     Secrets
	environment func(string) string
}

func New(endpoint string, secrets Secrets, environment func(string) string) (*Client, error) {
	u, err := url.Parse(endpoint)
	if err != nil || (u.Scheme != "https" && u.Scheme != "http") || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return nil, fmt.Errorf("invalid Cloudflare API endpoint")
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	return &Client{base: strings.TrimRight(endpoint, "/"), http: &http.Client{Transport: transport, Timeout: 20 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}, secrets: secrets, environment: environment}, nil
}
func (c *Client) Close() { c.http.CloseIdleConnections() }
func (c *Client) request(ctx context.Context, token, method, path string, body any) (json.RawMessage, error) {
	var input io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		input = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.base+path, input)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	response, err := c.http.Do(req)
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		return nil, fmt.Errorf("%w: Cloudflare transport failed", deployment.ErrRuntime)
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, fmt.Errorf("%w: Cloudflare HTTP %d", deployment.ErrRuntime, response.StatusCode)
	}
	raw, err := io.ReadAll(io.LimitReader(response.Body, (4<<20)+1))
	if err != nil || len(raw) > 4<<20 {
		return nil, fmt.Errorf("%w: Cloudflare response interrupted or oversized", deployment.ErrRuntime)
	}
	var envelope struct {
		Success bool
		Result  json.RawMessage
	}
	if json.Unmarshal(raw, &envelope) != nil || !envelope.Success {
		return nil, fmt.Errorf("%w: Cloudflare rejected DNS operation", deployment.ErrRuntime)
	}
	return envelope.Result, nil
}
func (c *Client) Sync(ctx context.Context, config deployment.DNSConfig, records []deployment.DNSRecord) ([]string, error) {
	messages := []string{}
	if len(records) == 0 {
		return messages, nil
	}
	names := []string{config.TokenEnv, config.ZoneIDEnv}
	values := map[string]string{}
	if c.environment != nil {
		for _, name := range names {
			values[name] = c.environment(name)
		}
	}
	if c.secrets != nil {
		stored, err := c.secrets.GlobalSecrets(ctx, names)
		if err != nil {
			return nil, err
		}
		for k, v := range stored {
			values[k] = v
		}
	}
	token, zone := values[config.TokenEnv], values[config.ZoneIDEnv]
	if zone == "" {
		zone = config.ZoneID
	}
	if token == "" || zone == "" {
		return nil, fmt.Errorf("%w: Cloudflare API token and zone ID are required", deployment.ErrInput)
	}
	for _, record := range records {
		path := "/zones/" + url.PathEscape(zone) + "/dns_records"
		result, err := c.request(ctx, token, "GET", path+"?"+url.Values{"type": {record.Type}, "name": {record.Name}}.Encode(), nil)
		if err != nil {
			return nil, err
		}
		var existing []struct {
			ID string `json:"id"`
		}
		if json.Unmarshal(result, &existing) != nil {
			return nil, fmt.Errorf("%w: invalid Cloudflare DNS list", deployment.ErrRuntime)
		}
		method, verb := "POST", "created"
		if len(existing) > 0 {
			if existing[0].ID == "" {
				return nil, fmt.Errorf("%w: missing DNS record ID", deployment.ErrRuntime)
			}
			method, verb = "PUT", "updated"
			path += "/" + url.PathEscape(existing[0].ID)
		}
		if _, err := c.request(ctx, token, method, path, record); err != nil {
			return nil, err
		}
		messages = append(messages, "DNS "+verb+": "+record.Name+" -> "+record.Content)
	}
	return messages, nil
}
