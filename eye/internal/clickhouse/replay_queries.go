package clickhouse

import (
	"context"
	"encoding/json"
	"sort"
	"strings"
)

type SessionReplayResponse struct {
	Project      string            `json:"project"`
	SessionID    string            `json:"session_id"`
	ActorID      string            `json:"actor_id"`
	HasDOMReplay bool              `json:"has_dom_replay"`
	SegmentCount int               `json:"segment_count"`
	EventsCount  int               `json:"events_count"`
	StartMS      int64             `json:"start_ms"`
	EndMS        int64             `json:"end_ms"`
	DurationMS   int64             `json:"duration_ms"`
	ViewportW    uint16            `json:"viewport_w"`
	ViewportH    uint16            `json:"viewport_h"`
	PageURL      string            `json:"page_url"`
	Events       []json.RawMessage `json:"events"`
}

type replayEventEnvelope struct {
	Type int   `json:"t"`
	TS   int64 `json:"ts"`
	raw  json.RawMessage
}

func (c *Client) GetSessionReplay(ctx context.Context, project, sessionID string) (*SessionReplayResponse, error) {
	sessionID = strings.TrimSpace(sessionID)
	out := &SessionReplayResponse{
		Project:   project,
		SessionID: sessionID,
		Events:    []json.RawMessage{},
	}
	if sessionID == "" || strings.HasPrefix(sessionID, "actor:") {
		return out, nil
	}

	type segRow struct {
		ActorID         string `json:"actor_id"`
		Seq             uint32 `json:"seq"`
		StartMS         int64  `json:"start_ms"`
		EndMS           int64  `json:"end_ms"`
		HasFullSnapshot uint8  `json:"has_full_snapshot"`
		PageURL         string `json:"page_url"`
		ViewportW       uint16 `json:"viewport_w"`
		ViewportH       uint16 `json:"viewport_h"`
		EventsJSON      string `json:"events_json"`
	}

	rows, err := QueryJSON[segRow](ctx, c, `
SELECT
    actor_id,
    seq,
    start_ms,
    end_ms,
    has_full_snapshot,
    page_url,
    viewport_w,
    viewport_h,
    events_json
FROM {{DB}}.replay_segments
WHERE project = {project:String}
  AND session_id = {sessionID:String}
ORDER BY start_ms ASC, seq ASC
LIMIT 240
`, map[string]string{
		"project":   project,
		"sessionID": sessionID,
	})
	if err != nil {
		return nil, err
	}
	if len(rows) == 0 {
		return out, nil
	}

	out.SegmentCount = len(rows)
	parsed := make([]replayEventEnvelope, 0, len(rows)*16)
	hasRoot := false

	for _, r := range rows {
		if out.ActorID == "" && r.ActorID != "" {
			out.ActorID = r.ActorID
		}
		if out.PageURL == "" && r.PageURL != "" {
			out.PageURL = r.PageURL
		}
		if out.ViewportW == 0 && r.ViewportW > 0 {
			out.ViewportW = r.ViewportW
		}
		if out.ViewportH == 0 && r.ViewportH > 0 {
			out.ViewportH = r.ViewportH
		}
		if r.HasFullSnapshot > 0 {
			hasRoot = true
		}

		var batch []json.RawMessage
		if err := json.Unmarshal([]byte(r.EventsJSON), &batch); err != nil {
			continue
		}
		for _, rawEv := range batch {
			var hdr struct {
				T  int   `json:"t"`
				TS int64 `json:"ts"`
			}
			if err := json.Unmarshal(rawEv, &hdr); err != nil || hdr.T <= 0 {
				continue
			}
			if hdr.T == 2 {
				hasRoot = true
			}
			parsed = append(parsed, replayEventEnvelope{
				Type: hdr.T,
				TS:   hdr.TS,
				raw:  rawEv,
			})
		}
	}

	if len(parsed) == 0 {
		return out, nil
	}

	sort.SliceStable(parsed, func(i, j int) bool {
		if parsed[i].TS != parsed[j].TS {
			return parsed[i].TS < parsed[j].TS
		}
		// Ensure Meta (1) & FullSnapshot (2) precede mutations at identical millisecond
		if (parsed[i].Type <= 2) != (parsed[j].Type <= 2) {
			return parsed[i].Type < parsed[j].Type
		}
		return false
	})

	events := make([]json.RawMessage, len(parsed))
	for i := range parsed {
		events[i] = parsed[i].raw
	}

	out.Events = events
	out.EventsCount = len(events)
	out.HasDOMReplay = hasRoot
	out.StartMS = parsed[0].TS
	out.EndMS = parsed[len(parsed)-1].TS
	if out.EndMS > out.StartMS {
		out.DurationMS = out.EndMS - out.StartMS
	}
	return out, nil
}
