// Package build owns build queue policy. Execution and durable queue mutations
// are migrated separately from the read-only queue-position projection.
package build

type QueueEntry struct {
	ID, Status, Mode                 string
	Project                          *string
	Order                            int64
	Executing, Managed, CallerQueued bool
}

type Position struct {
	QueuePosition int    `json:"queuePosition"`
	WaitingFor    string `json:"waitingFor"`
}

// PositionCounter computes a projection with constant memory while a repository
// streams candidates; callers never need to hydrate every build/event payload.
type PositionCounter struct {
	current QueueEntry
	count   int
	first   *QueueEntry
}

func NewPositionCounter(current QueueEntry) *PositionCounter {
	return &PositionCounter{current: current}
}
func (p *PositionCounter) Add(other QueueEntry) {
	if p.current.Status != "queued" || other.ID == p.current.ID || !sameProject(other.Project, p.current.Project) {
		return
	}
	active := other.Status == "running" || other.Status == "canceling" || other.Status == "finalizing"
	if !active && other.Status != "queued" {
		return
	}
	callerUpload := other.Mode == "local" && other.CallerQueued && !other.Managed
	holds := other.Executing || (active && !callerUpload)
	ahead := other.Status == "queued" && (other.Order < p.current.Order || other.Order == p.current.Order && other.ID < p.current.ID)
	if !holds && !ahead {
		return
	}
	p.count++
	if p.first == nil || before(other, *p.first) {
		copy := other
		p.first = &copy
	}
}
func (p *PositionCounter) Result() *Position {
	if p.current.Status != "queued" {
		return nil
	}
	result := &Position{QueuePosition: p.count + 1}
	if p.first != nil {
		result.WaitingFor = p.first.ID
	}
	return result
}
func sameProject(a, b *string) bool {
	if a == nil || b == nil {
		return a == b
	}
	return *a == *b
}
func before(a, b QueueEntry) bool {
	if (a.Status == "queued") != (b.Status == "queued") {
		return a.Status != "queued"
	}
	if a.Order != b.Order {
		return a.Order < b.Order
	}
	return a.ID < b.ID
}
