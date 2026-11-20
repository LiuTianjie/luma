package pipeline

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"compress/zlib"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/klauspost/compress/zstd"
	"github.com/liutianjie/luma-eye/internal/model"
	"github.com/liutianjie/luma-eye/internal/scrub"
)

type SentryParseResult struct {
	EventID    string
	SentryKey  string
	Exceptions []model.ExceptionEvent
	Logs       []model.LogRecord
	Events     []model.Event
}

// ExtractSentryKey checks ?sentry_key=, X-Sentry-Auth header, Authorization Bearer, or envelope DSN.
func ExtractSentryKey(r *http.Request, body []byte) string {
	if k := strings.TrimSpace(r.URL.Query().Get("sentry_key")); k != "" {
		return k
	}
	if auth := strings.TrimSpace(r.Header.Get("X-Sentry-Auth")); auth != "" {
		for _, part := range strings.Split(auth, ",") {
			part = strings.TrimSpace(part)
			if strings.HasPrefix(part, "Sentry ") {
				part = strings.TrimSpace(strings.TrimPrefix(part, "Sentry "))
			}
			k, v, ok := strings.Cut(part, "=")
			if ok && strings.TrimSpace(k) == "sentry_key" {
				return strings.Trim(strings.TrimSpace(v), `"'`)
			}
		}
	}
	if bearer := strings.TrimSpace(r.Header.Get("Authorization")); strings.HasPrefix(strings.ToLower(bearer), "bearer ") {
		return strings.TrimSpace(bearer[7:])
	}
	// Try peeking at the envelope first line for "dsn": "https://<key>@host/..."
	firstLine, _, _ := bytes.Cut(body, []byte{'\n'})
	var envHdr struct {
		DSN string `json:"dsn"`
	}
	if len(firstLine) > 0 && json.Unmarshal(firstLine, &envHdr) == nil && envHdr.DSN != "" {
		if u, err := url.Parse(envHdr.DSN); err == nil && u.User != nil {
			return u.User.Username()
		}
	}
	return ""
}

func DecompressBody(contentEncoding string, raw []byte) ([]byte, error) {
	ce := strings.ToLower(strings.TrimSpace(contentEncoding))
	if ce == "" && len(raw) >= 2 && raw[0] == 0x1f && raw[1] == 0x8b {
		ce = "gzip"
	}
	switch ce {
	case "", "identity":
		return raw, nil
	case "gzip":
		gr, err := gzip.NewReader(bytes.NewReader(raw))
		if err != nil {
			return nil, err
		}
		defer gr.Close()
		return io.ReadAll(io.LimitReader(gr, 8<<20))
	case "zstd":
		dec, err := zstd.NewReader(nil)
		if err != nil {
			return nil, err
		}
		defer dec.Close()
		return dec.DecodeAll(raw, nil)
	case "deflate":
		zr, err := zlib.NewReader(bytes.NewReader(raw))
		if err != nil {
			return nil, err
		}
		defer zr.Close()
		return io.ReadAll(io.LimitReader(zr, 8<<20))
	default:
		return raw, nil
	}
}

// ParseSentryEnvelope parses a Sentry Envelope (header line + sequence of itemHeader + itemPayload).
func ParseSentryEnvelope(projectSlug string, uaInfo scrub.UAInfo, raw []byte) (*SentryParseResult, error) {
	res := &SentryParseResult{}
	reader := bufio.NewReader(bytes.NewReader(raw))

	envHeaderLine, err := readLineBytes(reader)
	if err != nil && len(envHeaderLine) == 0 {
		return nil, fmt.Errorf("empty sentry envelope")
	}
	var envHdr struct {
		EventID string `json:"event_id"`
		DSN     string `json:"dsn"`
		SentAt  string `json:"sent_at"`
	}
	_ = json.Unmarshal(envHeaderLine, &envHdr)
	res.EventID = envHdr.EventID
	if envHdr.DSN != "" {
		if u, err := url.Parse(envHdr.DSN); err == nil && u.User != nil {
			res.SentryKey = u.User.Username()
		}
	}

	for {
		itemHeaderLine, err := readLineBytes(reader)
		if len(bytes.TrimSpace(itemHeaderLine)) == 0 {
			if err != nil {
				break
			}
			continue
		}
		var itemHdr struct {
			Type   string `json:"type"`
			Length *int   `json:"length"`
		}
		if jErr := json.Unmarshal(itemHeaderLine, &itemHdr); jErr != nil {
			if err != nil {
				break
			}
			continue
		}

		var payload []byte
		if itemHdr.Length != nil && *itemHdr.Length >= 0 {
			if *itemHdr.Length > 8<<20 {
				return nil, fmt.Errorf("sentry envelope item too large")
			}
			payload = make([]byte, *itemHdr.Length)
			if _, rErr := io.ReadFull(reader, payload); rErr != nil {
				return nil, fmt.Errorf("read envelope item payload: %w", rErr)
			}
			// Consume optional trailing newline after length-delimited payload.
			if peek, pErr := reader.Peek(1); pErr == nil && (peek[0] == '\n' || peek[0] == '\r') {
				_, _ = readLineBytes(reader)
			}
		} else {
			payload, _ = readLineBytes(reader)
		}

		switch itemHdr.Type {
		case "event":
			if exc, ok := parseSentryEventPayload(projectSlug, uaInfo, payload); ok {
				if res.EventID == "" && exc.Tags["sentry_event_id"] != "" {
					res.EventID = exc.Tags["sentry_event_id"]
				}
				res.Exceptions = append(res.Exceptions, exc)
			} else if logRec, ok := parseSentryMessagePayload(projectSlug, payload); ok {
				res.Logs = append(res.Logs, logRec)
			}
		case "log", "logs":
			if logRec, ok := parseSentryMessagePayload(projectSlug, payload); ok {
				res.Logs = append(res.Logs, logRec)
			}
		}

		if err == io.EOF {
			break
		}
	}

	return res, nil
}

