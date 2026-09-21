package controlstore

import (
	"context"
	"database/sql"
	"errors"
	"strings"

	"github.com/LiuTianjie/luma/internal/history"
)

const columns = `e.id,e.kind,e.app,e.status,e.created_at,e.updated_at,e.rowid,e.payload,
 (SELECT COUNT(*) FROM control_events s WHERE s.kind=e.kind AND s.entity_id=e.id
 AND s.stream=CASE WHEN e.kind='buildRuns' THEN 'events' ELSE 'steps' END)`

type scanner interface{ Scan(...any) error }

func scanRow(row scanner) (history.Row, error) {
	var r history.Row
	var payload []byte
	err := row.Scan(&r.ID, &r.Kind, &r.App, &r.Status, &r.CreatedAt, &r.UpdatedAt, &r.Insertion, &payload, &r.StepCount)
	r.Payload = payload
	return r, err
}

func (s *Store) List(ctx context.Context, q history.ListQuery) (history.ListResult, error) {
	result := history.ListResult{Rows: []history.Row{}}
	err := s.snapshot(ctx, func(tx *sql.Tx) error {
		if q.Cursor != nil {
			result.Watermark = q.Cursor.Watermark
		} else if err := tx.QueryRowContext(ctx, "SELECT COALESCE(MAX(rowid),0) FROM control_entities").Scan(&result.Watermark); err != nil {
			return err
		}
		conditions := []string{"e.kind IN ('buildRuns','deploymentEvents')", "e.rowid <= ?"}
		args := []any{result.Watermark}
		for _, filter := range []struct{ column, value string }{
			{"e.kind", history.InternalKind(q.Filters.Kind)}, {"e.app", q.Filters.App}, {"e.status", q.Filters.Status},
			{"CASE WHEN e.kind='buildRuns' THEN 'build' ELSE COALESCE(NULLIF(json_extract(e.payload,'$.origin'),''),'cli') END", q.Filters.Source},
		} {
			if filter.value != "" {
				conditions = append(conditions, filter.column+" = ?")
				args = append(args, filter.value)
			}
		}
		if q.Filters.Since != nil {
			conditions = append(conditions, "e.created_at >= ?")
			args = append(args, *q.Filters.Since)
		}
		if q.Filters.Until != nil {
			conditions = append(conditions, "e.created_at <= ?")
			args = append(args, *q.Filters.Until)
		}
		if q.Cursor != nil {
			if q.InsertionOrder {
				conditions = append(conditions, "(e.created_at,e.rowid) < (?,?)")
				args = append(args, q.Cursor.CreatedAt, q.Cursor.Insertion)
			} else {
				conditions = append(conditions, "(e.created_at,e.id,e.kind) < (?,?,?)")
				args = append(args, q.Cursor.CreatedAt, q.Cursor.ID, q.Cursor.Kind)
			}
		}
		order := "e.created_at DESC,e.id DESC,e.kind DESC"
		if q.InsertionOrder {
			order = "e.created_at DESC,e.rowid DESC"
		}
		rows, err := tx.QueryContext(ctx, "SELECT "+columns+" FROM control_entities e WHERE "+strings.Join(conditions, " AND ")+" ORDER BY "+order+" LIMIT ?", append(args, q.Limit+1)...)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			row, err := scanRow(rows)
			if err != nil {
				return err
			}
			result.Rows = append(result.Rows, row)
		}
		return rows.Err()
	})
	return result, err
}

func (s *Store) Detail(ctx context.Context, q history.DetailQuery) (history.DetailResult, error) {
	result := history.DetailResult{Events: []history.Event{}}
	err := s.snapshot(ctx, func(tx *sql.Tx) error {
		kind := history.InternalKind(q.Kind)
		row, err := scanRow(tx.QueryRowContext(ctx, "SELECT "+columns+" FROM control_entities e WHERE e.kind=? AND e.id=?", kind, q.ID))
		if errors.Is(err, sql.ErrNoRows) {
			return history.ErrNotFound
		}
		if err != nil {
			return err
		}
		result.Row = row
		stream := "steps"
		if q.Kind == "build" {
			stream = "events"
		}
		if q.Through != nil {
			result.Through = *q.Through
		} else if err := tx.QueryRowContext(ctx, "SELECT COALESCE(MAX(position),-1) FROM control_events WHERE kind=? AND entity_id=? AND stream=?", kind, q.ID, stream).Scan(&result.Through); err != nil {
			return err
		}
		rows, err := tx.QueryContext(ctx, `SELECT position,payload FROM control_events WHERE kind=? AND entity_id=? AND stream=? AND position>? AND position<=? ORDER BY position LIMIT ?`, kind, q.ID, stream, q.After, result.Through, q.Limit+1)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var event history.Event
			var payload []byte
			if err := rows.Scan(&event.Position, &payload); err != nil {
				return err
			}
			event.Payload = payload
			result.Events = append(result.Events, event)
		}
		if err := rows.Err(); err != nil {
			return err
		}
		if err := rows.Close(); err != nil {
			return err
		}
		if q.IncludeQueuePosition && result.Row.Status == "queued" {
			result.Queue, err = queuePosition(ctx, tx, result.Row.Payload)
			return err
		}
		return nil
	})
	return result, err
}
