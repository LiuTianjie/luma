package node

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/LiuTianjie/luma/internal/observability"
)

var (
	ErrControlInput      = errors.New("invalid agent request")
	ErrTaskNotFound      = errors.New("agent task not found")
	ErrTaskState         = errors.New("agent task state conflict")
	ErrActionNotMigrated = errors.New("agent action or parent workflow has not been migrated")
)
var actionName = regexp.MustCompile(`^[a-z][a-z0-9-]{0,79}$`)

// AgentReport distinguishes omitted capabilities/actions from an explicit empty
// list. Identity-only completion must never clear capability negotiation.
type AgentReport struct {
	Identity
	OS               string                         `json:"os"`
	Arch             string                         `json:"arch"`
	Version          string                         `json:"version"`
	Capabilities     *[]string                      `json:"capabilities"`
	SupportedActions *[]string                      `json:"supportedActions"`
	ActiveTaskID     *string                        `json:"activeTaskId"`
	Metrics          *Metrics                       `json:"metrics"`
	ContainerStats   *[]observability.ContainerStat `json:"containerStats"`
	WaitSeconds      int                            `json:"waitSeconds"`
}

type AgentSnapshot struct {
	Name, OS, Arch, Version, Status, ActiveTaskID                                 string
	Capabilities                                                                  []string
	SupportedActions                                                              *[]string
	JoinVerified                                                                  bool
	LastSeen, ActiveTaskObservedAt, MetricsCollectedAt, ContainerStatsCollectedAt int64
	Metrics                                                                       *Metrics
	ContainerStats                                                                *[]observability.ContainerStat
}

type StoredTask struct {
	ID                      string          `json:"id"`
	NodeName                string          `json:"nodeName"`
	Action                  string          `json:"action"`
	Payload                 json.RawMessage `json:"payload"`
	Status                  string          `json:"status"`
	RequiredCapability      string          `json:"requiredCapability,omitempty"`
	RequiredCapabilitiesAny []string        `json:"requiredCapabilitiesAny,omitempty"`
	BuilderTaskID           string          `json:"builderTaskId,omitempty"`
	GoBuild                 bool            `json:"goBuild,omitempty"`
	BuildRunID              string          `json:"buildRunId,omitempty"`
	CreatedAt               int64           `json:"createdAt"`
	UpdatedAt               int64           `json:"updatedAt"`
	LeasedAt                int64           `json:"leasedAt,omitempty"`
	CompletedAt             int64           `json:"completedAt,omitempty"`
	CancelRequestedAt       int64           `json:"cancelRequestedAt,omitempty"`
	Message                 string          `json:"message,omitempty"`
	Result                  json.RawMessage `json:"result,omitempty"`
	ProgressOffset          int64           `json:"progressOffset"`
}

type LeasedTask struct {
	ID      string          `json:"id"`
	Action  string          `json:"action"`
	Payload json.RawMessage `json:"payload"`
}
type LeaseReply struct {
	Task *LeasedTask `json:"task"`
}
type HeartbeatReply struct {
	Name            string `json:"nodeName"`
	Status          string `json:"status"`
	ActiveTaskID    string `json:"activeTaskId"`
	CancelRequested bool   `json:"cancelRequested"`
}
type Completion struct {
	AgentReport
	TaskID  string          `json:"taskId"`
	Status  string          `json:"status"`
	Message string          `json:"message"`
	Result  json.RawMessage `json:"result"`
}
type CompletionReply struct {
	TaskID string `json:"taskId"`
	Status string `json:"status"`
}
type ProgressRequest struct {
	Identity
	TaskID string  `json:"taskId"`
	Events []Event `json:"events"`
}
type ProgressReply struct {
	TaskID string `json:"taskId"`
	Events int    `json:"events"`
}

// AgentTransaction exposes only the authenticated node and its owned tasks.
// Pages are ordered by ID, at most 100 records; saves preserve unknown legacy
// fields and unrelated event streams in the adapter.
type AgentTransaction interface {
	Agent() AgentSnapshot
	SaveAgent(AgentSnapshot) error
	Tasks(status, after string) ([]StoredTask, error)
	Task(id string) (StoredTask, error)
	SaveTask(StoredTask) error
	LeasePayload(StoredTask) (json.RawMessage, error)
	AppendProgress(*StoredTask, []Event, int) error
}
type AgentRepository interface {
	AgentTransaction(context.Context, string, Identity, func(AgentTransaction) error) error
}

