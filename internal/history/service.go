package history

import (
	"bytes"
	"context"
	"encoding/json"
	"strconv"

	"github.com/LiuTianjie/luma/internal/build"
)

type Row struct {
	ID, Kind, App, Status                      string
	CreatedAt, UpdatedAt, Insertion, StepCount int64
	Payload                                    json.RawMessage
}
type Event struct {
	Position int64
	Payload  json.RawMessage
}
type ListResult struct {
	Rows      []Row
	Watermark int64
}
type DetailResult struct {
	Row     Row
	Events  []Event
	Through int64
	Queue   *build.Position
}

// Repository executes each read in one database snapshot, returning at most
// limit+1 records. It must not hydrate all Control state or unrelated streams.
type Repository interface {
	List(context.Context, ListQuery) (ListResult, error)
	Detail(context.Context, DetailQuery) (DetailResult, error)
}

type Page struct {
	Limit       int     `json:"limit"`
	NextCursor  *string `json:"nextCursor"`
	HasMore     bool    `json:"hasMore"`
	ResumeAfter *int64  `json:"resumeAfter,omitempty"`
}

type Service struct{ Repository Repository }

// List returns timeline, legacy build-list or legacy deployment-list envelopes.
func (s Service) List(ctx context.Context, values map[string]string, view string) (map[string]any, error) {
	fixedKind := ""
	if view == "runs" {
		fixedKind = "build"
	}
	if view == "events" {
		fixedKind = "deployment"
	}
	q, err := ParseList(values, fixedKind, view == "events")
	if err != nil {
		return nil, err
	}
	result, err := s.Repository.List(ctx, q)
	if err != nil {
		return nil, err
	}
	page := Page{Limit: q.Limit, HasMore: len(result.Rows) > q.Limit}
	rows := result.Rows
	if page.HasMore {
		rows = rows[:q.Limit]
		last := rows[len(rows)-1]
		key := []any{last.CreatedAt, last.ID, last.Kind}
		if q.InsertionOrder {
			key = []any{last.CreatedAt, last.Insertion}
		}
		cursor := encodeCursor(map[string]any{"v": 1, "scope": q.Scope, "watermark": result.Watermark, "key": key})
		page.NextCursor = &cursor
	}
	items := make([]map[string]any, 0, len(rows))
	for _, row := range rows {
		data, err := payload(row.Payload)
		if err != nil {
			return nil, err
		}
		switch view {
		case "runs":
			items = append(items, buildSummary(data))
		case "events":
			delete(data, "steps")
			items = append(items, data)
		default:
			items = append(items, summary(row, data))
		}
	}
	key := "items"
	if view == "runs" || view == "events" {
		key = view
	}
	return map[string]any{key: items, "page": page}, nil
}

func (s Service) Detail(ctx context.Context, kind, id string, values map[string]string, legacy bool) (map[string]any, error) {
	q, err := ParseDetail(kind, id, values)
	if err != nil {
		return nil, err
	}
	q.IncludeQueuePosition = legacy && kind == "build"
	result, err := s.Repository.Detail(ctx, q)
	if err != nil {
		return nil, err
	}
	page := Page{Limit: q.Limit, HasMore: len(result.Events) > q.Limit}
	events := result.Events
	if page.HasMore {
		events = events[:q.Limit]
	}
	after := q.After
	if len(events) > 0 {
		after = events[len(events)-1].Position
	}
	page.ResumeAfter = &after
	if page.HasMore {
		cursor := encodeCursor(map[string]any{"v": 1, "scope": q.Scope, "after": after, "through": result.Through})
		page.NextCursor = &cursor
	}
	entries := make([]map[string]any, 0, len(events))
	for _, event := range events {
		data, err := payload(event.Payload)
		if err != nil {
			return nil, err
		}
		entries = append(entries, data)
	}
	data, err := payload(result.Row.Payload)
	if err != nil {
		return nil, err
	}
	item := summary(result.Row, data)
	record := data
	if kind == "build" {
		record = buildSummary(data)
		record["message"] = truncate(text(data["message"]), 4000)
		record["request"] = object(data["request"])
		record["result"] = object(data["result"])
	} else {
		delete(record, "steps")
	}
	if legacy {
		if kind == "build" {
			record["events"] = entries
			if result.Queue != nil {
				record["queuePosition"] = result.Queue.QueuePosition
				record["waitingFor"] = result.Queue.WaitingFor
			}
			return map[string]any{"run": record, "eventsPage": page}, nil
		}
		record["steps"] = entries
		return map[string]any{"event": record, "stepsPage": page}, nil
	}
	return map[string]any{"item": item, "record": record, "events": entries, "page": page}, nil
}

