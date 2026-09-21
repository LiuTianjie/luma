package controlstore

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"

	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/credentials"
	"github.com/LiuTianjie/luma/internal/node"
	"github.com/LiuTianjie/luma/internal/observability"
)

type document map[string]json.RawMessage

func readDocument(raw []byte) (document, error) {
	var d document
	if json.Unmarshal(raw, &d) != nil || d == nil {
		return nil, errors.New("invalid stored Control record")
	}
	return d, nil
}
func (d document) text(key string) string  { var s string; _ = json.Unmarshal(d[key], &s); return s }
func (d document) number(key string) int64 { var n int64; _ = json.Unmarshal(d[key], &n); return n }
func (d document) boolean(key string) bool { var b bool; _ = json.Unmarshal(d[key], &b); return b }
func (d document) strings(key string) []string {
	var list []string
	if json.Unmarshal(d[key], &list) == nil {
		return list
	}
	if s := d.text(key); s != "" {
		return []string{s}
	}
	return nil
}
func (d document) set(key string, value any) error {
	raw, err := json.Marshal(value)
	if err != nil {
		return err
	}
	d[key] = raw
	return nil
}
func (d document) object(key string) document {
	child, _ := readDocument(d[key])
	if child == nil {
		child = document{}
	}
	return child
}

type agentTx struct {
	ctx      context.Context
	tx       *sql.Tx
	key      string
	record   document
	agent    document
	names    []string
	snapshot node.AgentSnapshot
	tasks    map[string]document
}

