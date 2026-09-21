package node

import (
	"context"
	"sync"
	"time"
)

// Progress is observational. Bounded buffering prevents a stalled Control from
// blocking process stdout or consuming unbounded memory during noisy builds.
type progressReporter struct {
	mu     sync.Mutex
	closed bool
	queue  chan Event
	stop   context.CancelFunc
	done   chan struct{}
}

func newProgress(ctx context.Context, control Control, identity Identity, taskID string) *progressReporter {
	ctx, cancel := context.WithCancel(ctx)
	p := &progressReporter{queue: make(chan Event, 1000), stop: cancel, done: make(chan struct{})}
	go func() {
		defer close(p.done)
		for {
			var first Event
			var ok bool
			select {
			case <-ctx.Done():
				return
			case first, ok = <-p.queue:
				if !ok {
					return
				}
			}
			batch := []Event{first}
			timer := time.NewTimer(250 * time.Millisecond)
			closed := false
		collect:
			for len(batch) < 50 {
				select {
				case event, ok := <-p.queue:
					if !ok {
						closed = true
						break collect
					}
					batch = append(batch, event)
				case <-timer.C:
					break collect
				case <-ctx.Done():
					timer.Stop()
					return
				}
			}
			timer.Stop()
			for attempt := 0; attempt < 2; attempt++ {
				call, stop := context.WithTimeout(ctx, 5*time.Second)
				err := control.Progress(call, identity, taskID, batch)
				stop()
				if err == nil {
					break
				}
				if wait(ctx, 250*time.Millisecond) != nil {
					return
				}
			}
			if closed {
				return
			}
		}
	}()
	return p
}
func (p *progressReporter) Submit(event Event) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.closed {
		return
	}
	if len(event.Line) > 4000 {
		event.Line = event.Line[:4000]
	}
	if len(event.Type) > 40 {
		event.Type = event.Type[:40]
	}
	if event.Timestamp == 0 {
		event.Timestamp = time.Now().Unix()
	}
	select {
	case p.queue <- event:
	default:
	}
}
func (p *progressReporter) Close() {
	p.mu.Lock()
	if !p.closed {
		p.closed = true
		close(p.queue)
	}
	p.mu.Unlock()
	timer := time.NewTimer(12 * time.Second)
	defer timer.Stop()
	select {
	case <-p.done:
	case <-timer.C:
		p.stop()
		<-p.done
	}
	p.stop()
}
