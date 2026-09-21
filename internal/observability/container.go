// Package observability defines resource projections without orchestration I/O.
package observability

type ContainerStat struct {
	ContainerID      string   `json:"containerId"`
	Name             string   `json:"name"`
	Service          string   `json:"service"`
	TaskID           string   `json:"taskId"`
	NomadAllocID     string   `json:"nomadAllocId,omitempty"`
	NomadTask        string   `json:"nomadTask,omitempty"`
	NomadGroup       string   `json:"nomadGroup,omitempty"`
	CPUPercent       *float64 `json:"cpuPercent,omitempty"`
	MemoryPercent    *float64 `json:"memoryPercent,omitempty"`
	MemoryUsageBytes uint64   `json:"memoryUsageBytes,omitempty"`
	MemoryLimitBytes uint64   `json:"memoryLimitBytes,omitempty"`
}
