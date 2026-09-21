package deployment

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"slices"
	"strings"
	"time"

	"github.com/LiuTianjie/luma/internal/storage"
)

var ErrRuntime = errors.New("deployment runtime failure")
var ErrSuperseded = errors.New("deployment superseded by another Nomad job revision")
var ErrJobNotFound = errors.New("Nomad job not found")

type RuntimeJob struct {
	ID          string
	ModifyIndex uint64
	Version     *uint64
	Groups      map[string]int
	Placement   storage.PlacementEvidence
}
type Registration struct {
	EvalID      string
	ModifyIndex uint64
}
type Evaluation struct {
	ID, JobID                           string
	ModifyIndex                         uint64
	Status, Next, Blocked, DeploymentID string
	PlacementFailed                     bool
}
type Rollout struct {
	ID, JobID   string
	ModifyIndex uint64
	Version     *uint64
	CreateIndex uint64
	Status      string
	Groups      map[string]int
}
type Allocation struct {
	ID, NodeID, JobID, Group string
	Version                  *uint64
	CreateIndex              uint64
	Desired, Status          string
	Tasks                    map[string]TaskState
	Healthy                  *bool
	HostPorts                []int
}
type TaskState struct {
	State  string
	Failed bool
}
type Runtime interface {
	Inspect(context.Context, string) (RuntimeJob, error)
	Allocations(context.Context, string) ([]Allocation, error)
	Register(context.Context, json.RawMessage, uint64) (Registration, error)
	Evaluation(context.Context, string) (Evaluation, error)
	Rollout(context.Context, string) (Rollout, error)
	Rollouts(context.Context, string) ([]Rollout, error)
}
type RolloutResult struct {
	JobID              string `json:"jobId"`
	ModifyIndex        uint64 `json:"jobModifyIndex"`
	Version            uint64 `json:"jobVersion"`
	EvalID             string `json:"evalId"`
	DeploymentID       string `json:"deploymentId,omitempty"`
	HealthyAllocations int    `json:"healthyAllocations"`
}
type RolloutMonitor struct {
	Runtime      Runtime
	PollInterval time.Duration
}

func (m RolloutMonitor) poll(ctx context.Context) error {
	interval := m.PollInterval
	if interval <= 0 {
		interval = time.Second
	}
	t := time.NewTimer(interval)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}
func runtimeError(message string) error { return fmt.Errorf("%w: %s", ErrRuntime, message) }
func identity(job, actual string, index, actualIndex uint64, required bool) error {
	if actual != job || actualIndex != index && (required || actualIndex != 0) {
		return runtimeError("Nomad rollout identity does not match this submission")
	}
	return nil
}
func (m RolloutMonitor) target(ctx context.Context, job string, index uint64) (RuntimeJob, error) {
	j, err := m.Runtime.Inspect(ctx, job)
	if err != nil {
		return j, err
	}
	if j.ID != job || j.ModifyIndex != index {
		return j, ErrSuperseded
	}
	if j.Version == nil || len(j.Groups) == 0 {
		return j, runtimeError("Nomad job lacks version or desired task groups")
	}
	return j, nil
}

