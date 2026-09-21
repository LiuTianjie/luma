package controlstore

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"slices"
	"time"

	"github.com/LiuTianjie/luma/internal/deployment"
	"github.com/LiuTianjie/luma/internal/node"
)

func checkTaskOwner(ctx context.Context, tx *sql.Tx, owner node.TaskOwner, pending bool) error {
	d, err := configDocument(ctx, tx, "deployments", 32<<20)
	if err != nil {
		return err
	}
	if owner.OperationID == "" || owner.Kind != "service" && owner.Kind != "compose" {
		return deployment.ErrConflict
	}
	r := d.object(deploymentBucket(owner.Kind)).object(owner.Slug)
	if r.text("operationId") != owner.OperationID || pending && r.text("status") != "pending" {
		return deployment.ErrConflict
	}
	return nil
}
func (s *Store) CreateCommandTask(ctx context.Context, token string, owner node.TaskOwner, task node.StoredTask) error {
	if s.writer == nil {
		return ErrReadOnly
	}
	capability := ""
	var fields []string
	switch task.Action {
	case "cache-runtime-image":
		capability = "runtime-image-cache-v1"
		fields = []string{"sourceImage", "pushImage", "destinationImage", "platform", "proxy", "insecure", "timeout"}
	case "resolve-docker-image":
		capability = "docker-image"
		fields = []string{"image", "forcePull", "platform", "timeout"}
	case "repair-nomad-cni-hostports":
		capability = "nomad-cni-repair"
		fields = []string{"ports"}
	case "prepare-managed-volume-path":
		capability = "managed-volume-path"
		fields = []string{"root", "relative", "preserveExisting"}
	}
	if capability == "" || task.RequiredCapability != capability || task.Status != "queued" || task.ID == "" {
		return node.ErrControlInput
	}
	payload, err := readDocument(task.Payload)
	if err != nil {
		return err
	}
	for key := range payload {
		if !slices.Contains(fields, key) {
			return node.ErrControlInput
		}
	}
	return s.transaction(ctx, s.writer, false, func(tx *sql.Tx) error {
		if err := checkManagement(ctx, tx, token); err != nil {
			return err
		}
		if err := checkTaskOwner(ctx, tx, owner, true); err != nil {
			return err
		}
		var raw []byte
		if err := tx.QueryRowContext(ctx, `SELECT payload FROM control_entities WHERE kind='nodes' AND id=?`, task.NodeName).Scan(&raw); err != nil {
			return err
		}
		record, err := readDocument(raw)
		if err != nil {
			return err
		}
		a := record.object("agent")
		ready := a.text("status") == "ready" || a.text("status") == "online" && time.Now().Unix()-a.number("lastSeen") <= 120
		if !ready || a.text("tokenHash") == "" || !slices.Contains(a.strings("capabilities"), task.RequiredCapability) {
			return node.ErrTaskState
		}
		if _, exists := a["supportedActions"]; exists && !slices.Contains(a.strings("supportedActions"), task.Action) {
			return node.ErrActionNotMigrated
		}
		raw, err = json.Marshal(task)
		if err != nil {
			return err
		}
		d, err := readDocument(raw)
		if err != nil {
			return err
		}
		for key, value := range map[string]string{"deploymentOperationId": owner.OperationID, "deploymentKind": owner.Kind, "deploymentSlug": owner.Slug} {
			if err := d.set(key, value); err != nil {
				return err
			}
		}
		raw, err = json.Marshal(d)
		if err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `INSERT OR IGNORE INTO control_collections(kind,shape) VALUES('agentTasks','dict')`); err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx, `INSERT INTO control_entities(kind,id,payload,status,created_at,updated_at) VALUES('agentTasks',?,?, 'queued',?,?)`, task.ID, string(raw), task.CreatedAt, task.UpdatedAt)
		return err
	})
}
func commandTask(ctx context.Context, tx *sql.Tx, owner node.TaskOwner, id string) (node.StoredTask, document, error) {
	var raw []byte
	err := tx.QueryRowContext(ctx, `SELECT payload FROM control_entities WHERE kind='agentTasks' AND id=?`, id).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return node.StoredTask{}, nil, node.ErrTaskNotFound
	}
	if err != nil {
		return node.StoredTask{}, nil, err
	}
	d, err := readDocument(raw)
	if err != nil {
		return node.StoredTask{}, nil, err
	}
	if d.text("deploymentOperationId") != owner.OperationID || d.text("deploymentKind") != owner.Kind || d.text("deploymentSlug") != owner.Slug {
		return node.StoredTask{}, nil, node.ErrTaskNotFound
	}
	var task node.StoredTask
	if json.Unmarshal(raw, &task) != nil {
		return task, nil, node.ErrControlInput
	}
	return task, d, nil
}
func (s *Store) CommandTask(ctx context.Context, owner node.TaskOwner, id string) (node.StoredTask, error) {
	var result node.StoredTask
	err := s.snapshot(ctx, func(tx *sql.Tx) error { var err error; result, _, err = commandTask(ctx, tx, owner, id); return err })
	return result, err
}
func cancelCommandTask(ctx context.Context, tx *sql.Tx, owner node.TaskOwner, id string) error {
	task, d, err := commandTask(ctx, tx, owner, id)
	if err != nil {
		return err
	}
	if node.Terminal(task.Status) {
		return nil
	}
	now := time.Now().Unix()
	_ = d.set("cancelRequestedAt", now)
	_ = d.set("updatedAt", now)
	if task.Status == "queued" {
		task.Status = "canceled"
		_ = d.set("status", task.Status)
		_ = d.set("completedAt", now)
		_ = d.set("message", "owning deployment ended before task execution")
		_ = d.set("result", map[string]any{})
	}
	raw, err := json.Marshal(d)
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `UPDATE control_entities SET payload=?,status=?,updated_at=? WHERE kind='agentTasks' AND id=?`, string(raw), task.Status, now, id)
	return err
}
func (s *Store) CancelCommandTask(ctx context.Context, owner node.TaskOwner, id string) error {
	if s.writer == nil {
		return ErrReadOnly
	}
	return s.transaction(ctx, s.writer, false, func(tx *sql.Tx) error { return cancelCommandTask(ctx, tx, owner, id) })
}

func cancelDeploymentTasks(ctx context.Context, tx *sql.Tx, r deployment.Record) error {
	rows, err := tx.QueryContext(ctx, `SELECT id FROM control_entities WHERE kind='agentTasks' AND status IN ('queued','running') AND json_extract(payload,'$.deploymentOperationId')=?`, r.OperationID)
	if err != nil {
		return err
	}
	ids := []string{}
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return err
		}
		ids = append(ids, id)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	for _, id := range ids {
		if err := cancelCommandTask(ctx, tx, node.TaskOwner{Kind: r.Kind, Slug: r.Slug, OperationID: r.OperationID}, id); err != nil {
			return err
		}
	}
	return nil
}