func readLineBytes(r *bufio.Reader) ([]byte, error) {
	line, err := r.ReadBytes('\n')
	line = bytes.TrimRight(line, "\r\n")
	return line, err
}

type sentryEventPayload struct {
	EventID     string  `json:"event_id"`
	Timestamp   any     `json:"timestamp"`
	Level       string  `json:"level"`
	Environment string  `json:"environment"`
	Release     string  `json:"release"`
	Culprit     string  `json:"culprit"`
	Transaction string  `json:"transaction"`
	Message     any     `json:"message"`
	LogEntry    *struct {
		Formatted string `json:"formatted"`
		Message   string `json:"message"`
	} `json:"logentry"`
	User *struct {
		ID       any    `json:"id"`
		Username string `json:"username"`
		Email    string `json:"email"`
	} `json:"user"`
	Request *struct {
		URL     string            `json:"url"`
		Method  string            `json:"method"`
		Headers map[string]string `json:"headers"`
	} `json:"request"`
	Exception *struct {
		Values []struct {
			Type       string `json:"type"`
			Value      string `json:"value"`
			Stacktrace *struct {
				Frames []struct {
					Filename string `json:"filename"`
					AbsPath  string `json:"abs_path"`
					Function string `json:"function"`
					LineNo   int    `json:"lineno"`
					ColNo    int    `json:"colno"`
					InApp    *bool  `json:"in_app"`
				} `json:"frames"`
			} `json:"stacktrace"`
		} `json:"values"`
	} `json:"exception"`
	Breadcrumbs any               `json:"breadcrumbs"`
	Tags        map[string]string `json:"tags"`
	Extra       map[string]any    `json:"extra"`
	Contexts    map[string]any    `json:"contexts"`
}

func parseSentryEventPayload(projectSlug string, uaInfo scrub.UAInfo, payload []byte) (model.ExceptionEvent, bool) {
	var ev sentryEventPayload
	if err := json.Unmarshal(payload, &ev); err != nil {
		return model.ExceptionEvent{}, false
	}
	if ev.Exception == nil || len(ev.Exception.Values) == 0 {
		return model.ExceptionEvent{}, false
	}
	primary := ev.Exception.Values[len(ev.Exception.Values)-1]
	errType := strings.TrimSpace(primary.Type)
	if errType == "" {
		errType = "Error"
	}
	errMsg := strings.TrimSpace(primary.Value)

	var frames []model.StackFrame
	if primary.Stacktrace != nil {
		for _, sf := range primary.Stacktrace.Frames {
			file := sf.Filename
			if file == "" {
				file = sf.AbsPath
			}
			inApp := IsInAppFile(file)
			if sf.InApp != nil {
				inApp = *sf.InApp
			}
			fn := sf.Function
			if fn == "" {
				fn = "<anonymous>"
			}
			frames = append(frames, model.StackFrame{
				Filename: file,
				AbsPath:  sf.AbsPath,
				Function: fn,
				LineNo:   sf.LineNo,
				ColNo:    sf.ColNo,
				InApp:    inApp,
			})
		}
	}

	ts := parseSentryTimestamp(ev.Timestamp)
	env := strings.TrimSpace(ev.Environment)
	if env == "" {
		env = "production"
	}
	level := strings.ToLower(strings.TrimSpace(ev.Level))
	if level == "" {
		level = "error"
	}

	actorID := ""
	if ev.User != nil {
		if ev.User.ID != nil {
			actorID = fmt.Sprintf("%v", ev.User.ID)
		} else if ev.User.Username != "" {
			actorID = ev.User.Username
		}
	}

	pageURL := ""
	if ev.Request != nil {
		pageURL = scrub.ScrubURL(ev.Request.URL)
	}
	if pageURL == "" {
		if u, ok := ev.Tags["url"]; ok {
			pageURL = scrub.ScrubURL(u)
		}
	}

	traceID := ""
	if traceCtx, ok := ev.Contexts["trace"].(map[string]any); ok {
		if tid, ok := traceCtx["trace_id"].(string); ok {
			traceID = tid
		}
	}

	tags := scrub.ScrubStringMap(ev.Tags)
	if tags == nil {
		tags = make(map[string]string)
	}
	if ev.EventID != "" {
		tags["sentry_event_id"] = ev.EventID
	}
	extra := scrub.ScrubMap(ev.Extra)
	bcs := parseSentryBreadcrumbs(ev.Breadcrumbs)

	culprit := ev.Culprit
	if culprit == "" {
		culprit = ComputeCulprit(frames)
	}
	if culprit == "" {
		culprit = ev.Transaction
	}

	fp := ComputeFingerprint(errType, errMsg, frames)
	exc := model.ExceptionEvent{
		TS:               model.FormatTS(ts),
		Project:          projectSlug,
		Env:              env,
		Release:          ev.Release,
		Fingerprint:      fp,
		Level:            level,
		ErrorType:        errType,
		ErrorMessage:     errMsg,
		Culprit:          culprit,
		ActorID:          actorID,
		PageURL:          pageURL,
		TraceID:          traceID,
		OccurrenceWeight: 1,
		Frames:           frames,
		Breadcrumbs:      bcs,
		Tags:             tags,
		Browser:          uaInfo.Browser,
		OS:               uaInfo.OS,
		DeviceType:       uaInfo.DeviceType,
	}
	if extra != nil {
		if b, err := json.Marshal(extra); err == nil {
			exc.ExtraJSON = string(b)
		}
	}
	exc.EncodeJSONFields()
	return exc, true
}

