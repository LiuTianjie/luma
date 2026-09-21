package controlhttp

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"github.com/LiuTianjie/luma/internal/deployment"
)

func (c *Client) Submit(ctx context.Context, r deployment.SubmitRequest, emit func(deployment.Event) error) error {
	body := struct {
		Manifest         string             `json:"manifest"`
		ComposeContent   string             `json:"composeContent,omitempty"`
		SourceName       string             `json:"sourceName"`
		SkipDNS          bool               `json:"skipDns"`
		SkipOrchestrator bool               `json:"skipOrchestrator"`
		EnvSecrets       *map[string]string `json:"envSecrets,omitempty"`
	}{Manifest: r.Manifest, ComposeContent: r.ComposeContent, SourceName: r.SourceName, SkipDNS: r.SkipDNS}
	if r.EnvSecrets != nil {
		body.EnvSecrets = &r.EnvSecrets
	}
	path := "/v1/deployments/stream"
	if r.ComposeContent != "" {
		path = "/v1/compose-deployments/stream"
	}
	response, err := c.open(ctx, "POST", path, body)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	scanner := bufio.NewScanner(response.Body)
	scanner.Buffer(make([]byte, 64<<10), maxResponse)
	for scanner.Scan() {
		line := bytes.TrimSpace(scanner.Bytes())
		if len(line) == 0 {
			continue
		}
		if !object(line) {
			return fmt.Errorf("control API returned invalid stream JSON")
		}
		var wire struct {
			Status  string          `json:"status"`
			Message string          `json:"message"`
			Result  json.RawMessage `json:"result"`
		}
		if err := json.Unmarshal(line, &wire); err != nil {
			return fmt.Errorf("invalid control event: %w", err)
		}
		if wire.Status == "done" && !object(wire.Result) {
			return fmt.Errorf("control stream ended without a deploy result")
		}
		if err := emit(deployment.Event{Status: wire.Status, Message: wire.Message, Result: wire.Result, Wire: append([]byte(nil), line...)}); err != nil {
			return err
		}
	}
	// The peer may close cleanly as cancellation propagates, so EOF alone does
	// not establish that the request completed before its deadline.
	if err := ctx.Err(); err != nil {
		return fmt.Errorf("control stream interrupted; request was not replayed: %w", err)
	}
	if err := scanner.Err(); err != nil {
		return fmt.Errorf("control stream interrupted; request was not replayed: %w", err)
	}
	return nil
}
