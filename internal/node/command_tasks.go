package node

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"time"
)

type TaskOwner struct{ Kind, Slug, OperationID string }
type CommandTaskRunner interface {
	Run(context.Context, string, TaskOwner, string, string, string, json.RawMessage) (json.RawMessage, error)
}
type CommandTaskRepository interface {
	CreateCommandTask(context.Context, string, TaskOwner, StoredTask) error
	CommandTask(context.Context, TaskOwner, string) (StoredTask, error)
	CancelCommandTask(context.Context, TaskOwner, string) error
}
type CommandTasks struct {
	Repository   CommandTaskRepository
	PollInterval time.Duration
}

// Run queues once and waits on that exact task. Credentials are attached only by
// the Agent lease adapter, never by this durable task creation path.
func (c CommandTasks) Run(ctx context.Context, token string, owner TaskOwner, nodeName, action, capability string, payload json.RawMessage) (json.RawMessage, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	var nonce [16]byte
	if _, err := rand.Read(nonce[:]); err != nil {
		return nil, err
	}
	task := StoredTask{ID: "agent-" + hex.EncodeToString(nonce[:]), NodeName: nodeName, Action: action, RequiredCapability: capability, Payload: payload, Status: "queued", CreatedAt: time.Now().Unix(), UpdatedAt: time.Now().Unix()}
	if err := c.Repository.CreateCommandTask(ctx, token, owner, task); err != nil {
		return nil, err
	}
	defer func() {
		if ctx.Err() != nil {
			cleanup, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
			defer cancel()
			_ = c.Repository.CancelCommandTask(cleanup, owner, task.ID)
		}
	}()
	delay := c.PollInterval
	if delay <= 0 {
		delay = 250 * time.Millisecond
	}
	for {
		current, err := c.Repository.CommandTask(ctx, owner, task.ID)
		if err != nil {
			return nil, err
		}
		if Terminal(current.Status) {
			if current.Status != "succeeded" {
				return nil, fmt.Errorf("node task %s %s; inspect its progress for details", task.ID, current.Status)
			}
			return current.Result, nil
		}
		timer := time.NewTimer(delay)
		select {
		case <-ctx.Done():
			timer.Stop()
			return nil, ctx.Err()
		case <-timer.C:
		}
	}
}