// Wait verifies only the registration's exact JobModifyIndex and JobVersion.
// It never registers/replays a job, including after transport errors or restart.
func (m RolloutMonitor) Wait(ctx context.Context, job string, reg Registration) (RolloutResult, error) {
	r := RolloutResult{JobID: job, ModifyIndex: reg.ModifyIndex, EvalID: reg.EvalID}
	if reg.ModifyIndex == 0 {
		return r, runtimeError("Nomad registration omitted JobModifyIndex; submission may have been accepted")
	}
	eval := reg.EvalID
	visited := map[string]bool{}
	for eval != "" {
		if err := ctx.Err(); err != nil {
			return r, err
		}
		if visited[eval] || len(visited) > 1000 {
			return r, runtimeError("Nomad evaluation chain loops or exceeds limit")
		}
		e, err := m.Runtime.Evaluation(ctx, eval)
		if err != nil {
			return r, err
		}
		if e.ID != eval {
			return r, runtimeError("Nomad returned the wrong evaluation")
		}
		if err := identity(job, e.JobID, reg.ModifyIndex, e.ModifyIndex, eval == reg.EvalID); err != nil {
			return r, err
		}
		switch strings.ToLower(e.Status) {
		case "failed", "canceled", "cancelled", "blocked":
			return r, runtimeError("Nomad evaluation " + e.Status)
		}
		if e.Blocked != "" {
			return r, runtimeError("Nomad placement is blocked")
		}
		if e.Status == "complete" {
			if e.DeploymentID != "" || e.Next == "" {
				if e.PlacementFailed && e.DeploymentID == "" {
					return r, runtimeError("Nomad placement failed")
				}
				r.DeploymentID = e.DeploymentID
				break
			}
			visited[eval] = true
			eval = e.Next
		}
		if err := m.poll(ctx); err != nil {
			return r, err
		}
	}
	target, err := m.target(ctx, job, reg.ModifyIndex)
	if err != nil {
		return r, err
	}
	r.Version = *target.Version
	if r.DeploymentID == "" {
		rows, err := m.Runtime.Rollouts(ctx, job)
		if err != nil {
			return r, err
		}
		var newest Rollout
		for _, d := range rows {
			if d.JobID == job && d.ModifyIndex == reg.ModifyIndex && d.Version != nil && *d.Version == r.Version && d.CreateIndex >= newest.CreateIndex {
				newest = d
			}
		}
		if newest.ID != "" && !slices.Contains([]string{"successful", "failed", "canceled", "cancelled"}, newest.Status) {
			r.DeploymentID = newest.ID
		}
	}
	for r.DeploymentID != "" {
		if err := ctx.Err(); err != nil {
			return r, err
		}
		d, err := m.Runtime.Rollout(ctx, r.DeploymentID)
		if err != nil {
			return r, err
		}
		if d.ID != r.DeploymentID || d.Version == nil || *d.Version != r.Version {
			return r, runtimeError("Nomad deployment version mismatch")
		}
		if err := identity(job, d.JobID, reg.ModifyIndex, d.ModifyIndex, true); err != nil {
			return r, err
		}
		if len(d.Groups) > 0 && !maps.Equal(d.Groups, target.Groups) {
			return r, runtimeError("Nomad deployment desired counts differ from submitted job")
		}
		if d.Status == "successful" {
			break
		}
		if slices.Contains([]string{"failed", "canceled", "cancelled", "blocked", "paused"}, d.Status) {
			return r, runtimeError("Nomad deployment " + d.Status)
		}
		if _, err := m.target(ctx, job, reg.ModifyIndex); err != nil {
			return r, err
		}
		if err := m.poll(ctx); err != nil {
			return r, err
		}
	}
	for {
		if err := ctx.Err(); err != nil {
			return r, err
		}
		current, err := m.target(ctx, job, reg.ModifyIndex)
		if err != nil {
			return r, err
		}
		if *current.Version != r.Version || !maps.Equal(current.Groups, target.Groups) {
			return r, ErrSuperseded
		}
		allocs, err := m.Runtime.Allocations(ctx, job)
		if err != nil {
			return r, err
		}
		ready, count := AllocationsHealthy(allocs, job, r.Version, target.Groups)
		if ready {
			// Close the read race: an out-of-band update between job and allocation
			// queries must not let old healthy allocations acknowledge new state.
			if _, err := m.target(ctx, job, reg.ModifyIndex); err != nil {
				return r, err
			}
			r.HealthyAllocations = count
			return r, nil
		}
		if err := m.poll(ctx); err != nil {
			return r, err
		}
	}
}

func AllocationsHealthy(allocs []Allocation, job string, version uint64, groups map[string]int) (bool, int) {
	active, healthy := map[string]int{}, map[string]int{}
	seen := map[string]bool{}
	for _, a := range allocs {
		if a.ID == "" || seen[a.ID] {
			return false, 0
		}
		seen[a.ID] = true
		if a.JobID != job || a.Version == nil || *a.Version != version || a.Desired != "" && a.Desired != "run" && a.Desired != "running" || slices.Contains([]string{"complete", "dead", "failed", "lost"}, a.Status) {
			continue
		}
		if _, ok := groups[a.Group]; !ok {
			continue
		}
		active[a.Group]++
		running := len(a.Tasks) > 0
		for _, task := range a.Tasks {
			if task.State != "running" || task.Failed {
				running = false
				break
			}
		}
		if a.Status != "running" || len(a.Tasks) > 0 && !running || a.Healthy != nil && !*a.Healthy || a.Healthy == nil && !running {
			continue
		}
		healthy[a.Group]++
	}
	count := 0
	for group, desired := range groups {
		if desired < 0 || active[group] != desired || healthy[group] != desired {
			return false, 0
		}
		count += healthy[group]
	}
	return len(groups) > 0, count
}
