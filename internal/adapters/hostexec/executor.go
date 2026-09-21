package hostexec

import (
	"context"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"time"

	"github.com/LiuTianjie/luma/internal/node"
)

// Executor dispatches only implemented host operations, with per-action typed
// payloads. An arbitrary shell command is never an accepted task action.
type Executor struct {
	Runner node.CommandRunner
	Docker string
}

func (e Executor) Capabilities() []string {
	caps := []string{"managed-volume-path"}
	if e.Docker != "" && e.Runner != nil {
		caps = append(caps, "docker-volume")
	}
	return caps
}
func (e Executor) Execute(ctx context.Context, task node.Task, emit func(node.Event)) (node.Outcome, error) {
	if err := ctx.Err(); err != nil {
		return node.Outcome{}, err
	}
	switch task.Action {
	case "prepare-managed-volume-path", "remove-managed-volume-path":
		var payload volumePath
		if err := decode(task.Payload, &payload); err != nil {
			return node.Outcome{}, err
		}
		path, err := volumeOperation(ctx, payload, task.Action == "remove-managed-volume-path")
		if err != nil {
			return node.Outcome{}, err
		}
		message := "volume path ready"
		if task.Action == "remove-managed-volume-path" {
			message = "volume path removed"
		}
		return outcome(map[string]string{"path": path, "message": message}, message)
	case "remove-docker-volume":
		var payload struct {
			Name string `json:"name"`
		}
		if err := decode(task.Payload, &payload); err != nil {
			return node.Outcome{}, err
		}
		if err := e.removeVolume(ctx, payload.Name, emit); err != nil {
			return node.Outcome{}, err
		}
		return outcome(map[string]string{"name": payload.Name, "message": "Docker volume removed"}, "Docker volume removed")
	default:
		return node.Outcome{}, fmt.Errorf("unsupported node task action: %s", task.Action)
	}
}
func decode(data []byte, output any) error {
	if len(data) > 4<<20 || !json.Valid(data) || len(strings.TrimSpace(string(data))) == 0 || strings.TrimSpace(string(data))[0] != '{' {
		return fmt.Errorf("invalid node task payload")
	}
	if err := json.Unmarshal(data, output); err != nil {
		return fmt.Errorf("invalid node task field types")
	}
	return nil
}
func outcome(value any, message string) (node.Outcome, error) {
	data, err := json.Marshal(value)
	return node.Outcome{Result: data, Message: message}, err
}

var volumeName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]*$`)

func (e Executor) removeVolume(ctx context.Context, name string, emit func(node.Event)) error {
	if !volumeName.MatchString(name) {
		return fmt.Errorf("invalid Docker volume name")
	}
	if e.Docker == "" || e.Runner == nil {
		return fmt.Errorf("Docker executable is unavailable")
	}
	for attempt := 0; attempt < 16; attempt++ {
		inspect, err := e.Runner.Run(ctx, node.Command{Args: []string{e.Docker, "volume", "inspect", name}, Timeout: 30 * time.Second}, nil)
		if err != nil {
			return err
		}
		if inspect.Code != 0 {
			if strings.Contains(strings.ToLower(inspect.Output), "no such volume") {
				return nil
			}
			return fmt.Errorf("could not inspect Docker volume; removal was not attempted")
		}
		removed, err := e.Runner.Run(ctx, node.Command{Args: []string{e.Docker, "volume", "rm", "-f", name}, Timeout: 30 * time.Second}, nil)
		if err != nil {
			return err
		}
		if removed.Code == 0 {
			return nil
		}
		if attempt == 15 {
			break
		}
		if emit != nil {
			emit(node.Event{Type: "output", Line: "Docker volume is still busy; waiting before removal retry"})
		}
		timer := time.NewTimer(2 * time.Second)
		select {
		case <-ctx.Done():
			timer.Stop()
			return ctx.Err()
		case <-timer.C:
		}
	}
	return fmt.Errorf("Docker volume removal did not complete")
}
