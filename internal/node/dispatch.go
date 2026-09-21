package node

import (
	"context"
	"fmt"
	"sort"
)

// Handler binds a narrow set of actions to an executor. Composition must make
// every action owner explicit, instead of growing one host-operation switch.
type Handler struct {
	Actions  []string
	Executor Executor
}
type Dispatcher struct {
	handlers     map[string]Executor
	capabilities []string
}

func NewDispatcher(handlers ...Handler) (*Dispatcher, error) {
	d := &Dispatcher{handlers: map[string]Executor{}}
	caps := map[string]bool{}
	for _, h := range handlers {
		if h.Executor == nil || len(h.Actions) == 0 {
			return nil, fmt.Errorf("invalid agent executor registration")
		}
		for _, action := range h.Actions {
			if action == "" || d.handlers[action] != nil {
				return nil, fmt.Errorf("duplicate or empty agent action registration")
			}
			d.handlers[action] = h.Executor
		}
		for _, cap := range h.Executor.Capabilities() {
			caps[cap] = true
		}
	}
	for cap := range caps {
		d.capabilities = append(d.capabilities, cap)
	}
	sort.Strings(d.capabilities)
	return d, nil
}
func (d *Dispatcher) Capabilities() []string { return append([]string(nil), d.capabilities...) }
func (d *Dispatcher) SupportedActions() []string {
	actions := make([]string, 0, len(d.handlers))
	for action := range d.handlers {
		actions = append(actions, action)
	}
	sort.Strings(actions)
	return actions
}
func (d *Dispatcher) Execute(ctx context.Context, task Task, emit func(Event)) (Outcome, error) {
	if executor := d.handlers[task.Action]; executor != nil {
		return executor.Execute(ctx, task, emit)
	}
	return Outcome{}, fmt.Errorf("unsupported node task action")
}
