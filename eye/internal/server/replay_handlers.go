package server

import (
	"encoding/json"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/liutianjie/luma-eye/internal/clickhouse"
	"github.com/liutianjie/luma-eye/internal/model"
	"github.com/liutianjie/luma-eye/internal/pipeline"
	"github.com/liutianjie/luma-eye/internal/scrub"
)

const maxReplayIngestBytes = 2 << 20 // 2 MiB compressed cap per replay segment POST

type replayIngestPayload struct {
	Key             string            `json:"key"`
	Project         string            `json:"project"`
	Env             string            `json:"env"`
	Release         string            `json:"release"`
	SessionID       string            `json:"session_id"`
	ActorID         string            `json:"actor_id"`
	Seq             uint32            `json:"seq"`
	StartMS         int64             `json:"start_ms"`
	EndMS           int64             `json:"end_ms"`
	HasFullSnapshot uint8             `json:"has_full_snapshot"`
	PageURL         string            `json:"page_url"`
	ViewportW       uint16            `json:"viewport_w"`
	ViewportH       uint16            `json:"viewport_h"`
	Events          []json.RawMessage `json:"events"`
}

func (s *Server) handleReplayIngest(w http.ResponseWriter, r *http.Request) {
	setIngestCORS(w, r)

	uaInfo := scrub.ParseUserAgent(r.Header.Get("User-Agent"))
	if uaInfo.IsBot {
		w.WriteHeader(http.StatusNoContent)
		return
	}

	raw, err := io.ReadAll(io.LimitReader(r.Body, maxReplayIngestBytes+1))
	if err != nil {
		http.Error(w, "invalid body", http.StatusBadRequest)
		return
	}
	if len(raw) > maxReplayIngestBytes {
		http.Error(w, "payload too large", http.StatusRequestEntityTooLarge)
		return
	}
	raw, err = pipeline.DecompressBody(r.Header.Get("Content-Encoding"), raw)
	if err != nil {
		http.Error(w, "invalid compressed payload", http.StatusBadRequest)
		return
	}

	var payload replayIngestPayload
	if err := json.Unmarshal(raw, &payload); err != nil {
		http.Error(w, "invalid json", http.StatusBadRequest)
		return
	}
	if len(payload.Events) == 0 {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	if len(payload.Events) > 600 {
		payload.Events = payload.Events[:600]
	}

	keyHint := strings.TrimSpace(r.URL.Query().Get("k"))
	if keyHint == "" {
		keyHint = strings.TrimSpace(payload.Key)
	}
	if keyHint == "" {
		keyHint = extractBearer(r)
	}
	projHint := strings.TrimSpace(r.URL.Query().Get("p"))
	if projHint == "" {
		projHint = strings.TrimSpace(payload.Project)
	}

	origin := strings.TrimSpace(r.Header.Get("Origin"))
	proj, err := s.store.ResolveForIngest(projHint, keyHint, origin, s.cfg.AutoProvisionKey, s.cfg.AllowedOriginSuffixes)
	if err != nil {
		s.writeResolveError(w, err)
		return
	}

	clientIP := scrub.ExtractClientIP(r)
	if !s.allowRateLimit(proj, clientIP) {
		http.Error(w, "rate limit exceeded", http.StatusTooManyRequests)
		return
	}

	sessionID := strings.TrimSpace(payload.SessionID)
	if sessionID == "" {
		sessionID = strings.TrimSpace(r.URL.Query().Get("s"))
	}
	if sessionID == "" || len(sessionID) > 128 {
		http.Error(w, "session_id is required", http.StatusBadRequest)
		return
	}

	seq := payload.Seq
	if seq == 0 {
		if qSeq, err := strconv.ParseUint(r.URL.Query().Get("seq"), 10, 32); err == nil {
			seq = uint32(qSeq)
		}
	}

	var minTS, maxTS int64
	hasFull := payload.HasFullSnapshot
	for _, evRaw := range payload.Events {
		var hdr struct {
			T  int   `json:"t"`
			TS int64 `json:"ts"`
		}
		if err := json.Unmarshal(evRaw, &hdr); err == nil {
			if hdr.T == 2 {
				hasFull = 1
			}
			if hdr.TS > 0 {
				if minTS == 0 || hdr.TS < minTS {
					minTS = hdr.TS
				}
				if hdr.TS > maxTS {
					maxTS = hdr.TS
				}
			}
		}
	}
	now := time.Now().UTC()
	if minTS <= 0 {
		minTS = payload.StartMS
	}
	if minTS <= 0 {
		minTS = now.UnixMilli()
	}
	if maxTS <= 0 {
		maxTS = payload.EndMS
	}
	if maxTS < minTS {
		maxTS = minTS
	}

	eventsBytes, err := json.Marshal(payload.Events)
	if err != nil {
		http.Error(w, "failed to encode events", http.StatusBadRequest)
		return
	}

	seg := model.ReplaySegment{
		TS:              model.FormatTS(time.UnixMilli(minTS).UTC()),
		Project:         proj.Slug,
		Env:             truncate(defaultStr(payload.Env, "production"), 32),
		Release:         truncate(payload.Release, 64),
		SessionID:       truncate(sessionID, 128),
		ActorID:         truncate(strings.TrimSpace(payload.ActorID), 128),
		Seq:             seq,
		StartMS:         minTS,
		EndMS:           maxTS,
		EventsCount:     uint32(len(payload.Events)),
		HasFullSnapshot: hasFull,
		PageURL:         truncate(scrub.ScrubURL(payload.PageURL), 1024),
		ViewportW:       payload.ViewportW,
		ViewportH:       payload.ViewportH,
		EventsJSON:      string(eventsBytes),
	}

	if s.batcher != nil {
		s.batcher.Enqueue(model.RelayBatch{
			ReplaySegments: []model.ReplaySegment{seg},
		})
	}
	writeJSON(w, http.StatusAccepted, map[string]any{"ok": true})
}

func (s *Server) handleGetReplay(w http.ResponseWriter, r *http.Request) {
	projectSlug := strings.TrimSpace(r.URL.Query().Get("project"))
	sessionID := strings.TrimSpace(r.PathValue("sessionId"))
	if projectSlug == "" || sessionID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "project and sessionId are required"})
		return
	}
	if s.ch == nil {
		writeJSON(w, http.StatusOK, &clickhouse.SessionReplayResponse{
			Project:   projectSlug,
			SessionID: sessionID,
			Events:    []json.RawMessage{},
		})
		return
	}
	resp, err := s.ch.GetSessionReplay(r.Context(), projectSlug, sessionID)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, resp)
}