func payload(raw json.RawMessage) (map[string]any, error) {
	var data map[string]any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if !json.Valid(raw) || decoder.Decode(&data) != nil || data == nil {
		return nil, ErrStoredRecord
	}
	delete(data, "__luma_event_streams__")
	return data, nil
}
func object(value any) map[string]any {
	if result, ok := value.(map[string]any); ok && result != nil {
		return result
	}
	return map[string]any{}
}
func text(value any) string {
	switch v := value.(type) {
	case string:
		return v
	case json.Number:
		return string(v)
	case bool:
		if v {
			return "True"
		}
		return ""
	default:
		return ""
	}
}
func number(value any) int64 { n, _ := strconv.ParseInt(text(value), 10, 64); return n }
func first(values ...any) string {
	for _, v := range values {
		if s := text(v); s != "" {
			return s
		}
	}
	return ""
}
func truncate(value string, length int) string {
	runes := []rune(value)
	if len(runes) > length {
		return string(runes[:length])
	}
	return value
}

func buildSummary(data map[string]any) map[string]any {
	request := object(data["request"])
	result := map[string]any{
		"source":         first(data["source"], request["repoUrl"], request["repository"]),
		"buildNode":      first(data["buildNode"], request["buildNode"]),
		"mode":           first(data["mode"], "builder"),
		"message":        truncate(text(data["message"]), 500),
		"queueExecuting": data["queueExecuting"] == true,
	}
	for _, key := range []string{"id", "status", "supersededBy", "projectKey", "retryOf", "retryRootId"} {
		result[key] = text(data[key])
	}
	for _, key := range []string{"providerId", "repository", "ref"} {
		result[key] = text(request[key])
	}
	for _, key := range []string{"createdAt", "updatedAt", "completedAt", "detailsExpiredAt", "detailsRetentionDays"} {
		result[key] = number(data[key])
	}
	return result
}
func summary(row Row, data map[string]any) map[string]any {
	build := row.Kind == "buildRuns"
	request, git := object(data["request"]), object(data["gitSource"])
	repository, ref := first(git["repository"], git["repoUrl"]), text(git["ref"])
	source, buildNode, retryOf, retryRoot := first(data["origin"], "cli"), "", "", ""
	if build {
		repository = first(request["repository"], request["repoUrl"], data["source"])
		ref = text(request["ref"])
		source = "build"
		buildNode = first(data["buildNode"], request["buildNode"])
		retryOf = text(data["retryOf"])
		retryRoot = text(data["retryRootId"])
	}
	return map[string]any{
		"id": row.ID, "kind": PublicKind(row.Kind), "application": row.App, "source": source,
		"status": row.Status, "createdAt": row.CreatedAt, "updatedAt": row.UpdatedAt,
		"title": first(row.App, data["name"], repository, row.ID), "repository": repository, "ref": ref,
		"message": truncate(first(data["message"], data["error"]), 500), "buildNode": buildNode, "stepCount": row.StepCount,
		"retryOf": retryOf, "retryRootId": retryRoot, "detailsExpiredAt": number(data["detailsExpiredAt"]), "detailsRetentionDays": number(data["detailsRetentionDays"]),
	}
}
