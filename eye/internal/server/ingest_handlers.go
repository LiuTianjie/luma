package server

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/liutianjie/luma-eye/internal/geoip"
	"github.com/liutianjie/luma-eye/internal/model"
	"github.com/liutianjie/luma-eye/internal/pipeline"
	"github.com/liutianjie/luma-eye/internal/project"
	"github.com/liutianjie/luma-eye/internal/scrub"
)

const maxIngestBodyBytes = 1 << 20 // 1 MiB hard cap per HTTP ingest request

type unifiedBatchPayload struct {
	Key         string `json:"key"`
	Project     string `json:"project"`
	Track       string `json:"track"`
	Env         string `json:"env"`
	Release     string `json:"release"`
	SDK         string `json:"sdk"`
	OS          string `json:"os"`
	DeviceType  string `json:"device_type"`
	ActorID     string `json:"actor_id"`
	SessionID   string `json:"session_id"`
	PageURL     string `json:"page_url"`
	Referrer    string `json:"referrer"`
	UTMSource   string `json:"utm_source"`
	UTMMedium   string `json:"utm_medium"`
	UTMCampaign string `json:"utm_campaign"`
	TZ          string `json:"tz"`
	Lang        string `json:"lang"`

	Events []struct {
		TS          any            `json:"ts"`
		Type        string         `json:"type"`
		Name        string         `json:"name"`
		PageURL     string         `json:"page_url"`
		PagePath    string         `json:"page_path"`
		Referrer    string         `json:"referrer"`
		UTMSource   string         `json:"utm_source"`
		UTMMedium   string         `json:"utm_medium"`
		UTMCampaign string         `json:"utm_campaign"`
		DurationMS  uint32         `json:"duration_ms"`
		TraceID     string         `json:"trace_id"`
		Props       map[string]any `json:"props"`
	} `json:"events"`

	Errors []struct {
		TS          any                `json:"ts"`
		Level       string             `json:"level"`
		Type        string             `json:"type"`
		Message     string             `json:"message"`
		Stack       string             `json:"stack"`
		Frames      []model.StackFrame `json:"frames"`
		Breadcrumbs []flexBreadcrumb   `json:"breadcrumbs"`
		PageURL     string             `json:"page_url"`
		TraceID     string             `json:"trace_id"`
		Tags        map[string]string  `json:"tags"`
		Extra       map[string]any     `json:"extra"`
	} `json:"errors"`

	Logs []struct {
		TS      any            `json:"ts"`
		Level   string         `json:"level"`
		Message string         `json:"message"`
		PageURL string         `json:"page_url"`
		TraceID string         `json:"trace_id"`
		Attrs   map[string]any `json:"attrs"`
	} `json:"logs"`

	Vitals []struct {
		TS       any     `json:"ts"`
		Name     string  `json:"name"`
		Value    float64 `json:"value"`
		Rating   string  `json:"rating"`
		PagePath string  `json:"page_path"`
		PageURL  string  `json:"page_url"`
	} `json:"vitals"`
}

type flexBreadcrumb struct {
	TS       any            `json:"ts"`
	Category string         `json:"category"`
	Level    string         `json:"level"`
	Message  string         `json:"message"`
	Data     map[string]any `json:"data"`
}

func (s *Server) handleCORS(w http.ResponseWriter, r *http.Request) {
	setIngestCORS(w, r)
	w.WriteHeader(http.StatusNoContent)
}

