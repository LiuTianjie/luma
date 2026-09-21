package controlstore

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"time"

	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/node"
)

func readBuild(ctx context.Context, tx *sql.Tx, id string) (builddomain.Run, error) {
	var raw []byte
	err := tx.QueryRowContext(ctx, `SELECT CASE WHEN length(CAST(payload AS BLOB))<=8388608 THEN payload ELSE NULL END FROM control_entities WHERE kind='buildRuns' AND id=?`, id).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return builddomain.Run{}, builddomain.ErrNotFound
	}
	if err != nil {
		return builddomain.Run{}, err
	}
	var r builddomain.Run
	if json.Unmarshal(raw, &r) != nil || r.ID != id {
		return r, fmt.Errorf("invalid build run record")
	}
	return r, nil
}
func activeBuilds(ctx context.Context, tx *sql.Tx) ([]builddomain.Run, error) {
	rows, err := tx.QueryContext(ctx, `SELECT payload FROM control_entities WHERE kind='buildRuns' AND (status IN ('queued','running','canceling','finalizing') OR json_extract(payload,'$.queueExecuting')=1) ORDER BY json_extract(payload,'$.queueOrder'),id LIMIT 10001`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []builddomain.Run{}
	for rows.Next() {
		var raw []byte
		if err := rows.Scan(&raw); err != nil {
			return nil, err
		}
		var r builddomain.Run
		if json.Unmarshal(raw, &r) != nil {
			return nil, fmt.Errorf("invalid queued build")
		}
		result = append(result, r)
	}
	if len(result) > 10000 {
		return nil, fmt.Errorf("too many active builds")
	}
	return result, rows.Err()
}
func buildApp(r builddomain.Run) string {
	for _, key := range []string{"service", "deployment"} {
		if value, ok := r.Result[key].(string); ok && value != "" {
			return value
		}
	}
	return ""
}
func saveBuild(ctx context.Context, tx *sql.Tx, r builddomain.Run) error {
	raw, err := json.Marshal(r)
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `UPDATE control_entities SET payload=json_patch(payload,?),status=?,app=?,created_at=?,updated_at=? WHERE kind='buildRuns' AND id=?`, string(raw), r.Status, buildApp(r), r.CreatedAt, r.UpdatedAt, r.ID)
	return err
}
func privateBuildQueue(ctx context.Context, tx *sql.Tx) (document, error) {
	return configDocument(ctx, tx, "buildQueue", 32<<20)
}
func writeBuildQueue(ctx context.Context, tx *sql.Tx, d document) error {
	raw, err := json.Marshal(d)
	if err != nil {
		return err
	}
	if len(raw) > 32<<20 {
		return fmt.Errorf("build queue exceeds size limit")
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO control_config(key,payload) VALUES('buildQueue',?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload`, string(raw))
	return err
}
func discardBuild(ctx context.Context, tx *sql.Tx, r *builddomain.Run) error {
	queue, err := privateBuildQueue(ctx, tx)
	if err != nil {
		return err
	}
	delete(queue, r.ID)
	if err := writeBuildQueue(ctx, tx, queue); err != nil {
		return err
	}
	r.QueueExecuting = false
	r.QueueOwnerReleased = false
	return nil
}
func stopBuildChild(ctx context.Context, tx *sql.Tx, r builddomain.Run) (bool, error) {
	if r.AgentTaskID == "" {
		return true, nil
	}
	var raw []byte
	err := tx.QueryRowContext(ctx, `SELECT payload FROM control_entities WHERE kind='agentTasks' AND id=?`, r.AgentTaskID).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		// Missing child rows are not proof that a remote process stopped.
		var nodeRaw []byte
		if err := tx.QueryRowContext(ctx, `SELECT payload FROM control_entities WHERE kind='nodes' AND id=?`, r.BuildNode).Scan(&nodeRaw); err != nil {
			return false, nil
		}
		n, err := readDocument(nodeRaw)
		if err != nil {
			return false, err
		}
		a := n.object("agent")
		_, known := a["activeTaskId"]
		return known && a.text("activeTaskId") != r.AgentTaskID && a.number("activeTaskObservedAt") > r.UpdatedAt && time.Now().Unix()-a.number("lastSeen") <= 120, nil
	}
	if err != nil {
		return false, err
	}
	d, err := readDocument(raw)
	if err != nil {
		return false, err
	}
	if d.text("buildRunId") != r.ID {
		return false, builddomain.ErrState
	}
	if node.Terminal(d.text("status")) {
		return true, nil
	}
	now := time.Now().Unix()
	_ = d.set("cancelRequestedAt", now)
	_ = d.set("updatedAt", now)
	stopped := d.text("status") == "queued"
	if stopped {
		_ = d.set("status", "canceled")
		_ = d.set("completedAt", now)
		_ = d.set("message", "Owning build canceled before execution")
	}
	raw, err = json.Marshal(d)
	if err != nil {
		return false, err
	}
	_, err = tx.ExecContext(ctx, `UPDATE control_entities SET payload=?,status=?,updated_at=? WHERE kind='agentTasks' AND id=?`, string(raw), d.text("status"), now, r.AgentTaskID)
	return stopped, err
}
func requestBuildCancel(ctx context.Context, tx *sql.Tx, r *builddomain.Run) error {
	if builddomain.Terminal(r.Status) {
		return nil
	}
	now := time.Now().Unix()
	r.CancelRequestedAt = now
	r.UpdatedAt = now
	_, err := stopBuildChild(ctx, tx, *r)
	if err != nil {
		return err
	}
	r.Status = "canceling"
	r.Message = "Build cancellation requested"
	if !r.QueueExecuting {
		r.Status = "canceled"
		r.CompletedAt = now
		r.CanceledAt = now
		if err := discardBuild(ctx, tx, r); err != nil {
			return err
		}
	}
	return saveBuild(ctx, tx, *r)
}
func (s *Store) EnqueueBuild(ctx context.Context, token string, submission builddomain.Submission) (builddomain.Run, error) {
	r := submission.Run
	if s.writer == nil {
		return r, ErrReadOnly
	}
	err := s.transaction(ctx, s.writer, false, func(tx *sql.Tx) error {
		if err := checkManagement(ctx, tx, token); err != nil {
			return err
		}
		if r.ID == "" || r.Source == "" || r.ProjectKey == "" || r.BuildNode == "" {
			return builddomain.ErrInput
		}
		if r.RetryOf != "" {
			parent, err := readBuild(ctx, tx, r.RetryOf)
			if err != nil {
				return err
			}
			if !builddomain.Terminal(parent.Status) || parent.Mode == "local" {
				return builddomain.ErrState
			}
			r.RetryRootID = parent.RetryRootID
			if r.RetryRootID == "" {
				r.RetryRootID = parent.ID
			}
		}
		active, err := activeBuilds(ctx, tx)
		if err != nil {
			return err
		}
		if !submission.Request.Queue {
			for _, other := range active {
				if other.ProjectKey == r.ProjectKey && (other.Status == "queued" || other.HoldsProject()) {
					return fmt.Errorf("%w: another project build is active; use queue", builddomain.ErrState)
				}
			}
		}
		var order int64
		if err := tx.QueryRowContext(ctx, `SELECT COALESCE(MAX(CAST(json_extract(payload,'$.queueOrder') AS INTEGER)),0)+1 FROM control_entities WHERE kind='buildRuns'`).Scan(&order); err != nil {
			return err
		}
		r.Status = "queued"
		r.Mode = "builder"
		r.GoBuild = true
		r.QueueManaged = true
		r.QueueOrder = order
		r.CreatedAt = time.Now().Unix()
		r.UpdatedAt = r.CreatedAt
		r.Request = submission.Request.Public()
		r.Message = "Waiting in project deployment queue"
		if r.ReplacementKey != "" && submission.Request.Queue {
			for _, other := range active {
				if !other.GoBuild || other.ReplacementKey != r.ReplacementKey {
					continue
				}
				other.SupersededBy = r.ID
				if err := requestBuildCancel(ctx, tx, &other); err != nil {
					return err
				}
			}
		}
		queue, err := privateBuildQueue(ctx, tx)
		if err != nil {
			return err
		}
		if err := queue.set(r.ID, map[string]any{"kind": "remote", "body": submission.Request, "goBuild": true}); err != nil {
			return err
		}
		if err := writeBuildQueue(ctx, tx, queue); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `INSERT OR IGNORE INTO control_collections(kind,shape) VALUES('buildRuns','dict')`); err != nil {
			return err
		}
		raw, err := json.Marshal(r)
		if err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx, `INSERT INTO control_entities(kind,id,payload,status,app,created_at,updated_at) VALUES('buildRuns',?,?,?,?,?,?)`, r.ID, string(raw), r.Status, buildApp(r), r.CreatedAt, r.UpdatedAt)
		return err
	})
	return r, err
}
func (s *Store) ClaimBuild(ctx context.Context) (*builddomain.Work, error) {
	if s.writer == nil {
		return nil, ErrReadOnly
	}
	var work *builddomain.Work
	err := s.transaction(ctx, s.writer, false, func(tx *sql.Tx) error {
		active, err := activeBuilds(ctx, tx)
		if err != nil {
			return err
		}
		for i := range active {
			r := &active[i]
			if r.GoBuild && r.Mode == "local" && !r.QueueManaged && r.Status == "running" && r.ExpiresAt <= time.Now().Unix() {
				r.Status = "failed"
				r.Message = "Local build upload reservation expired"
				r.UpdatedAt = time.Now().Unix()
				r.CompletedAt = r.UpdatedAt
				if err := discardBuild(ctx, tx, r); err != nil {
					return err
				}
				if err := saveBuild(ctx, tx, *r); err != nil {
					return err
				}
			}
			if !r.GoBuild || !r.QueueOwnerReleased {
				continue
			}
			stopped, err := stopBuildChild(ctx, tx, *r)
			if err != nil {
				return err
			}
			if stopped {
				if err := discardBuild(ctx, tx, r); err != nil {
					return err
				}
				if err := saveBuild(ctx, tx, *r); err != nil {
					return err
				}
			}
		}
		queue, err := privateBuildQueue(ctx, tx)
		if err != nil {
			return err
		}
		for _, r := range active {
			if !builddomain.CanClaim(r, active) {
				continue
			}
			entry := queue.object(r.ID)
			var request builddomain.ControlRequest
			if !entry.boolean("goBuild") || json.Unmarshal(entry["body"], &request) != nil {
				return fmt.Errorf("private Go build request missing")
			}
			r.Status = "running"
			r.QueueExecuting = true
			r.UpdatedAt = time.Now().Unix()
			r.Message = "Executing queued build"
			if err := saveBuild(ctx, tx, r); err != nil {
				return err
			}
			work = &builddomain.Work{Run: r, Request: request}
			if r.Mode == "local" {
				var built builddomain.ImageResult
				if entry.text("kind") != "local" || json.Unmarshal(entry["buildResult"], &built) != nil {
					return builddomain.ErrState
				}
				work.LocalResult = &built
			}
			break
		}
		return nil
	})
	return work, err
}
func (s *Store) ReadBuild(ctx context.Context, id string) (builddomain.Run, error) {
	var r builddomain.Run
	err := s.snapshot(ctx, func(tx *sql.Tx) error { var err error; r, err = readBuild(ctx, tx, id); return err })
	return r, err
}
func (s *Store) CancelBuild(ctx context.Context, token, id string) (builddomain.Run, error) {
	var r builddomain.Run
	if s.writer == nil {
		return r, ErrReadOnly
	}
	err := s.transaction(ctx, s.writer, false, func(tx *sql.Tx) error {
		if err := checkManagement(ctx, tx, token); err != nil {
			return err
		}
		var err error
		r, err = readBuild(ctx, tx, id)
		if err != nil {
			return err
		}
		if !r.GoBuild {
			return node.ErrActionNotMigrated
		}
		return requestBuildCancel(ctx, tx, &r)
	})
	return r, err
}
func (s *Store) BeginBuildDeployment(ctx context.Context, id string) error {
	if s.writer == nil {
		return ErrReadOnly
	}
	return s.transaction(ctx, s.writer, false, func(tx *sql.Tx) error {
		r, err := readBuild(ctx, tx, id)
		if err != nil {
			return err
		}
		if !r.GoBuild || !r.QueueExecuting || r.Status != "running" || r.CancelRequestedAt > 0 {
			return builddomain.ErrState
		}

		if r.Mode == "local" {
			queue, err := privateBuildQueue(ctx, tx)
			if err != nil {
				return err
			}
			if queue.object(id).text("kind") != "local" {
				return builddomain.ErrState
			}
			r.Status = "finalizing"
			r.UpdatedAt = time.Now().Unix()
			return saveBuild(ctx, tx, r)
		}
		var raw []byte
		if err := tx.QueryRowContext(ctx, `SELECT payload FROM control_entities WHERE kind='agentTasks' AND id=?`, r.AgentTaskID).Scan(&raw); err != nil {
			return err
		}
		var child node.StoredTask
		if json.Unmarshal(raw, &child) != nil || child.Status != "succeeded" {
			return builddomain.ErrState
		}
		if err := buildTaskOwner(ctx, tx, child, true); err != nil {
			return err
		}
		r.Status = "finalizing"
		r.UpdatedAt = time.Now().Unix()
		return saveBuild(ctx, tx, r)
	})
}
func (s *Store) FinishBuild(ctx context.Context, id, status string, result map[string]any, message string) error {
	if s.writer == nil {
		return ErrReadOnly
	}
	return s.transaction(ctx, s.writer, false, func(tx *sql.Tx) error {
		r, err := readBuild(ctx, tx, id)
		if err != nil {
			return err
		}
		if !r.GoBuild || !builddomain.Terminal(status) {
			return builddomain.ErrState
		}
		if builddomain.Terminal(r.Status) {
			return nil
		}
		if r.CancelRequestedAt > 0 {
			status = "canceled"
			message = "Build canceled"
		}
		r.Status = status
		r.Message = message
		r.UpdatedAt = time.Now().Unix()
		r.CompletedAt = r.UpdatedAt
		if status == "canceled" {
			r.CanceledAt = r.UpdatedAt
			r.Result = nil
		} else if status == "succeeded" {
			r.Result = result
		} else {
			r.Result = nil
		}
		stopped, err := stopBuildChild(ctx, tx, r)
		if err != nil {
			return err
		}
		r.QueueOwnerReleased = true
		if stopped {
			if err := discardBuild(ctx, tx, &r); err != nil {
				return err
			}
		}
		return saveBuild(ctx, tx, r)
	})
}
func (s *Store) RecoverBuilds(ctx context.Context) error {
	if s.writer == nil {
		return ErrReadOnly
	}
	return s.transaction(ctx, s.writer, false, func(tx *sql.Tx) error {
		active, err := activeBuilds(ctx, tx)
		if err != nil {
			return err
		}
		for _, r := range active {
			if !r.GoBuild || r.Status == "queued" || r.Mode == "local" && r.Status == "running" && !r.QueueManaged && r.ExpiresAt > time.Now().Unix() {
				continue
			}
			if !builddomain.Terminal(r.Status) {
				r.Status = "failed"
				if r.CancelRequestedAt > 0 {
					r.Status = "canceled"
				}
				r.Message = "Control restarted during build; interrupted work is not replayed"
				r.UpdatedAt = time.Now().Unix()
				r.CompletedAt = r.UpdatedAt
			}
			r.QueueOwnerReleased = true
			stopped, err := stopBuildChild(ctx, tx, r)
			if err != nil {
				return err
			}
			if stopped {
				if err := discardBuild(ctx, tx, &r); err != nil {
					return err
				}
			}
			if err := saveBuild(ctx, tx, r); err != nil {
				return err
			}
		}
		return nil
	})
}
func (s *Store) AttachBuildTask(ctx context.Context, id string, payload json.RawMessage) (string, error) {
	if s.writer == nil {
		return "", ErrReadOnly
	}
	var nonce [16]byte
	if _, err := rand.Read(nonce[:]); err != nil {
		return "", err
	}
	taskID := "agent-" + hex.EncodeToString(nonce[:])
	var request builddomain.ImageRequest
	d, err := readDocument(payload)
	if err != nil {
		return "", builddomain.ErrInput
	}
	allowed := []string{"repoUrl", "ref", "registryHost", "pushHost", "repo", "proxy", "gitProviderId", "buildTimeout", "context", "dockerfile", "platform", "region", "composeSidecar", "targetPlatformsByNode", "targetPlatformsByRegion"}
	for key := range d {
		if !slices.Contains(allowed, key) {
			return "", builddomain.ErrInput
		}
	}
	if json.Unmarshal(payload, &request) != nil {
		return "", builddomain.ErrInput
	}
	request, err = request.Validate()
	if err != nil {
		return "", fmt.Errorf("%w: %s", builddomain.ErrInput, err)
	}
	payload, err = json.Marshal(request)
	if err != nil {
		return "", err
	}
	err = s.transaction(ctx, s.writer, false, func(tx *sql.Tx) error {
		r, err := readBuild(ctx, tx, id)
		if err != nil {
			return err
		}
		if !r.GoBuild || !r.QueueExecuting || r.Status != "running" || r.AgentTaskID != "" || r.CancelRequestedAt > 0 {
			return builddomain.ErrState
		}
		var raw []byte
		if err := tx.QueryRowContext(ctx, `SELECT payload FROM control_entities WHERE kind='nodes' AND id=?`, r.BuildNode).Scan(&raw); err != nil {
			return err
		}
		n, err := readDocument(raw)
		if err != nil {
			return err
		}
		a := n.object("agent")
		ready := a.text("status") == "ready" || a.text("status") == "online" && time.Now().Unix()-a.number("lastSeen") <= 120
		if !ready || a.text("tokenHash") == "" || !slices.Contains(a.strings("capabilities"), "docker-build") {
			return node.ErrTaskState
		}
		if _, ok := a["supportedActions"]; ok && !slices.Contains(a.strings("supportedActions"), "build-image") {
			return node.ErrActionNotMigrated
		}
		now := time.Now().Unix()
		task := node.StoredTask{ID: taskID, NodeName: r.BuildNode, Action: "build-image", RequiredCapability: "docker-build", Status: "queued", BuildRunID: r.ID, GoBuild: true, Payload: payload, CreatedAt: now, UpdatedAt: now}
		raw, err = json.Marshal(task)
		if err != nil {
			return err
		}
		d, err := readDocument(raw)
		if err != nil {
			return err
		}
		_ = d.set("goBuild", true)
		raw, err = json.Marshal(d)
		if err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `INSERT OR IGNORE INTO control_collections(kind,shape) VALUES('agentTasks','dict')`); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO control_entities(kind,id,payload,status,created_at,updated_at) VALUES('agentTasks',?,?,'queued',?,?)`, taskID, string(raw), now, now); err != nil {
			return err
		}
		r.AgentTaskID = taskID
		r.UpdatedAt = now
		return saveBuild(ctx, tx, r)
	})
	return taskID, err
}
func appendBuildEvents(ctx context.Context, tx *sql.Tx, id string, events []builddomain.Event) error {
	r, err := readBuild(ctx, tx, id)
	if err != nil {
		return err
	}
	if !r.GoBuild || builddomain.Terminal(r.Status) {
		return nil
	}
	if _, err := tx.ExecContext(ctx, `INSERT OR IGNORE INTO control_event_streams(kind,entity_id,stream) VALUES('buildRuns',?,'events')`, id); err != nil {
		return err
	}
	var position int64
	if err := tx.QueryRowContext(ctx, `SELECT COALESCE(MAX(position),-1) FROM control_events WHERE kind='buildRuns' AND entity_id=? AND stream='events'`, id).Scan(&position); err != nil {
		return err
	}
	for _, event := range events {
		event.Timestamp = time.Now().Unix()
		if len(event.Message) > 4000 {
			event.Message = event.Message[:4000]
		}
		raw, err := json.Marshal(event)
		if err != nil {
			return err
		}
		position++
		if _, err := tx.ExecContext(ctx, `INSERT INTO control_events(kind,entity_id,stream,position,payload) VALUES('buildRuns',?,'events',?,?)`, id, position, string(raw)); err != nil {
			return err
		}
	}
	if _, err := tx.ExecContext(ctx, `DELETE FROM control_events WHERE kind='buildRuns' AND entity_id=? AND stream='events' AND position NOT IN (SELECT position FROM control_events WHERE kind='buildRuns' AND entity_id=? AND stream='events' ORDER BY position DESC LIMIT 300)`, id, id); err != nil {
		return err
	}
	r.UpdatedAt = time.Now().Unix()
	return saveBuild(ctx, tx, r)
}
func (s *Store) BuildEvent(ctx context.Context, id string, event builddomain.Event) error {
	if s.writer == nil {
		return ErrReadOnly
	}
	return s.transaction(ctx, s.writer, false, func(tx *sql.Tx) error { return appendBuildEvents(ctx, tx, id, []builddomain.Event{event}) })
}
