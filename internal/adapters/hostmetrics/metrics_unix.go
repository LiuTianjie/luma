//go:build linux || darwin

// Package hostmetrics samples host telemetry outside the agent lease/heartbeat
// path. Slow OS probes cannot delay task cancellation or result acknowledgement.
package hostmetrics

import (
	"context"
	"math"
	"os"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/LiuTianjie/luma/internal/node"
)

type Sampler struct {
	Runner              node.CommandRunner
	DiskPath            string
	mu                  sync.Mutex
	metrics             node.Metrics
	lastTotal, lastIdle uint64
}

func (s *Sampler) Snapshot() node.Metrics {
	s.mu.Lock()
	defer s.mu.Unlock()
	m := s.metrics
	if m.CPUCount == 0 {
		m.CPUCount = runtime.NumCPU()
	}
	return m
}
func (s *Sampler) Run(ctx context.Context) {
	for ctx.Err() == nil {
		s.sample(ctx)
		timer := time.NewTimer(30 * time.Second)
		select {
		case <-ctx.Done():
			timer.Stop()
			return
		case <-timer.C:
		}
	}
}
func (s *Sampler) sample(ctx context.Context) {
	m := node.Metrics{CPUCount: runtime.NumCPU()}
	if runtime.GOOS == "linux" {
		s.linux(&m)
	} else {
		s.darwin(ctx, &m)
	}
	path := s.DiskPath
	if path == "" {
		path = "/"
		if info, err := os.Stat("/opt/luma"); err == nil && info.IsDir() {
			path = "/opt/luma"
		}
	}
	var fs syscall.Statfs_t
	if strings.HasPrefix(path, "/") && syscall.Statfs(path, &fs) == nil && fs.Blocks > 0 {
		m.MetricsPath = path
		m.DiskTotalBytes = fs.Blocks * uint64(fs.Bsize)
		m.DiskAvailableBytes = fs.Bavail * uint64(fs.Bsize)
		used := fs.Blocks - min(fs.Blocks, fs.Bfree)
		if used+fs.Bavail > 0 {
			m.DiskUsedPercent = percent(float64(used) / float64(used+fs.Bavail) * 100)
		}
		if fs.Files > 0 {
			m.InodesUsedPercent = percent(float64(fs.Files-min(fs.Files, fs.Ffree)) / float64(fs.Files) * 100)
		}
	}
	s.mu.Lock()
	s.metrics = m
	s.mu.Unlock()
}
func percent(value float64) *float64 {
	value = math.Round(min(100, max(0, value))*10) / 10
	return &value
}
func number(value string) uint64 {
	n, _ := strconv.ParseUint(strings.TrimSpace(value), 10, 64)
	return n
}
func load(m *node.Metrics, text string) {
	fields := strings.Fields(strings.Trim(text, "{} \n"))
	if len(fields) == 0 {
		return
	}
	v, err := strconv.ParseFloat(fields[0], 64)
	if err == nil {
		v = math.Round(v*100) / 100
		m.Load1 = &v
		m.LoadPercent = percent(v / float64(m.CPUCount) * 100)
	}
}
func memory(m *node.Metrics, total, available uint64) {
	if total == 0 {
		return
	}
	m.MemoryTotalBytes = total
	m.MemoryAvailableBytes = available
	m.MemoryUsedPercent = percent(float64(total-min(total, available)) / float64(total) * 100)
}
func (s *Sampler) linux(m *node.Metrics) {
	data, _ := os.ReadFile("/proc/loadavg")
	load(m, string(data))
	data, _ = os.ReadFile("/proc/meminfo")
	values := map[string]uint64{}
	for _, line := range strings.Split(string(data), "\n") {
		parts := strings.Fields(line)
		if len(parts) >= 2 {
			values[strings.TrimSuffix(parts[0], ":")] = number(parts[1]) * 1024
		}
	}
	memory(m, values["MemTotal"], values["MemAvailable"])
	data, _ = os.ReadFile("/proc/stat")
	first, _, _ := strings.Cut(string(data), "\n")
	parts := strings.Fields(first)
	if len(parts) >= 5 && parts[0] == "cpu" {
		var total uint64
		for _, value := range parts[1:] {
			total += number(value)
		}
		idle := number(parts[4])
		if len(parts) > 5 {
			idle += number(parts[5])
		}
		if s.lastTotal > 0 && total > s.lastTotal && idle >= s.lastIdle {
			m.CPUPercent = percent(float64((total-s.lastTotal)-min(total-s.lastTotal, idle-s.lastIdle)) / float64(total-s.lastTotal) * 100)
		}
		s.lastTotal, s.lastIdle = total, idle
	}
}
func (s *Sampler) darwin(ctx context.Context, m *node.Metrics) {
	if s.Runner == nil {
		return
	}
	run := func(args ...string) string {
		result, err := s.Runner.Run(ctx, node.Command{Args: args, Timeout: 2 * time.Second}, nil)
		if err != nil || result.Code != 0 {
			return ""
		}
		return result.Output
	}
	load(m, run("/usr/sbin/sysctl", "-n", "vm.loadavg"))
	m.CPUPercent = m.LoadPercent
	total := number(run("/usr/sbin/sysctl", "-n", "hw.memsize"))
	stat := run("/usr/bin/vm_stat")
	pagesize := uint64(4096)
	if _, suffix, ok := strings.Cut(stat, "page size of "); ok {
		fields := strings.Fields(suffix)
		if len(fields) > 0 && number(fields[0]) > 0 {
			pagesize = number(fields[0])
		}
	}
	var available uint64
	for _, line := range strings.Split(stat, "\n") {
		key, value, ok := strings.Cut(line, ":")
		if ok && (key == "Pages free" || key == "Pages inactive" || key == "Pages speculative") {
			available += number(strings.TrimSuffix(strings.TrimSpace(value), ".")) * pagesize
		}
	}
	if stat != "" {
		memory(m, total, available)
	}
}