func (s *Server) handleUnifiedIngest(w http.ResponseWriter, r *http.Request) {
	setIngestCORS(w, r)

	uaInfo := scrub.ParseUserAgent(r.Header.Get("User-Agent"))
	if uaInfo.IsBot {
		// Silently drop known crawlers / health probes without wasting storage.
		w.WriteHeader(http.StatusNoContent)
		return
	}

	raw, err := io.ReadAll(io.LimitReader(r.Body, maxIngestBodyBytes+1))
	if err != nil {
		http.Error(w, "invalid body", http.StatusBadRequest)
		return
	}
	if len(raw) > maxIngestBodyBytes {
		http.Error(w, "payload too large", http.StatusRequestEntityTooLarge)
		return
	}
	raw, err = pipeline.DecompressBody(r.Header.Get("Content-Encoding"), raw)
	if err != nil {
		http.Error(w, "invalid compressed payload", http.StatusBadRequest)
		return
	}

	var payload unifiedBatchPayload
	if err := json.Unmarshal(raw, &payload); err != nil {
		http.Error(w, "invalid json", http.StatusBadRequest)
		return
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

	if sdk := strings.TrimSpace(payload.SDK); sdk != "" && (uaInfo.Browser == "Other" || uaInfo.Browser == "React Native") {
		uaInfo.Browser = truncate(sdk, 64)
	}
	if osName := strings.TrimSpace(payload.OS); osName != "" {
		uaInfo.OS = truncate(osName, 64)
	}
	if devType := strings.ToLower(strings.TrimSpace(payload.DeviceType)); devType != "" {
		uaInfo.DeviceType = truncate(devType, 32)
	}

	now := time.Now().UTC()
	ipHash := scrub.HashIP(clientIP, s.cfg.IPSalt)
	country := geoip.Resolve(scrub.ExtractCountry(r), clientIP, payload.TZ)
	env := defaultStr(payload.Env, "production")
	basePageURL := scrub.ScrubURL(payload.PageURL)
	baseRef := scrub.NormalizeReferrer(payload.Referrer, basePageURL)
	browserLang := extractPrimaryLanguage(payload.Lang, r.Header.Get("Accept-Language"))
	clientPlatform := inferClientPlatform(payload.SDK, uaInfo.DeviceType)

	var batch model.RelayBatch

	for _, ev := range payload.Events {
		ts := parseFlexTime(ev.TS, now)
		evType := strings.ToLower(defaultStr(ev.Type, "custom"))
		evName := defaultStr(ev.Name, evType)
		pageURL := scrub.ScrubURL(defaultStr(ev.PageURL, basePageURL))
		pagePath := ev.PagePath
		if pagePath == "" {
			pagePath = scrub.ExtractPath(pageURL)
		}
		ref := scrub.NormalizeReferrer(defaultStr(ev.Referrer, baseRef), pageURL)
		utmSrc := truncate(defaultStr(ev.UTMSource, payload.UTMSource), 128)
		utmMed := truncate(defaultStr(ev.UTMMedium, payload.UTMMedium), 128)
		utmCamp := truncate(defaultStr(ev.UTMCampaign, payload.UTMCampaign), 256)

		var cleanProps map[string]any
		if len(ev.Props) > 0 {
			cleanProps = scrub.ScrubMap(ev.Props)
		}
		if evType == "pageview" {
			if cleanProps == nil {
				cleanProps = make(map[string]any, 3)
			}
			if _, ok := cleanProps["browser_language"]; !ok && browserLang != "" {
				cleanProps["browser_language"] = browserLang
			}
			if _, ok := cleanProps["client_platform"]; !ok && clientPlatform != "" {
				cleanProps["client_platform"] = clientPlatform
			}
			if _, ok := cleanProps["channel"]; !ok {
				cleanProps["channel"] = inferTrafficChannel(ref, utmSrc, utmMed)
			}
		}

		propsJSON := "{}"
		if len(cleanProps) > 0 {
			if b, err := json.Marshal(cleanProps); err == nil {
				propsJSON = string(b)
			}
		}

		batch.Events = append(batch.Events, model.Event{
			TS:           model.FormatTS(ts),
			Project:      proj.Slug,
			Env:          env,
			Release:      payload.Release,
			EventType:    evType,
			EventName:    evName,
			ActorID:      truncate(payload.ActorID, 128),
			SessionID:    truncate(payload.SessionID, 128),
			PagePath:     truncate(pagePath, 512),
			PageURL:      truncate(pageURL, 1024),
			Referrer:     truncate(ref, 256),
			UTMSource:    utmSrc,
			UTMMedium:    utmMed,
			UTMCampaign:  utmCamp,
			DurationMS:   ev.DurationMS,
			Browser:      uaInfo.Browser,
			OS:           uaInfo.OS,
			DeviceType:   uaInfo.DeviceType,
			Country:      country,
			TraceID:      truncate(ev.TraceID, 64),
			PropsJSON:    propsJSON,
			ClientIPHash: ipHash,
		})
	}

	for _, e := range payload.Errors {
		ts := parseFlexTime(e.TS, now)
		errType := defaultStr(e.Type, "Error")
		errMsg := strings.TrimSpace(e.Message)
		frames := e.Frames
		if len(frames) == 0 && e.Stack != "" {
			frames = pipeline.ParseStackString(e.Stack)
		} else {
			for i := range frames {
				if frames[i].Filename == "" && frames[i].AbsPath != "" {
					frames[i].Filename = frames[i].AbsPath
				}
				if !frames[i].InApp && frames[i].Filename != "" {
					frames[i].InApp = pipeline.IsInAppFile(frames[i].Filename)
				}
			}
		}

		bcs := make([]model.Breadcrumb, 0, len(e.Breadcrumbs))
		for _, rawBC := range e.Breadcrumbs {
			bcTS := parseFlexTime(rawBC.TS, ts)
			bcs = append(bcs, model.Breadcrumb{
				TS:       bcTS.Format(time.RFC3339Nano),
				Category: truncate(rawBC.Category, 64),
				Level:    defaultStr(strings.ToLower(rawBC.Level), "info"),
				Message:  truncate(rawBC.Message, 512),
				Data:     scrub.ScrubMap(rawBC.Data),
			})
		}

		tags := scrub.ScrubStringMap(e.Tags)
		extraJSON := "{}"
		if len(e.Extra) > 0 {
			if b, err := json.Marshal(scrub.ScrubMap(e.Extra)); err == nil {
				extraJSON = string(b)
			}
		}
		pageURL := scrub.ScrubURL(defaultStr(e.PageURL, basePageURL))

		exc := model.ExceptionEvent{
			TS:               model.FormatTS(ts),
			Project:          proj.Slug,
			Env:              env,
			Release:          payload.Release,
			Fingerprint:      pipeline.ComputeFingerprint(errType, errMsg, frames),
			Level:            defaultStr(strings.ToLower(e.Level), "error"),
			ErrorType:        truncate(errType, 128),
			ErrorMessage:     truncate(errMsg, 2048),
			Culprit:          truncate(pipeline.ComputeCulprit(frames), 256),
			ActorID:          truncate(payload.ActorID, 128),
			SessionID:        truncate(payload.SessionID, 128),
			PageURL:          truncate(pageURL, 1024),
			TraceID:          truncate(e.TraceID, 64),
			OccurrenceWeight: 1,
			Frames:           frames,
			Breadcrumbs:      bcs,
			Tags:             tags,
			ExtraJSON:        extraJSON,
			Browser:          uaInfo.Browser,
			OS:               uaInfo.OS,
			DeviceType:       uaInfo.DeviceType,
		}
		exc.EncodeJSONFields()
		batch.Exceptions = append(batch.Exceptions, exc)
	}

	for _, lg := range payload.Logs {
		ts := parseFlexTime(lg.TS, now)
		attrsJSON := "{}"
		if len(lg.Attrs) > 0 {
			if b, err := json.Marshal(scrub.ScrubMap(lg.Attrs)); err == nil {
				attrsJSON = string(b)
			}
		}
		batch.Logs = append(batch.Logs, model.LogRecord{
			TS:        model.FormatTS(ts),
			Project:   proj.Slug,
			Env:       env,
			Release:   payload.Release,
			Level:     defaultStr(strings.ToLower(lg.Level), "info"),
			Message:   truncate(lg.Message, 4096),
			ActorID:   truncate(payload.ActorID, 128),
			SessionID: truncate(payload.SessionID, 128),
			PageURL:   truncate(scrub.ScrubURL(defaultStr(lg.PageURL, basePageURL)), 1024),
			TraceID:   truncate(lg.TraceID, 64),
			AttrsJSON: attrsJSON,
		})
	}

	for _, vt := range payload.Vitals {
		ts := parseFlexTime(vt.TS, now)
		name := strings.ToUpper(strings.TrimSpace(vt.Name))
		if name == "" {
			continue
		}
		pagePath := vt.PagePath
		if pagePath == "" {
			pagePath = scrub.ExtractPath(defaultStr(vt.PageURL, basePageURL))
		}
		batch.Vitals = append(batch.Vitals, model.VitalRecord{
			TS:         model.FormatTS(ts),
			Project:    proj.Slug,
			Env:        env,
			Release:    payload.Release,
			MetricName: truncate(name, 32),
			Value:      vt.Value,
			Rating:     defaultStr(strings.ToLower(vt.Rating), "unknown"),
			PagePath:   truncate(pagePath, 512),
			ActorID:    truncate(payload.ActorID, 128),
			SessionID:  truncate(payload.SessionID, 128),
			Browser:    uaInfo.Browser,
			DeviceType: uaInfo.DeviceType,
		})
	}

	s.batcher.Enqueue(batch)
	w.WriteHeader(http.StatusNoContent)
}

func (s *Server) handleSentryEnvelope(w http.ResponseWriter, r *http.Request) {
	setIngestCORS(w, r)

	idOrSlug := strings.TrimSpace(r.PathValue("id"))
	if idOrSlug == "" {
		http.Error(w, "missing project id", http.StatusBadRequest)
		return
	}

	raw, err := io.ReadAll(io.LimitReader(r.Body, maxIngestBodyBytes+1))
	if err != nil {
		http.Error(w, "invalid body", http.StatusBadRequest)
		return
	}
	if len(raw) > maxIngestBodyBytes {
		http.Error(w, "payload too large", http.StatusRequestEntityTooLarge)
		return
	}
	raw, err = pipeline.DecompressBody(r.Header.Get("Content-Encoding"), raw)
	if err != nil {
		http.Error(w, "invalid compressed envelope", http.StatusBadRequest)
		return
	}

	sentryKey := pipeline.ExtractSentryKey(r, raw)
	origin := strings.TrimSpace(r.Header.Get("Origin"))
	proj, err := s.store.ResolveForIngest(idOrSlug, sentryKey, origin, s.cfg.AutoProvisionKey, s.cfg.AllowedOriginSuffixes)
	if err != nil {
		s.writeResolveError(w, err)
		return
	}

	clientIP := scrub.ExtractClientIP(r)
	if !s.allowRateLimit(proj, clientIP) {
		http.Error(w, "rate limit exceeded", http.StatusTooManyRequests)
		return
	}

	uaInfo := scrub.ParseUserAgent(r.Header.Get("User-Agent"))
	parsed, err := pipeline.ParseSentryEnvelope(proj.Slug, uaInfo, raw)
	if err != nil {
		http.Error(w, "invalid sentry envelope", http.StatusBadRequest)
		return
	}
	country := geoip.Resolve(scrub.ExtractCountry(r), clientIP, "")
	ipHash := scrub.HashIP(clientIP, s.cfg.IPSalt)
	for i := range parsed.Events {
		if parsed.Events[i].Country == "" {
			parsed.Events[i].Country = country
		}
		if parsed.Events[i].ClientIPHash == "" {
			parsed.Events[i].ClientIPHash = ipHash
		}
	}

	s.batcher.Enqueue(model.RelayBatch{
		Events:     parsed.Events,
		Exceptions: parsed.Exceptions,
		Logs:       parsed.Logs,
	})

	eventID := parsed.EventID
	if eventID == "" {
		eventID = fmt.Sprintf("%x", time.Now().UnixNano())
	}
	writeJSON(w, http.StatusOK, map[string]string{"id": eventID})
}

func (s *Server) allowRateLimit(proj *model.Project, clientIP string) bool {
	if clientIP != "" && !s.ipLimiter.Allow("ip:"+clientIP, 0) {
		s.metrics.AddRateLimited(1)
		return false
	}
	if !s.projLimiter.Allow("proj:"+proj.Slug, float64(proj.RateLimitRPS)) {
		s.metrics.AddRateLimited(1)
		return false
	}
	return true
}

func (s *Server) writeResolveError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, project.ErrOriginRejected):
		http.Error(w, "origin not allowed", http.StatusForbidden)
	case errors.Is(err, project.ErrInvalidSlug):
		http.Error(w, err.Error(), http.StatusBadRequest)
	default:
		http.Error(w, "unauthorized project key", http.StatusUnauthorized)
	}
}

