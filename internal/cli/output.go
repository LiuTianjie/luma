package cli

import (
	"encoding/json"
	"fmt"
	"github.com/LiuTianjie/luma/internal/deployment"
)

func eventWriter(streams IO, o options) func(deployment.Event) error {
	return func(event deployment.Event) error {
		if event.Status == "warn" {
			_, err := fmt.Fprintln(streams.Err, "Warning:", event.Message)
			return err
		}
		if o.format == "ndjson" {
			var payload map[string]json.RawMessage
			if err := json.Unmarshal(event.Wire, &payload); err != nil {
				return err
			}
			payload["type"] = json.RawMessage(`"event"`)
			return json.NewEncoder(streams.Out).Encode(payload)
		}
		if o.format == "text" && event.Message != "" {
			_, err := fmt.Fprintf(streams.Err, "[%s] %s\n", event.Status, event.Message)
			return err
		}
		return nil

	}
}
