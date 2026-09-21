package controlhttp

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"strconv"
	"time"

	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/deployment"
)

func (c *Client) ListBuilds(ctx context.Context, q url.Values) (json.RawMessage, error) {
	return c.json(ctx, "GET", "/v1/builds?"+q.Encode(), nil)
}
func (c *Client) GetBuild(ctx context.Context, id string, q url.Values) (json.RawMessage, error) {
	return c.json(ctx, "GET", "/v1/builds/"+url.PathEscape(id)+"?"+q.Encode(), nil)
}
func (c *Client) CancelBuild(ctx context.Context, id string) (json.RawMessage, error) {
	return c.json(ctx, "POST", "/v1/builds/"+url.PathEscape(id)+"/cancel", map[string]any{})
}

// Import accepts one durable attempt then observes that ID. Neither poll errors
// nor cancellation of the local wait can resubmit a repository build.
func (c *Client) Import(ctx context.Context, r builddomain.ControlRequest, emit func(deployment.Event) error) ([]byte, error) {
	r.Queue = true
	raw, err := c.json(ctx, "POST", "/v1/builds", r)
	if err != nil {
		return nil, err
	}
	return c.waitBuild(ctx, raw, emit)
}

func (c *Client) RetryBuild(ctx context.Context, id string, r builddomain.RetryRequest, emit func(deployment.Event) error) ([]byte, error) {
	r.Queue = true
	raw, err := c.json(ctx, "POST", "/v1/builds/"+url.PathEscape(id)+"/retry", r)
	if err != nil {
		return nil, err
	}
	return c.waitBuild(ctx, raw, emit)
}

func (c *Client) waitBuild(ctx context.Context, raw []byte, emit func(deployment.Event) error) ([]byte, error) {
	var accepted struct {
		Queued bool   `json:"queued"`
		ID     string `json:"buildRunId"`
	}
	if json.Unmarshal(raw, &accepted) != nil || !accepted.Queued || accepted.ID == "" {
		return nil, fmt.Errorf("Control did not return a queued build identity; inspect build history before retrying")
	}
	send := func(status, message string, result []byte) error {
		if emit == nil {
			return nil
		}
		wire, _ := json.Marshal(map[string]any{"status": status, "message": message, "buildRunId": accepted.ID, "result": json.RawMessage(result)})
		return emit(deployment.Event{Status: status, Message: message, Result: result, Wire: wire})
	}
	if err := send("start", "Build accepted: "+accepted.ID, nil); err != nil {
		return nil, err
	}
	after := int64(-1)
	previous := ""
	for {
		data, err := c.GetBuild(ctx, accepted.ID, url.Values{"after": {strconv.FormatInt(after, 10)}, "limit": {"100"}})
		if err != nil {
			return nil, fmt.Errorf("build %s accepted; observation stopped: %w", accepted.ID, err)
		}
		var reply struct {
			Run struct {
				ID, Status, Message string
				Result              json.RawMessage
				Events              []builddomain.Event
			} `json:"run"`
			Page struct {
				HasMore     bool   `json:"hasMore"`
				ResumeAfter *int64 `json:"resumeAfter"`
			} `json:"eventsPage"`
		}
		if json.Unmarshal(data, &reply) != nil || reply.Run.ID != accepted.ID || reply.Page.ResumeAfter == nil || *reply.Page.ResumeAfter < after {
			return nil, fmt.Errorf("invalid status for build %s", accepted.ID)
		}
		for _, event := range reply.Run.Events {
			if err := send(event.Status, event.Message, nil); err != nil {
				return nil, err
			}
		}
		after = *reply.Page.ResumeAfter
		if reply.Page.HasMore {
			continue
		}
		if builddomain.Terminal(reply.Run.Status) {
			if reply.Run.Status != "succeeded" {
				return nil, fmt.Errorf("build %s %s: %s", accepted.ID, reply.Run.Status, reply.Run.Message)
			}
			if !object(reply.Run.Result) {
				return nil, fmt.Errorf("build %s has no deployment result", accepted.ID)
			}
			if err := send("done", "Build and deployment completed", reply.Run.Result); err != nil {
				return nil, err
			}
			return reply.Run.Result, nil
		}
		if previous != reply.Run.Status {
			if err := send("waiting", reply.Run.Status+": "+reply.Run.Message, nil); err != nil {
				return nil, err
			}
			previous = reply.Run.Status
		}
		timer := time.NewTimer(500 * time.Millisecond)
		select {
		case <-ctx.Done():
			timer.Stop()
			return nil, fmt.Errorf("build %s accepted and may still be running: %w", accepted.ID, ctx.Err())
		case <-timer.C:
		}
	}
}

func (c *Client) BuildConfig(ctx context.Context, change builddomain.ConfigChange, write bool) (json.RawMessage, error) {
	if write {
		return c.json(ctx, "POST", "/v1/builds/config", change)
	}
	return c.json(ctx, "GET", "/v1/builds/config", nil)
}

func (c *Client) PrepareLocalBuild(ctx context.Context, r builddomain.LocalPrepareRequest) (builddomain.LocalReservation, error) {
	var result builddomain.LocalReservation
	raw, err := c.json(ctx, "POST", "/v1/builds/local/prepare", r)
	if err != nil {
		return result, err
	}
	if json.Unmarshal(raw, &result) != nil || result.Run.ID == "" || result.Run.Mode != "local" || result.Upload.Tag != builddomain.LocalBuildTag(result.Run.ID) || result.Upload.RegistryHost == "" || result.Upload.Repository == "" {
		return result, fmt.Errorf("invalid local build reservation")
	}
	return result, nil
}
func (c *Client) CompleteLocalBuild(ctx context.Context, id string, r builddomain.LocalCompleteRequest, emit func(deployment.Event) error) ([]byte, error) {
	r.Queue = true
	raw, err := c.json(ctx, "POST", "/v1/builds/local/"+url.PathEscape(id)+"/complete", r)
	if err != nil {
		return nil, err
	}
	return c.waitBuild(ctx, raw, emit)
}
func (c *Client) FailLocalBuild(ctx context.Context, id, message string) error {
	_, err := c.json(ctx, "POST", "/v1/builds/local/"+url.PathEscape(id)+"/fail", map[string]string{"message": message})
	return err
}
