package controlstore

import (
	"context"
	"database/sql"
	"encoding/json"

	"github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/history"
)

func queueEntry(raw []byte) (build.QueueEntry, error) {
	var data struct {
		ID        string  `json:"id"`
		Status    string  `json:"status"`
		Mode      string  `json:"mode"`
		Project   *string `json:"projectKey"`
		Order     int64   `json:"queueOrder"`
		Executing bool    `json:"queueExecuting"`
		Managed   bool    `json:"queueManaged"`
		Request   struct {
			Queue bool `json:"queue"`
		} `json:"request"`
	}
	if json.Unmarshal(raw, &data) != nil {
		return build.QueueEntry{}, history.ErrStoredRecord
	}
	return build.QueueEntry{ID: data.ID, Status: data.Status, Mode: data.Mode, Project: data.Project, Order: data.Order, Executing: data.Executing, Managed: data.Managed, CallerQueued: data.Request.Queue}, nil
}

func queuePosition(ctx context.Context, tx *sql.Tx, raw []byte) (*build.Position, error) {
	current, err := queueEntry(raw)
	if err != nil {
		return nil, err
	}
	projection := build.NewPositionCounter(current)
	var project any
	if current.Project != nil {
		project = *current.Project
	}
	// The status index limits this to active builds. Only one project's records
	// are streamed, and event/private buildQueue payloads are never materialized.
	rows, err := tx.QueryContext(ctx, `SELECT payload FROM control_entities WHERE kind='buildRuns'
	 AND status IN ('queued','running','canceling','finalizing') AND json_extract(payload,'$.projectKey') IS ?`, project)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var payload []byte
		if err := rows.Scan(&payload); err != nil {
			return nil, err
		}
		entry, err := queueEntry(payload)
		if err != nil {
			return nil, err
		}
		projection.Add(entry)
	}
	return projection.Result(), rows.Err()
}
