package node

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type memoryReceipts struct {
	pending       *Receipt
	saves, clears int
}

func (s *memoryReceipts) Load() (*Receipt, error) { return s.pending, nil }
func (s *memoryReceipts) Save(r Receipt) error    { s.saves++; s.pending = &r; return nil }
func (s *memoryReceipts) Clear() error            { s.clears++; s.pending = nil; return nil }

type executorFunc func(context.Context, Task, func(Event)) (Outcome, error)

func (f executorFunc) Execute(c context.Context, t Task, e func(Event)) (Outcome, error) {
	return f(c, t, e)
}
func (executorFunc) Capabilities() []string { return []string{"implemented"} }

type controlFixture struct {
	lease     func(context.Context, Presence, time.Duration) (*Task, error)
	heartbeat func(context.Context, Presence) (Heartbeat, error)
	progress  func(context.Context, Identity, string, []Event) error
	complete  func(context.Context, Receipt) (Acknowledgement, error)
}

func (f controlFixture) Lease(c context.Context, p Presence, d time.Duration) (*Task, error) {
	return f.lease(c, p, d)
}
func (f controlFixture) Heartbeat(c context.Context, p Presence) (Heartbeat, error) {
	if f.heartbeat != nil {
		return f.heartbeat(c, p)
	}
	return Heartbeat{}, nil
}
func (f controlFixture) Progress(c context.Context, i Identity, id string, e []Event) error {
	if f.progress != nil {
		return f.progress(c, i, id, e)
	}
	return nil
}
func (f controlFixture) Complete(c context.Context, r Receipt) (Acknowledgement, error) {
	return f.complete(c, r)
}
func testWorker(control Control, executor Executor, receipts Receipts) Worker {
	return Worker{Control: control, Executor: executor, Receipts: receipts, Authority: "https://control.test", Presence: Presence{Identity: Identity{Name: "worker", ID: "node-1"}}, Options: WorkerOptions{Once: true, HeartbeatInterval: time.Millisecond, RetryMin: 5 * time.Millisecond, RetryMax: 10 * time.Millisecond, RequestTimeout: 20 * time.Millisecond, ShutdownGrace: 20 * time.Millisecond}}
}

func TestTaskRetainsOwnershipUntilTerminalAcknowledgement(t *testing.T) {
	store := &memoryReceipts{}
	var executed, leased int
	var heartbeats atomic.Int32
	ready := make(chan struct{})
	var readyOnce sync.Once
	var reports []Receipt
	var progress []Event
	control := controlFixture{
		lease: func(_ context.Context, p Presence, _ time.Duration) (*Task, error) {
			leased++
			if p.ActiveTaskID != "" || !reflect.DeepEqual(p.Capabilities, []string{"implemented"}) {
				t.Error(p)
			}
			return &Task{ID: "task", Action: "mutate", Payload: []byte(`{"credential":"never-persist"}`)}, nil
		},
		heartbeat: func(_ context.Context, p Presence) (Heartbeat, error) {
			if p.ActiveTaskID != "task" {
				t.Error("lost active task")
			}
			heartbeats.Add(1)
			readyOnce.Do(func() { close(ready) })
			return Heartbeat{}, nil
		},
		progress: func(_ context.Context, _ Identity, _ string, e []Event) error {
			progress = append(progress, e...)
			return nil
		},
		complete: func(_ context.Context, r Receipt) (Acknowledgement, error) {
			if store.pending == nil {
				t.Error("report sent before durable save")
			}
			reports = append(reports, r)
			if len(reports) < 3 {
				return Acknowledgement{}, errors.New("gateway unavailable")
			}
			return Acknowledgement{TaskID: r.TaskID, Status: r.Status}, nil
		},
	}
	executor := executorFunc(func(ctx context.Context, _ Task, emit func(Event)) (Outcome, error) {
		executed++
		select {
		case <-ready:
		case <-ctx.Done():
			return Outcome{}, ctx.Err()
		}
		for i := 0; i < 250; i++ {
			emit(Event{Type: "output", Line: fmt.Sprint(i)})
		}
		return Outcome{Result: []byte(`{"path":"/tmp/safe"}`), Message: "done"}, nil
	})
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := testWorker(control, executor, store).Run(ctx); err != nil {
		t.Fatal(err)
	}
	if executed != 1 || leased != 1 || store.saves != 1 || store.clears != 1 || len(reports) != 3 || heartbeats.Load() < 2 {
		t.Fatalf("executed=%d leased=%d reports=%d heartbeats=%d", executed, leased, len(reports), heartbeats.Load())
	}
	for _, r := range reports {
		if r.Status != "succeeded" || !reflect.DeepEqual(r, reports[0]) {
			t.Error("terminal decision changed on retry")
		}
	}
	if len(progress) != 250 {
		t.Fatalf("progress count=%d", len(progress))
	}
	for i, event := range progress {
		if event.Line != fmt.Sprint(i) {
			t.Fatal("progress order changed")
		}
	}
}