type Coordinator struct {
	Repository   AgentRepository
	Version      string
	Now          func() time.Time
	HandoffGrace time.Duration
}

func (c Coordinator) now() int64 {
	if c.Now != nil {
		return c.Now().Unix()
	}
	return time.Now().Unix()
}
func (c Coordinator) grace() int64 {
	if c.HandoffGrace > 0 {
		return max(1, int64(c.HandoffGrace/time.Second))
	}
	return 60
}

// ControlActionMigrated is the Control ownership gate, independent of a node's
// advertised executors. Linked builder workflows require their own atomic state
// transitions before they may enter this lane. NFS is never a target capability.
func ControlActionMigrated(action string) bool {
	switch action {
	case "prepare-managed-volume-path", "remove-managed-volume-path", "remove-docker-volume",
		"resolve-docker-image", "diagnose-docker-pull", "mirror-control-image", "mirror-system-image", "cache-runtime-image",
		"configure-insecure-registry", "configure-docker-egress-proxy", "repair-nomad-cni-hostports":
		return true
	}
	return false
}
func taskMigrated(task StoredTask) bool {
	if task.Action == "build-image" {
		return task.GoBuild && task.BuildRunID != "" && task.BuilderTaskID == ""
	}
	return ControlActionMigrated(task.Action) && task.BuilderTaskID == "" && task.BuildRunID == ""
}
func reportValid(p AgentReport) error {
	if strings.TrimSpace(p.Name) == "" || len(p.Name) > 255 || len(p.ID) > 255 {
		return fmt.Errorf("%w: nodeName is required and identity must be bounded", ErrControlInput)
	}
	if p.SupportedActions != nil {
		if len(*p.SupportedActions) > 100 {
			return fmt.Errorf("%w: supportedActions must contain at most 100 actions", ErrControlInput)
		}
		for _, value := range *p.SupportedActions {
			if !actionName.MatchString(value) {
				return fmt.Errorf("%w: invalid supportedActions", ErrControlInput)
			}
		}
	}
	if p.Capabilities != nil {
		if len(*p.Capabilities) > 100 {
			return fmt.Errorf("%w: too many capabilities", ErrControlInput)
		}
		for _, v := range *p.Capabilities {
			if len(v) > 100 {
				return fmt.Errorf("%w: capability too long", ErrControlInput)
			}
		}
	}
	if len(p.OS) > 80 || len(p.Arch) > 80 || len(p.Version) > 100 {
		return fmt.Errorf("%w: platform fields are too long", ErrControlInput)
	}
	if p.ActiveTaskID != nil && len(*p.ActiveTaskID) > 255 {
		return fmt.Errorf("%w: activeTaskId too long", ErrControlInput)
	}
	if p.ContainerStats != nil && len(*p.ContainerStats) > 200 {
		return fmt.Errorf("%w: too many container stats", ErrControlInput)
	}
	return nil
}
func (c Coordinator) presence(tx AgentTransaction, p AgentReport, now int64) (AgentSnapshot, error) {
	a := tx.Agent()
	a.Status = "online"
	a.JoinVerified = true
	a.LastSeen = now
	if p.OS != "" {
		a.OS = p.OS
	}
	if p.Arch != "" {
		a.Arch = p.Arch
	}
	if p.Version != "" {
		a.Version = p.Version
	} else if a.Version == "" {
		a.Version = c.Version
	}
	if p.Capabilities != nil {
		a.Capabilities = slices.Clone(*p.Capabilities)
		if p.SupportedActions == nil {
			a.SupportedActions = nil
		}
	}
	if p.SupportedActions != nil {
		values := slices.Clone(*p.SupportedActions)
		slices.Sort(values)
		values = slices.Compact(values)
		if values == nil {
			values = []string{}
		}
		a.SupportedActions = &values
	}
	if p.ActiveTaskID != nil {
		a.ActiveTaskID = strings.TrimSpace(*p.ActiveTaskID)
		a.ActiveTaskObservedAt = now
	}
	if p.Metrics != nil {
		a.Metrics = p.Metrics
		a.MetricsCollectedAt = now
	}
	if p.ContainerStats != nil {
		a.ContainerStats = p.ContainerStats
		a.ContainerStatsCollectedAt = now
	}
	return a, tx.SaveAgent(a)
}
func visitTasks(tx AgentTransaction, status string, visit func(*StoredTask) (bool, error)) error {
	after := ""
	for {
		page, err := tx.Tasks(status, after)
		if err != nil {
			return err
		}
		if len(page) == 0 {
			return nil
		}
		for _, task := range page {
			after = task.ID
			stop, err := visit(&task)
			if err != nil {
				return err
			}
			if stop {
				return nil
			}
		}
	}
}
func (c Coordinator) reconcile(tx AgentTransaction, active string, now int64) error {
	return visitTasks(tx, "running", func(task *StoredTask) (bool, error) {
		if !taskMigrated(*task) || task.ID == active || task.LeasedAt > 0 && now-task.LeasedAt < c.grace() {
			return false, nil
		}
		task.Status = "failed"
		task.Message = "node agent restarted before task completion"
		if task.CancelRequestedAt > 0 {
			task.Status = "canceled"
			task.Message = "agent task canceled"
		}
		task.Result = json.RawMessage(`{}`)
		task.CompletedAt = now
		task.UpdatedAt = now
		return false, tx.SaveTask(*task)
	})
}
func eligible(a AgentSnapshot, t StoredTask) bool {
	if !taskMigrated(t) || t.Status != "queued" {
		return false
	}
	if a.SupportedActions != nil && !slices.Contains(*a.SupportedActions, t.Action) {
		return false
	}
	if len(t.RequiredCapabilitiesAny) > 0 {
		for _, capability := range t.RequiredCapabilitiesAny {
			if slices.Contains(a.Capabilities, capability) {
				return true
			}
		}
		return false
	}
	return t.RequiredCapability == "" || slices.Contains(a.Capabilities, t.RequiredCapability)
}