func parseFlexTime(raw any, fallback time.Time) time.Time {
	switch v := raw.(type) {
	case string:
		return model.ParseTS(v, fallback)
	case float64:
		if v <= 0 {
			return fallback
		}
		// Heuristic: >= 1e11 is Unix milliseconds, otherwise Unix seconds.
		if v >= 1e11 {
			ms := int64(v)
			return time.UnixMilli(ms).UTC()
		}
		sec := int64(v)
		nsec := int64((v - float64(sec)) * 1e9)
		return time.Unix(sec, nsec).UTC()
	case int64:
		if v <= 0 {
			return fallback
		}
		if v >= 1e11 {
			return time.UnixMilli(v).UTC()
		}
		return time.Unix(v, 0).UTC()
	default:
		return fallback
	}
}

func defaultStr(v, fallback string) string {
	v = strings.TrimSpace(v)
	if v == "" {
		return fallback
	}
	return v
}

func truncate(s string, max int) string {
	if len(s) <= max {
		return s
	}
	return s[:max]
}

func extractPrimaryLanguage(explicit, acceptLang string) string {
	if v := strings.TrimSpace(explicit); v != "" {
		return truncate(v, 32)
	}
	first, _, _ := strings.Cut(acceptLang, ",")
	tag, _, _ := strings.Cut(first, ";")
	tag = strings.TrimSpace(tag)
	if tag == "" || tag == "*" {
		return ""
	}
	return truncate(tag, 32)
}