func parseSentryMessagePayload(projectSlug string, payload []byte) (model.LogRecord, bool) {
	var ev sentryEventPayload
	if err := json.Unmarshal(payload, &ev); err != nil {
		return model.LogRecord{}, false
	}
	msg := ""
	switch m := ev.Message.(type) {
	case string:
		msg = m
	case map[string]any:
		if fmtMsg, ok := m["formatted"].(string); ok {
			msg = fmtMsg
		} else if rawMsg, ok := m["message"].(string); ok {
			msg = rawMsg
		}
	}
	if msg == "" && ev.LogEntry != nil {
		msg = ev.LogEntry.Formatted
		if msg == "" {
			msg = ev.LogEntry.Message
		}
	}
	if msg == "" {
		return model.LogRecord{}, false
	}
	level := strings.ToLower(strings.TrimSpace(ev.Level))
	if level == "" {
		level = "info"
	}
	env := strings.TrimSpace(ev.Environment)
	if env == "" {
		env = "production"
	}
	attrs := scrub.ScrubMap(ev.Extra)
	attrsJSON := "{}"
	if len(attrs) > 0 {
		if b, err := json.Marshal(attrs); err == nil {
			attrsJSON = string(b)
		}
	}
	return model.LogRecord{
		TS:        model.FormatTS(parseSentryTimestamp(ev.Timestamp)),
		Project:   projectSlug,
		Env:       env,
		Release:   ev.Release,
		Level:     level,
		Message:   msg,
		AttrsJSON: attrsJSON,
	}, true
}

func parseSentryBreadcrumbs(raw any) []model.Breadcrumb {
	var list []any
	switch v := raw.(type) {
	case []any:
		list = v
	case map[string]any:
		if vals, ok := v["values"].([]any); ok {
			list = vals
		}
	}
	if len(list) == 0 {
		return nil
	}
	out := make([]model.Breadcrumb, 0, len(list))
	for _, item := range list {
		m, ok := item.(map[string]any)
		if !ok {
			continue
		}
		cat, _ := m["category"].(string)
		lvl, _ := m["level"].(string)
		msg, _ := m["message"].(string)
		var tsStr string
		if tsRaw, ok := m["timestamp"]; ok {
			tsStr = parseSentryTimestamp(tsRaw).Format(time.RFC3339Nano)
		}
		var data map[string]any
		if d, ok := m["data"].(map[string]any); ok {
			data = scrub.ScrubMap(d)
		}
		out = append(out, model.Breadcrumb{
			TS:       tsStr,
			Category: cat,
			Level:    lvl,
			Message:  msg,
			Data:     data,
		})
	}
	return out
}

func parseSentryTimestamp(raw any) time.Time {
	switch v := raw.(type) {
	case float64:
		sec := int64(v)
		nsec := int64((v - float64(sec)) * 1e9)
		return time.Unix(sec, nsec).UTC()
	case int64:
		return time.Unix(v, 0).UTC()
	case string:
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			sec := int64(f)
			nsec := int64((f - float64(sec)) * 1e9)
			return time.Unix(sec, nsec).UTC()
		}
		return model.ParseTS(v, time.Now().UTC())
	default:
		return time.Now().UTC()
	}
}
