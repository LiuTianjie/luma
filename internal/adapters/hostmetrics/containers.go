package hostmetrics

import (
	"context"
	"encoding/json"
	"math"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/LiuTianjie/luma/internal/node"
	"github.com/LiuTianjie/luma/internal/observability"
)

// ContainerSampler is independent from host sampling and busy heartbeats. A
// wedged Docker socket cannot delay cancellation or authoritative task receipts.
type ContainerSampler struct {
	Runner   node.CommandRunner
	Docker   string
	Interval time.Duration
	Log      func(string)
	mu       sync.Mutex
	items    []observability.ContainerStat
}

func (s *ContainerSampler) Snapshot() []observability.ContainerStat {
	s.mu.Lock()
	defer s.mu.Unlock()
	items := make([]observability.ContainerStat, len(s.items))
	copy(items, s.items)
	for i := range items {
		if items[i].CPUPercent != nil {
			v := *items[i].CPUPercent
			items[i].CPUPercent = &v
		}
		if items[i].MemoryPercent != nil {
			v := *items[i].MemoryPercent
			items[i].MemoryPercent = &v
		}
	}
	return items
}
func (s *ContainerSampler) Run(ctx context.Context) {
	interval := s.Interval
	if interval <= 0 {
		interval = 30 * time.Second
	}
	failures := 0
	for ctx.Err() == nil {
		items, ok := s.sample(ctx)
		if !ok {
			failures++
			if s.Log != nil && (failures == 1 || failures%20 == 0) {
				s.Log("container statistics sampling failed")
			}
		} else {
			if failures > 0 && s.Log != nil {
				s.Log("container statistics sampling recovered")
			}
			failures = 0
		}
		s.mu.Lock()
		s.items = items
		s.mu.Unlock()
		timer := time.NewTimer(interval)
		select {
		case <-ctx.Done():
			timer.Stop()
			return
		case <-timer.C:
		}
	}
}

var containerID = regexp.MustCompile(`^[a-f0-9]{12,64}$`)

const containerFormat = "{{.ID}}\t{{.Names}}\t{{.Label \"com.hashicorp.nomad.alloc_id\"}}\t{{.Label \"com.hashicorp.nomad.job_name\"}}\t{{.Label \"com.hashicorp.nomad.task_name\"}}\t{{.Label \"com.hashicorp.nomad.task_group_name\"}}"

func (s *ContainerSampler) sample(ctx context.Context) ([]observability.ContainerStat, bool) {
	items := []observability.ContainerStat{}
	if s.Runner == nil || s.Docker == "" {
		return items, true
	}
	ps, err := s.Runner.Run(ctx, node.Command{Args: []string{s.Docker, "ps", "--format", containerFormat}, Timeout: 5 * time.Second}, nil)
	if err != nil || ps.Code != 0 {
		return items, false
	}
	seen := map[string]bool{}
	for _, line := range strings.Split(ps.Output, "\n") {
		parts := strings.Split(line, "\t")
		if len(parts) < 3 {
			continue
		}
		for len(parts) < 6 {
			parts = append(parts, "")
		}
		for i := range parts {
			parts[i] = strings.TrimSpace(parts[i])
			if parts[i] == "<no value>" {
				parts[i] = ""
			}
		}
		if !containerID.MatchString(parts[0]) || seen[parts[0]] {
			continue
		}
		service := parts[3]
		if service == "" && parts[2] != "" {
			service = "nomad:" + parts[2]
		}
		if service == "" {
			continue
		}
		items = append(items, observability.ContainerStat{ContainerID: parts[0], Name: parts[1], Service: service, TaskID: parts[4], NomadAllocID: parts[2], NomadTask: parts[4], NomadGroup: parts[5]})
		seen[parts[0]] = true
		if len(items) == 200 {
			break
		}
	}
	if len(items) == 0 {
		return items, true
	}
	args := []string{s.Docker, "stats", "--no-stream", "--format", "{{json .}}"}
	for _, item := range items {
		args = append(args, item.ContainerID)
	}
	stats, err := s.Runner.Run(ctx, node.Command{Args: args, Timeout: 10 * time.Second}, nil)
	if err != nil || stats.Code != 0 {
		return items, false
	}
	for _, line := range strings.Split(stats.Output, "\n") {
		var raw struct{ ID, Container, Name, CPUPerc, MemPerc, MemUsage string }
		if json.Unmarshal([]byte(line), &raw) != nil {
			continue
		}
		id := strings.TrimSpace(raw.ID)
		if id == "" {
			id = strings.TrimSpace(raw.Container)
		}
		match := -1
		for i, item := range items {
			if (id != "" && (strings.HasPrefix(item.ContainerID, id) || strings.HasPrefix(id, item.ContainerID))) || (raw.Name != "" && raw.Name == item.Name) {
				if match != -1 {
					match = -1
					break
				}
				match = i
			}
		}
		if match < 0 {
			continue
		}
		item := &items[match]
		item.CPUPercent = parsePercent(raw.CPUPerc)
		item.MemoryPercent = parsePercent(raw.MemPerc)
		used, limit, _ := strings.Cut(raw.MemUsage, "/")
		item.MemoryUsageBytes = parseSize(used)
		item.MemoryLimitBytes = parseSize(limit)
	}
	return items, true
}

func parsePercent(text string) *float64 {
	v, err := strconv.ParseFloat(strings.TrimSuffix(strings.TrimSpace(text), "%"), 64)
	if err != nil || math.IsNaN(v) || math.IsInf(v, 0) || v < 0 {
		return nil
	}
	v = math.Round(v*100) / 100
	return &v
}

var sizePattern = regexp.MustCompile(`^([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)$`)

func parseSize(text string) uint64 {
	parts := sizePattern.FindStringSubmatch(strings.TrimSpace(text))
	if parts == nil {
		return 0
	}
	v, err := strconv.ParseFloat(parts[1], 64)
	if err != nil {
		return 0
	}
	scales := map[string]float64{"b": 1, "kb": 1e3, "kib": 1 << 10, "mb": 1e6, "mib": 1 << 20, "gb": 1e9, "gib": 1 << 30, "tb": 1e12, "tib": 1 << 40}
	v *= scales[strings.ToLower(parts[2])]
	if math.IsInf(v, 0) || v >= math.MaxUint64 {
		return 0
	}
	return uint64(v)
}