func inferClientPlatform(sdk, deviceType string) string {
	lowSDK := strings.ToLower(strings.TrimSpace(sdk))
	if strings.Contains(lowSDK, "rn") || strings.Contains(lowSDK, "react-native") || strings.Contains(lowSDK, "expo") {
		return "react-native"
	}
	if deviceType == "mobile" || deviceType == "tablet" {
		return "mobile-web"
	}
	return "web"
}

func inferTrafficChannel(referrer, utmSource, utmMedium string) string {
	lowMed := strings.ToLower(strings.TrimSpace(utmMedium))
	if lowMed == "cpc" || lowMed == "ppc" || lowMed == "paid" || lowMed == "ads" {
		return "paid"
	}
	if strings.TrimSpace(utmSource) != "" || lowMed != "" {
		return "campaign"
	}
	lowRef := strings.ToLower(strings.TrimSpace(referrer))
	if lowRef == "" {
		return "direct"
	}
	for _, se := range []string{"google.", "bing.", "baidu.", "duckduckgo.", "yahoo.", "sogou.", "so.com", "yandex."} {
		if strings.Contains(lowRef, se) {
			return "organic_search"
		}
	}
	for _, soc := range []string{"twitter.", "x.com", "t.co", "weibo.", "xiaohongshu.", "zhihu.", "reddit.", "facebook.", "linkedin.", "youtube.", "bilibili.", "mp.weixin.qq.com"} {
		if strings.Contains(lowRef, soc) {
			return "social"
		}
	}
	return "referral"
}
