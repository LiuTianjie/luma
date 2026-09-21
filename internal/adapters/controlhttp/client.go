// Package controlhttp implements the existing Control protocol. It never reads
// local credentials and never retries a deployment mutation.
package controlhttp

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

const maxResponse = 16 << 20

type Options struct {
	Endpoint, Token, ResolveIP string
	Insecure                   bool
}
type Client struct {
	endpoint, token string
	http            *http.Client
	transport       *http.Transport
}
type APIError struct {
	Status  int
	Message string
}

func (e *APIError) Error() string {
	return fmt.Sprintf("control API error %d: %s", e.Status, e.Message)
}

func New(options Options) (*Client, error) {
	u, err := url.Parse(options.Endpoint)
	if err != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return nil, fmt.Errorf("control endpoint must be an HTTPS URL without credentials, query or fragment")
	}
	if options.Token == "" {
		return nil, fmt.Errorf("management token is required")
	}
	if options.ResolveIP != "" && (!options.Insecure || net.ParseIP(options.ResolveIP) == nil) {
		return nil, fmt.Errorf("--resolve-ip requires --insecure and a valid IP address")
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12, InsecureSkipVerify: options.Insecure} // Explicit compatibility flag only.
	transport.ResponseHeaderTimeout = 30 * time.Second
	if options.ResolveIP != "" {
		transport.Proxy = nil
		port := u.Port()
		if port == "" {
			port = "443"
		}
		dialer := &net.Dialer{Timeout: 30 * time.Second, KeepAlive: 30 * time.Second}
		transport.DialContext = func(ctx context.Context, network, address string) (net.Conn, error) {
			return dialer.DialContext(ctx, network, net.JoinHostPort(options.ResolveIP, port))
		}
	}
	client := &http.Client{Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	return &Client{endpoint: strings.TrimRight(options.Endpoint, "/"), token: options.Token, http: client, transport: transport}, nil
}
func (c *Client) Close() { c.transport.CloseIdleConnections() }

func (c *Client) open(ctx context.Context, method, path string, body any) (*http.Response, error) {
	var input io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		input = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.endpoint+path, input)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	response, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("control request failed (%s %s); request was not replayed: %w", method, path, err)
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		defer response.Body.Close()
		data, _ := io.ReadAll(io.LimitReader(response.Body, 64<<10))
		var details struct {
			Message string `json:"message"`
			Error   string `json:"error"`
		}
		_ = json.Unmarshal(data, &details)
		message := details.Message
		if message == "" {
			message = details.Error
		}
		if message == "" {
			message = http.StatusText(response.StatusCode)
		}
		return nil, &APIError{Status: response.StatusCode, Message: message}
	}
	return response, nil
}
func (c *Client) json(ctx context.Context, method, path string, body any) (json.RawMessage, error) {
	response, err := c.open(ctx, method, path, body)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	data, err := io.ReadAll(io.LimitReader(response.Body, maxResponse+1))
	if err != nil {
		return nil, err
	}
	if len(data) > maxResponse {
		return nil, fmt.Errorf("control response exceeds limit")
	}
	if !object(data) {
		return nil, fmt.Errorf("control API returned invalid JSON object")
	}
	return data, nil
}
func object(data []byte) bool {
	b := bytes.TrimSpace(data)
	return len(b) > 0 && b[0] == '{' && json.Valid(b)
}
func (c *Client) Status(ctx context.Context) (json.RawMessage, error) {
	return c.json(ctx, "GET", "/v1/status", nil)
}
func (c *Client) Health(ctx context.Context) (json.RawMessage, error) {
	return c.json(ctx, "GET", "/v1/health", nil)
}
func (c *Client) VerifyLogin(ctx context.Context) (string, error) {
	data, err := c.json(ctx, "POST", "/v1/auth/login/verify", struct{}{})
	if err != nil {
		return "", err
	}
	var result struct {
		ClusterID string `json:"clusterId"`
	}
	if err := json.Unmarshal(data, &result); err != nil {
		return "", err
	}
	if result.ClusterID == "" {
		return "", fmt.Errorf("control API did not return clusterId")
	}
	return result.ClusterID, nil
}
