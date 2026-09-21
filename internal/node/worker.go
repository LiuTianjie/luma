package node

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/LiuTianjie/luma/internal/observability"
)

type Worker struct {
	Control    Control
	Executor   Executor
	Receipts   Receipts
	Telemetry  Telemetry
	Containers ContainerTelemetry
	Presence   Presence
	Authority  string
	Options    WorkerOptions
	Log        func(string)
}
type WorkerOptions struct {
	Once                                                                                          bool
	PollInterval, LeaseWait, HeartbeatInterval, RequestTimeout, RetryMin, RetryMax, ShutdownGrace time.Duration
}

func (o WorkerOptions) defaults() WorkerOptions {
	if o.PollInterval <= 0 {
		o.PollInterval = 5 * time.Second
	}
	if o.LeaseWait <= 0 {
		o.LeaseWait = 10 * time.Second
	}
	if o.HeartbeatInterval <= 0 {
		o.HeartbeatInterval = 2 * time.Second
	}
	if o.RequestTimeout <= 0 {
		o.RequestTimeout = 15 * time.Second
	}
	if o.RetryMin <= 0 {
		o.RetryMin = 500 * time.Millisecond
	}
	if o.RetryMax <= 0 {
		o.RetryMax = 10 * time.Second
	}
	if o.ShutdownGrace <= 0 {
		o.ShutdownGrace = 15 * time.Second
	}
	return o
}
func (w Worker) log(text string) {
	if w.Log != nil {
		w.Log(text)
	}
}
func (w Worker) presence(active string) Presence {
	p := w.Presence
	p.ActiveTaskID = active
	p.Capabilities = w.Executor.Capabilities()
	if catalog, ok := w.Executor.(ActionCatalog); ok {
		p.SupportedActions = catalog.SupportedActions()
	}
	if p.Capabilities == nil {
		p.Capabilities = []string{}
	}
	if w.Telemetry != nil {
		p.Metrics = w.Telemetry.Snapshot()
	}
	p.ContainerStats = []observability.ContainerStat{}
	if w.Containers != nil {
		p.ContainerStats = w.Containers.Snapshot()
	}
	return p
}

func (w Worker) Run(ctx context.Context) error {
	if w.Control == nil || w.Executor == nil || w.Receipts == nil || w.Presence.Name == "" || w.Authority == "" {
		return fmt.Errorf("agent worker is not configured")
	}
	w.Options = w.Options.defaults()
	pending, err := w.Receipts.Load()
	if err != nil {
		return fmt.Errorf("read pending agent receipt: %w", err)
	}
	if pending != nil {
		if pending.Identity != w.Presence.Identity || pending.Authority != w.Authority {
			return fmt.Errorf("pending agent receipt belongs to another node or Control endpoint")
		}
		if err := pending.Validate(); err != nil {
			return err
		}
		// Recover before the first idle lease; an idle heartbeat could otherwise
		// tell Control that this still-unacknowledged task was abandoned.
		if err := w.acknowledge(ctx, *pending); err != nil {
			return err
		}
		if w.Options.Once || pending.RestartAgent {
			return nil
		}
	}
	for ctx.Err() == nil {
		call, cancel := context.WithTimeout(ctx, w.Options.LeaseWait+w.Options.RequestTimeout)
		task, err := w.Control.Lease(call, w.presence(""), w.Options.LeaseWait)
		cancel()
		if err != nil {
			if w.Options.Once {
				return err
			}
			w.log("node agent lease failed; retrying")
		} else if task != nil {
			if task.ID == "" || task.Action == "" {
				return fmt.Errorf("Control returned an invalid agent task")
			}
			restart, err := w.complete(ctx, *task)
			if err != nil {
				return err
			}
			if restart {
				return nil
			}
		}
		if w.Options.Once {
			return nil
		}
		if err := wait(ctx, w.Options.PollInterval); err != nil {
			return err
		}
	}
	return ctx.Err()
}

