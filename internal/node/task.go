// Package node owns agent task execution and acknowledgement policy. Host I/O,
// credentials, transport, and process supervision are supplied through ports.
package node

import (
	"context"
	"fmt"
	"time"

	"github.com/LiuTianjie/luma/internal/observability"
)

type Identity struct {
	Name string `json:"nodeName"`
	ID   string `json:"nodeId"`
}
type Task struct {
	ID, Action string
	Payload    []byte
}
type Event struct {
	Type      string `json:"type"`
	Line      string `json:"line"`
	Timestamp int64  `json:"ts,omitempty"`
}
type Metrics struct {
	CPUCount             int      `json:"cpuCount"`
	Load1                *float64 `json:"load1,omitempty"`
	LoadPercent          *float64 `json:"loadPercent,omitempty"`
	MemoryTotalBytes     uint64   `json:"memoryTotalBytes,omitempty"`
	MemoryAvailableBytes uint64   `json:"memoryAvailableBytes,omitempty"`
	CPUPercent           *float64 `json:"cpuPercent,omitempty"`
	MemoryUsedPercent    *float64 `json:"memoryUsedPercent,omitempty"`
	MetricsPath          string   `json:"metricsPath,omitempty"`
	DiskTotalBytes       uint64   `json:"diskTotalBytes,omitempty"`
	DiskAvailableBytes   uint64   `json:"diskAvailableBytes,omitempty"`
	DiskUsedPercent      *float64 `json:"diskUsedPercent,omitempty"`
	InodesUsedPercent    *float64 `json:"inodesUsedPercent,omitempty"`
}
type Presence struct {
	Identity
	OS               string                        `json:"os"`
	Arch             string                        `json:"arch"`
	Version          string                        `json:"version"`
	Capabilities     []string                      `json:"capabilities"`
	SupportedActions []string                      `json:"supportedActions,omitempty"`
	ActiveTaskID     string                        `json:"activeTaskId"`
	Metrics          Metrics                       `json:"metrics"`
	ContainerStats   []observability.ContainerStat `json:"containerStats"`
}
type Heartbeat struct{ CancelRequested bool }
type Acknowledgement struct{ TaskID, Status string }
type Outcome struct {
	Result       []byte
	Message      string
	RestartAgent bool
}

// Receipt contains only the terminal outcome. Leased payloads, credentials, and
// command environments must never be persisted in an agent recovery record.
type Receipt struct {
	Identity
	Authority               string
	TaskID, Status, Message string
	Result                  []byte
	RestartAgent            bool
}

func Terminal(status string) bool {
	return status == "succeeded" || status == "failed" || status == "canceled"
}
func (r Receipt) Validate() error {
	if r.TaskID == "" || r.Name == "" || r.Authority == "" || !Terminal(r.Status) {
		return fmt.Errorf("invalid terminal agent receipt")
	}
	return nil
}

type Control interface {
	Lease(context.Context, Presence, time.Duration) (*Task, error)
	Heartbeat(context.Context, Presence) (Heartbeat, error)
	Progress(context.Context, Identity, string, []Event) error
	Complete(context.Context, Receipt) (Acknowledgement, error)
}
type Executor interface {
	Capabilities() []string
	Execute(context.Context, Task, func(Event)) (Outcome, error)
}
type ActionCatalog interface{ SupportedActions() []string }
type Receipts interface {
	Load() (*Receipt, error)
	Save(Receipt) error
	Clear() error
}
type Telemetry interface{ Snapshot() Metrics }
type ContainerTelemetry interface {
	Snapshot() []observability.ContainerStat
}

// Commands are produced by typed executors, never accepted from an agent lease.
type Command struct {
	Args []string
	Dir  string
	Env  map[string]string
	// ClearEnv starts with an empty environment for isolated, untrusted-source
	// tools. Other commands retain their existing inherited-environment behavior.
	ClearEnv bool
	Secrets  []string
	Timeout  time.Duration
}
type CommandResult struct {
	Code   int
	Output string
}
type CommandRunner interface {
	Run(context.Context, Command, func(string)) (CommandResult, error)
}