func (s *Store) AgentTransaction(ctx context.Context, token string, identity node.Identity, work func(node.AgentTransaction) error) error {
	if s.writer == nil {
		return ErrReadOnly
	}
	return s.transaction(ctx, s.writer, false, func(tx *sql.Tx) error {
		rows, err := tx.QueryContext(ctx, `SELECT id,payload FROM control_entities WHERE kind='nodes' AND json_extract(payload,'$.agent.tokenHash')=? LIMIT 2`, credentials.AgentTokenHash(token))
		if err != nil {
			return err
		}
		var key string
		var raw []byte
		count := 0
		for rows.Next() {
			count++
			if err := rows.Scan(&key, &raw); err != nil {
				rows.Close()
				return err
			}
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
		if count != 1 {
			return credentials.ErrUnauthorized
		}
		record, err := readDocument(raw)
		if err != nil {
			return err
		}
		agent := record.object("agent")
		labels := record.object("labels")
		names := []string{key, record.text("name"), record.text("displayName"), record.text("hostname"), labels.text("luma.node.name"), labels.text("luma_node_name")}
		names = append(names, record.strings("aliases")...)
		ids := []string{record.text("nodeId"), record.text("nomadNodeId"), labels.text("luma.node.id"), agent.text("nodeId"), agent.text("currentNodeId")}
		ids = append(ids, agent.strings("knownNodeIds")...)
		ids = append(ids, agent.strings("legacyNodeIds")...)
		clean := func(values []string) []string {
			out := []string{}
			for _, value := range values {
				value = strings.TrimSpace(value)
				if value != "" && !slices.Contains(out, value) {
					out = append(out, value)
				}
			}
			return out
		}
		names = clean(names)
		ids = clean(ids)
		if err := credentials.AuthenticateAgent(credentials.AgentIdentity{TokenHash: agent.text("tokenHash"), Names: names, IDs: ids}, token, identity.Name, identity.ID); err != nil {
			return err
		}
		if len(names)+len(ids) > 200 {
			return errors.New("too many stored node identities")
		}
		a := &agentTx{ctx: ctx, tx: tx, key: key, record: record, agent: agent, names: clean(append(names, ids...)), tasks: map[string]document{}}
		a.snapshot = node.AgentSnapshot{Name: key, OS: agent.text("os"), Arch: agent.text("arch"), Version: agent.text("version"), Status: agent.text("status"), ActiveTaskID: agent.text("activeTaskId"), Capabilities: agent.strings("capabilities"), JoinVerified: agent.boolean("joinVerified"), LastSeen: agent.number("lastSeen"), ActiveTaskObservedAt: agent.number("activeTaskObservedAt"), MetricsCollectedAt: agent.number("metricsCollectedAt"), ContainerStatsCollectedAt: agent.number("containerStatsCollectedAt")}
		if value, exists := agent["supportedActions"]; exists {
			var values []string
			if json.Unmarshal(value, &values) != nil {
				return errors.New("invalid stored action negotiation")
			}
			if values == nil {
				values = []string{}
			}
			a.snapshot.SupportedActions = &values
		}
		if value, exists := agent["metrics"]; exists {
			var metrics node.Metrics
			if json.Unmarshal(value, &metrics) == nil {
				a.snapshot.Metrics = &metrics
			}
		}
		if value, exists := agent["containerStats"]; exists {
			var stats []observability.ContainerStat
			if json.Unmarshal(value, &stats) == nil {
				a.snapshot.ContainerStats = &stats
			}
		}
		return work(a)
	})
}

func (a *agentTx) Agent() node.AgentSnapshot { return a.snapshot }
func (a *agentTx) SaveAgent(s node.AgentSnapshot) error {
	values := map[string]any{"os": s.OS, "arch": s.Arch, "version": s.Version, "status": s.Status, "capabilities": s.Capabilities, "joinVerified": s.JoinVerified, "lastSeen": s.LastSeen}
	if s.ActiveTaskObservedAt != 0 {
		values["activeTaskId"] = s.ActiveTaskID
		values["activeTaskObservedAt"] = s.ActiveTaskObservedAt
	}
	if s.SupportedActions != nil {
		values["supportedActions"] = *s.SupportedActions
	} else {
		delete(a.agent, "supportedActions")
	}
	if s.Metrics != nil {
		values["metrics"] = s.Metrics
		values["metricsCollectedAt"] = s.MetricsCollectedAt
	}
	if s.ContainerStats != nil {
		values["containerStats"] = *s.ContainerStats
		values["containerStatsCollectedAt"] = s.ContainerStatsCollectedAt
	}
	for key, value := range values {
		if err := a.agent.set(key, value); err != nil {
			return err
		}
	}
	if err := a.record.set("agent", a.agent); err != nil {
		return err
	}
	raw, err := json.Marshal(a.record)
	if err != nil {
		return err
	}
	_, err = a.tx.ExecContext(a.ctx, `UPDATE control_entities SET payload=? WHERE kind='nodes' AND id=?`, string(raw), a.key)
	if err == nil {
		a.snapshot = s
	}
	return err
}
func (a *agentTx) taskFrom(raw []byte, id string) (node.StoredTask, error) {
	var task node.StoredTask
	if json.Unmarshal(raw, &task) != nil || task.ID != id || task.ID == "" {
		return task, errors.New("invalid stored agent task")
	}
	if !slices.Contains(a.names, task.NodeName) {
		return task, node.ErrTaskNotFound
	}
	doc, err := readDocument(raw)
	if err != nil {
		return task, err
	}
	if task.GoBuild {
		if err := buildTaskOwner(a.ctx, a.tx, task, false); err != nil {
			if errors.Is(err, node.ErrTaskState) || errors.Is(err, node.ErrActionNotMigrated) || errors.Is(err, builddomain.ErrNotFound) {
				task.GoBuild = false
			} else {
				return task, err
			}
		}
	}
	a.tasks[id] = doc
	return task, nil
}
func (a *agentTx) Tasks(status, after string) ([]node.StoredTask, error) {
	// Saves apply within the currently visited page. Do not retain every skipped
	// queued task in memory when a node has a large unsupported backlog.
	a.tasks = map[string]document{}
	args := []any{status, after}
	placeholders := make([]string, len(a.names))
	for i, name := range a.names {
		placeholders[i] = "?"
		args = append(args, name)
	}
	rows, err := a.tx.QueryContext(a.ctx, `SELECT id,payload FROM control_entities WHERE kind='agentTasks' AND status=? AND id>? AND json_extract(payload,'$.nodeName') IN (`+strings.Join(placeholders, ",")+`) ORDER BY id LIMIT 100`, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	tasks := []node.StoredTask{}
	for rows.Next() {
		var id string
		var raw []byte
		if err := rows.Scan(&id, &raw); err != nil {
			return nil, err
		}
		task, err := a.taskFrom(raw, id)
		if err != nil {
			return nil, err
		}
		tasks = append(tasks, task)
	}
	return tasks, rows.Err()
}
func (a *agentTx) Task(id string) (node.StoredTask, error) {
	var raw []byte
	err := a.tx.QueryRowContext(a.ctx, `SELECT payload FROM control_entities WHERE kind='agentTasks' AND id=?`, id).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return node.StoredTask{}, node.ErrTaskNotFound
	}
	if err != nil {
		return node.StoredTask{}, err
	}
	return a.taskFrom(raw, id)
}
func (a *agentTx) SaveTask(task node.StoredTask) error {
	doc, ok := a.tasks[task.ID]
	if !ok || !slices.Contains(a.names, task.NodeName) {
		return node.ErrTaskNotFound
	}
	values := map[string]any{"status": task.Status, "updatedAt": task.UpdatedAt, "progressOffset": task.ProgressOffset}
	if task.LeasedAt > 0 {
		values["leasedAt"] = task.LeasedAt
	}
	if task.CompletedAt > 0 {
		values["completedAt"] = task.CompletedAt
		values["message"] = task.Message
		values["result"] = task.Result
	}
	if task.CancelRequestedAt > 0 {
		values["cancelRequestedAt"] = task.CancelRequestedAt
	}
	for key, value := range values {
		if err := doc.set(key, value); err != nil {
			return err
		}
	}
	raw, err := json.Marshal(doc)
	if err != nil {
		return err
	}
	_, err = a.tx.ExecContext(a.ctx, `UPDATE control_entities SET payload=?,status=?,updated_at=? WHERE kind='agentTasks' AND id=?`, string(raw), task.Status, task.UpdatedAt, task.ID)
	return err
}
func (a *agentTx) AppendProgress(task *node.StoredTask, events []node.Event, limit int) error {
	if _, exists := a.tasks[task.ID]; !exists {
		return node.ErrTaskNotFound
	}
	if _, err := a.tx.ExecContext(a.ctx, `INSERT OR IGNORE INTO control_event_streams(kind,entity_id,stream) VALUES('agentTasks',?,'progress')`, task.ID); err != nil {
		return err
	}
	var position, count int64
	if err := a.tx.QueryRowContext(a.ctx, `SELECT COALESCE(MAX(position),-1),COUNT(*) FROM control_events WHERE kind='agentTasks' AND entity_id=? AND stream='progress'`, task.ID).Scan(&position, &count); err != nil {
		return err
	}
	for _, event := range events {
		position++
		raw, err := json.Marshal(event)
		if err != nil {
			return err
		}
		if _, err := a.tx.ExecContext(a.ctx, `INSERT INTO control_events(kind,entity_id,stream,position,payload) VALUES('agentTasks',?,'progress',?,?)`, task.ID, position, string(raw)); err != nil {
			return err
		}
	}
	dropped := max(0, count+int64(len(events))-int64(limit))
	if dropped > 0 {
		if _, err := a.tx.ExecContext(a.ctx, `DELETE FROM control_events WHERE kind='agentTasks' AND entity_id=? AND stream='progress' AND position IN (SELECT position FROM control_events WHERE kind='agentTasks' AND entity_id=? AND stream='progress' ORDER BY position LIMIT ?)`, task.ID, task.ID, dropped); err != nil {
			return err
		}
		task.ProgressOffset = max(0, task.ProgressOffset) + dropped
	}
	if task.GoBuild && len(events) > 0 {
		if err := buildTaskOwner(a.ctx, a.tx, *task, false); err != nil {
			return err
		}
		log := make([]builddomain.Event, 0, len(events))
		for _, event := range events {
			log = append(log, builddomain.Event{Name: "Build image", Status: "output", Message: event.Line})
		}
		return appendBuildEvents(a.ctx, a.tx, task.BuildRunID, log)
	}
	return nil
}

func (a *agentTx) LeasePayload(task node.StoredTask) (json.RawMessage, error) {
	payload, err := readDocument(task.Payload)
	if err != nil {
		return nil, fmt.Errorf("invalid agent task payload")
	}
	// Credentials are resolved from the registry catalog only for this response.
	// Reject old injected copies rather than perpetuating a durable secret leak.
	for _, key := range []string{"registryAuth", "sourceRegistryAuth", "destinationRegistryAuth", "gitToken", "objectUrl", "tailscaleAuthKey"} {
		if _, exists := payload[key]; exists {
			return nil, errors.New("durable task contains ephemeral credentials; migrate the task before leasing")
		}
	}
	if task.Action == "build-image" {
		return a.buildLeasePayload(task, payload)
	}
	mapping := map[string]string{}
	switch task.Action {
	case "resolve-docker-image", "diagnose-docker-pull":
		mapping["registryAuth"] = payload.text("image")
	case "mirror-control-image", "mirror-system-image":
		mapping["registryAuth"] = payload.text("pushImage")
	case "cache-runtime-image":
		mapping["sourceRegistryAuth"] = payload.text("sourceImage")
		mapping["destinationRegistryAuth"] = payload.text("pushImage")
	}
	if len(mapping) > 0 {
		var raw []byte
		err := a.tx.QueryRowContext(a.ctx, `SELECT payload FROM control_config WHERE key='registries'`).Scan(&raw)
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return nil, err
		}
		registries := document{}
		if err == nil {
			registries, err = readDocument(raw)
			if err != nil {
				return nil, err
			}
		}
		for key, image := range mapping {
			auth, err := registryAuth(registries, image)
			if err != nil {
				return nil, err
			}
			if auth != nil {
				if err := payload.set(key, auth); err != nil {
					return nil, err
				}
			}
		}
	}
	return json.Marshal(payload)
}