func (w Worker) complete(ctx context.Context, task Task) (bool, error) {
	execution, cancelTask := context.WithCancel(ctx)
	defer cancelTask()
	// Keep heartbeat/reporting alive for a bounded shutdown grace while the
	// executor stops its children. A durable receipt handles the next startup.
	reportCtx, stopReporting := shutdownContext(ctx, w.Options.ShutdownGrace)
	defer stopReporting()
	stopHeartbeat := w.heartbeat(reportCtx, task.ID, cancelTask)
	defer stopHeartbeat()
	progress := newProgress(reportCtx, w.Control, w.Presence.Identity, task.ID)
	outcome, err := w.Executor.Execute(execution, task, progress.Submit)
	progress.Close()
	r := Receipt{Identity: w.Presence.Identity, Authority: w.Authority, TaskID: task.ID, Status: "succeeded", Result: outcome.Result, Message: outcome.Message, RestartAgent: outcome.RestartAgent}
	if err != nil {
		r.Status = "failed"
		r.Message = err.Error()
		r.Result = []byte(`{}`)
		r.RestartAgent = false
	}
	if execution.Err() != nil {
		r.Status = "canceled"
		r.Message = "agent task canceled"
		r.Result = []byte(`{}`)
		r.RestartAgent = false
	}
	if r.Message == "" {
		r.Message = "ok"
	}
	if len(r.Result) == 0 {
		r.Result = []byte(`{}`)
	}
	// Persist the exact decision before its first delivery. Failure to write or
	// acknowledge never reruns the mutation or replaces success with failure.
	if err := w.Receipts.Save(r); err != nil {
		return false, fmt.Errorf("persist agent outcome: %w", err)
	}
	if err := w.deliver(reportCtx, r); err != nil {
		return false, err
	}
	return r.RestartAgent, nil
}
func (w Worker) acknowledge(ctx context.Context, r Receipt) error {
	stop := w.heartbeat(ctx, r.TaskID, func() {})
	defer stop()
	return w.deliver(ctx, r)
}
func (w Worker) deliver(ctx context.Context, r Receipt) error {
	delay := w.Options.RetryMin
	for ctx.Err() == nil {
		call, cancel := context.WithTimeout(ctx, w.Options.RequestTimeout)
		ack, err := w.Control.Complete(call, r)
		cancel()
		if err == nil && ack.TaskID == r.TaskID && Terminal(ack.Status) {
			if ack.Status != r.Status {
				w.log("Control retained a different terminal decision for the completed agent task")
			}
			return w.Receipts.Clear()
		}
		w.log("agent outcome is pending acknowledgement; host action will not be replayed")
		if err := wait(ctx, delay); err != nil {
			return err
		}
		delay = min(delay*2, w.Options.RetryMax)
	}
	return ctx.Err()
}
func (w Worker) heartbeat(ctx context.Context, taskID string, cancelTask context.CancelFunc) func() {
	ctx, cancel := context.WithCancel(ctx)
	done := make(chan struct{})
	go func() {
		defer close(done)
		for ctx.Err() == nil {
			call, stop := context.WithTimeout(ctx, min(w.Options.RequestTimeout, w.Options.HeartbeatInterval))
			status, err := w.Control.Heartbeat(call, w.presence(taskID))
			stop()
			if err != nil {
				w.log("node agent busy heartbeat failed")
			} else if status.CancelRequested {
				cancelTask()
			}
			if wait(ctx, w.Options.HeartbeatInterval) != nil {
				return
			}
		}
	}()
	return func() { cancel(); <-done }
}
func wait(ctx context.Context, duration time.Duration) error {
	timer := time.NewTimer(duration)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}
func shutdownContext(parent context.Context, grace time.Duration) (context.Context, context.CancelFunc) {
	ctx, cancel := context.WithCancel(context.WithoutCancel(parent))
	go func() {
		select {
		case <-ctx.Done():
			return
		case <-parent.Done():
		}
		if errors.Is(wait(ctx, grace), context.Canceled) {
			return
		}
		cancel()
	}()
	return ctx, cancel
}