func (c Coordinator) Lease(ctx context.Context, token string, p AgentReport) (LeaseReply, error) {
	var reply LeaseReply
	if err := reportValid(p); err != nil {
		return reply, err
	}
	wait := time.Duration(min(30, max(0, p.WaitSeconds))) * time.Second
	deadline := time.Now().Add(wait)
	first := true
	for {
		err := c.Repository.AgentTransaction(ctx, token, p.Identity, func(tx AgentTransaction) error {
			now := c.now()
			a := tx.Agent()
			if first {
				var err error
				a, err = c.presence(tx, p, now)
				if err != nil {
					return err
				}
				if p.ActiveTaskID != nil {
					if err := c.reconcile(tx, strings.TrimSpace(*p.ActiveTaskID), now); err != nil {
						return err
					}
				}
			}
			// A serial node cannot receive another task while any earlier task is
			// still running, including a response in the lease handoff window.
			busy, err := tx.Tasks("running", "")
			if err != nil {
				return err
			}
			if len(busy) > 0 {
				return nil
			}
			return visitTasks(tx, "queued", func(task *StoredTask) (bool, error) {
				if !eligible(a, *task) {
					return false, nil
				}
				if task.CancelRequestedAt > 0 {
					task.Status = "canceled"
					task.Message = "agent task canceled before lease"
					task.Result = json.RawMessage(`{}`)
					task.CompletedAt = now
					task.UpdatedAt = now
					return false, tx.SaveTask(*task)
				}
				payload, err := tx.LeasePayload(*task)
				if err != nil {
					return false, err
				}
				task.Status = "running"
				task.LeasedAt = now
				task.UpdatedAt = now
				if err := tx.SaveTask(*task); err != nil {
					return false, err
				}
				reply.Task = &LeasedTask{ID: task.ID, Action: task.Action, Payload: payload}
				return true, nil
			})
		})
		if err != nil || reply.Task != nil || !time.Now().Before(deadline) {
			return reply, err
		}
		first = false
		timer := time.NewTimer(min(250*time.Millisecond, time.Until(deadline)))
		select {
		case <-ctx.Done():
			timer.Stop()
			return reply, ctx.Err()
		case <-timer.C:
		}
	}
}
func (c Coordinator) Heartbeat(ctx context.Context, token string, p AgentReport) (HeartbeatReply, error) {
	var reply HeartbeatReply
	if err := reportValid(p); err != nil {
		return reply, err
	}
	err := c.Repository.AgentTransaction(ctx, token, p.Identity, func(tx AgentTransaction) error {
		now := c.now()
		a, err := c.presence(tx, p, now)
		if err != nil {
			return err
		}
		active := a.ActiveTaskID
		if p.ActiveTaskID != nil {
			active = strings.TrimSpace(*p.ActiveTaskID)
			if err := c.reconcile(tx, active, now); err != nil {
				return err
			}
		}
		reply = HeartbeatReply{Name: a.Name, Status: "ready", ActiveTaskID: active}
		if active != "" {
			task, err := tx.Task(active)
			if err != nil && !errors.Is(err, ErrTaskNotFound) {
				return err
			}
			if err == nil {
				reply.CancelRequested = task.CancelRequestedAt > 0
			}
		}
		return nil
	})
	return reply, err
}
func (c Coordinator) Complete(ctx context.Context, token string, r Completion) (CompletionReply, error) {
	reply := CompletionReply{TaskID: r.TaskID}
	if err := reportValid(r.AgentReport); err != nil {
		return reply, err
	}
	if r.TaskID == "" || len(r.TaskID) > 255 || !Terminal(r.Status) || len(r.Message) > 16384 {
		return reply, fmt.Errorf("%w: invalid terminal task result", ErrControlInput)
	}
	if len(r.Result) == 0 {
		r.Result = json.RawMessage(`{}`)
	}
	var result map[string]json.RawMessage
	if json.Unmarshal(r.Result, &result) != nil || result == nil {
		return reply, fmt.Errorf("%w: result must be an object", ErrControlInput)
	}
	err := c.Repository.AgentTransaction(ctx, token, r.Identity, func(tx AgentTransaction) error {
		task, err := tx.Task(r.TaskID)
		if err != nil {
			return err
		}
		if !taskMigrated(task) {
			return ErrActionNotMigrated
		}
		if _, err := c.presence(tx, r.AgentReport, c.now()); err != nil {
			return err
		}
		if Terminal(task.Status) {
			reply.Status = task.Status
			return nil
		}
		if task.Status != "running" {
			return ErrTaskState
		}
		task.Status = r.Status
		task.Message = r.Message
		task.Result = r.Result
		task.UpdatedAt = c.now()
		task.CompletedAt = task.UpdatedAt
		if err := tx.SaveTask(task); err != nil {
			return err
		}
		reply.Status = task.Status
		return nil
	})
	return reply, err
}
func (c Coordinator) Progress(ctx context.Context, token string, r ProgressRequest) (ProgressReply, error) {
	reply := ProgressReply{TaskID: r.TaskID}
	if r.TaskID == "" || len(r.TaskID) > 255 {
		return reply, fmt.Errorf("%w: taskId is required", ErrControlInput)
	}
	if err := reportValid(AgentReport{Identity: r.Identity}); err != nil {
		return reply, err
	}
	events := make([]Event, 0, min(50, len(r.Events)))
	for _, event := range r.Events[:min(50, len(r.Events))] {
		if strings.TrimSpace(event.Line) == "" {
			continue
		}
		if event.Type == "" {
			event.Type = "output"
		}
		event.Type = truncateRunes(event.Type, 40)
		event.Line = truncateRunes(event.Line, 4000)
		if event.Timestamp <= 0 {
			event.Timestamp = c.now()
		}
		events = append(events, event)
	}
	err := c.Repository.AgentTransaction(ctx, token, r.Identity, func(tx AgentTransaction) error {
		task, err := tx.Task(r.TaskID)
		if err != nil {
			return err
		}
		if !taskMigrated(task) {
			return ErrActionNotMigrated
		}
		if Terminal(task.Status) {
			return nil
		}
		if task.Status != "running" {
			return ErrTaskState
		}
		if len(events) == 0 {
			return nil
		}
		if err := tx.AppendProgress(&task, events, 300); err != nil {
			return err
		}
		task.UpdatedAt = c.now()
		if err := tx.SaveTask(task); err != nil {
			return err
		}
		reply.Events = len(events)
		return nil
	})
	return reply, err
}
func truncateRunes(value string, limit int) string {
	r := []rune(value)
	if len(r) > limit {
		return string(r[:limit])
	}
	return value
}