func TestCancellationStopsExecutorAndReportsCanceled(t *testing.T) {
	control := controlFixture{lease: func(context.Context, Presence, time.Duration) (*Task, error) {
		return &Task{ID: "task", Action: "work"}, nil
	}, heartbeat: func(context.Context, Presence) (Heartbeat, error) { return Heartbeat{CancelRequested: true}, nil }, complete: func(_ context.Context, r Receipt) (Acknowledgement, error) {
		if r.Status != "canceled" || string(r.Result) != "{}" {
			t.Error(r)
		}
		return Acknowledgement{TaskID: r.TaskID, Status: "canceled"}, nil
	}}
	executor := executorFunc(func(ctx context.Context, _ Task, _ func(Event)) (Outcome, error) {
		<-ctx.Done()
		return Outcome{}, ctx.Err()
	})
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := testWorker(control, executor, &memoryReceipts{}).Run(ctx); err != nil {
		t.Fatal(err)
	}
}

func TestRecoveryResendsReceiptBeforeLeaseAndNeverReexecutes(t *testing.T) {
	store := &memoryReceipts{pending: &Receipt{Identity: Identity{Name: "worker", ID: "node-1"}, Authority: "https://control.test", TaskID: "done", Status: "succeeded", Result: []byte(`{"path":"/data"}`)}}
	attempts := 0
	control := controlFixture{lease: func(context.Context, Presence, time.Duration) (*Task, error) {
		t.Error("leased before recovery")
		return nil, nil
	}, complete: func(_ context.Context, r Receipt) (Acknowledgement, error) {
		attempts++
		if attempts == 1 {
			return Acknowledgement{TaskID: "wrong", Status: "succeeded"}, nil
		}
		return Acknowledgement{TaskID: r.TaskID, Status: "succeeded"}, nil
	}}
	executor := executorFunc(func(context.Context, Task, func(Event)) (Outcome, error) {
		t.Fatal("reexecuted acknowledged host action")
		return Outcome{}, nil
	})
	if err := testWorker(control, executor, store).Run(context.Background()); err != nil {
		t.Fatal(err)
	}
	if attempts != 2 || store.clears != 1 || store.saves != 0 {
		t.Fatal("invalid receipt recovery")
	}
}

func TestShutdownKeepsUnacknowledgedSuccessfulReceipt(t *testing.T) {
	store := &memoryReceipts{}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	control := controlFixture{lease: func(context.Context, Presence, time.Duration) (*Task, error) {
		return &Task{ID: "done", Action: "work"}, nil
	}, complete: func(context.Context, Receipt) (Acknowledgement, error) {
		cancel()
		return Acknowledgement{}, errors.New("offline")
	}}
	executor := executorFunc(func(context.Context, Task, func(Event)) (Outcome, error) {
		return Outcome{Message: "done", Result: []byte(`{}`)}, nil
	})
	err := testWorker(control, executor, store).Run(ctx)
	if !errors.Is(err, context.Canceled) || store.pending == nil || store.pending.Status != "succeeded" || store.clears != 0 {
		t.Fatalf("error=%v receipt=%+v", err, store.pending)
	}
}

func TestProgressOutageCannotBlockProducer(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	started := make(chan struct{})
	var once sync.Once
	control := controlFixture{progress: func(ctx context.Context, _ Identity, _ string, _ []Event) error {
		once.Do(func() { close(started) })
		<-ctx.Done()
		return ctx.Err()
	}}
	p := newProgress(ctx, control, Identity{Name: "worker"}, "task")
	done := make(chan struct{})
	go func() {
		for i := 0; i < 100000; i++ {
			p.Submit(Event{Line: "output"})
		}
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(time.Second):
		cancel()
		t.Fatal("process output was blocked by Control")
	}
	select {
	case <-started:
	case <-time.After(time.Second):
		cancel()
		t.Fatal("progress sender did not start")
	}
	if len(p.queue) > 1000 {
		t.Fatal("unbounded queue")
	}
	cancel()
	p.Close()
}
