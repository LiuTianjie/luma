package model

import (
	"encoding/json"
	"time"
)

const ClickHouseTimeLayout = "2006-01-02 15:04:05.000"

func FormatTS(t time.Time) string {
	if t.IsZero() {
		t = time.Now().UTC()
	}
	return t.UTC().Format(ClickHouseTimeLayout)
}

func ParseTS(raw string, fallback time.Time) time.Time {
	if fallback.IsZero() {
		fallback = time.Now().UTC()
	}
	if raw == "" {
		return fallback.UTC()
	}
	layouts := []string{
		time.RFC3339Nano,
		time.RFC3339,
		ClickHouseTimeLayout,
		"2006-01-02 15:04:05",
	}
	for _, l := range layouts {
		if t, err := time.Parse(l, raw); err == nil {
			return t.UTC()
		}
	}
	return fallback.UTC()
}

type Project struct {
	DSNID          int       `json:"dsn_id"`
	Slug           string    `json:"slug"`
	Name           string    `json:"name"`
	PublicKey      string    `json:"public_key"`
	AllowedOrigins []string  `json:"allowed_origins"`
	RateLimitRPS   int       `json:"rate_limit_rps"`
	CreatedAt      time.Time `json:"created_at"`
}

type IssueStatus string

const (
	IssueOpen     IssueStatus = "open"
	IssueResolved IssueStatus = "resolved"
	IssueIgnored  IssueStatus = "ignored"
)

type IssueState struct {
	Project     string      `json:"project"`
	Fingerprint string      `json:"fingerprint"`
	Status      IssueStatus `json:"status"`
	FirstSeen   time.Time   `json:"first_seen"`
	LastAlertAt time.Time   `json:"last_alert_at,omitempty"`
	UpdatedAt   time.Time   `json:"updated_at"`
}

type StackFrame struct {
	Filename     string   `json:"filename"`
	Function     string   `json:"function"`
	LineNo       int      `json:"lineno"`
	ColNo        int      `json:"colno"`
	InApp        bool     `json:"in_app"`
	AbsPath      string   `json:"abs_path,omitempty"`
	PreContext   []string `json:"pre_context,omitempty"`
	ContextLine  string   `json:"context_line,omitempty"`
	PostContext  []string `json:"post_context,omitempty"`
	Symbolicated bool     `json:"symbolicated,omitempty"`
}

type Breadcrumb struct {
	TS       string         `json:"ts,omitempty"`
	Category string         `json:"category"`
	Level    string         `json:"level"`
	Message  string         `json:"message"`
	Data     map[string]any `json:"data,omitempty"`
}

type Event struct {
	TS           string `json:"ts"`
	Project      string `json:"project"`
	Env          string `json:"env"`
	Release      string `json:"release"`
	EventType    string `json:"event_type"`
	EventName    string `json:"event_name"`
	ActorID      string `json:"actor_id"`
	SessionID    string `json:"session_id"`
	PagePath     string `json:"page_path"`
	PageURL      string `json:"page_url"`
	Referrer     string `json:"referrer"`
	UTMSource    string `json:"utm_source"`
	UTMMedium    string `json:"utm_medium"`
	UTMCampaign  string `json:"utm_campaign"`
	DurationMS   uint32 `json:"duration_ms"`
	Browser      string `json:"browser"`
	OS           string `json:"os"`
	DeviceType   string `json:"device_type"`
	Country      string `json:"country"`
	TraceID      string `json:"trace_id"`
	PropsJSON    string `json:"props_json"`
	ClientIPHash string `json:"client_ip_hash"`
}

type ExceptionEvent struct {
	TS               string            `json:"ts"`
	Project          string            `json:"project"`
	Env              string            `json:"env"`
	Release          string            `json:"release"`
	Fingerprint      string            `json:"fingerprint"`
	Level            string            `json:"level"`
	ErrorType        string            `json:"error_type"`
	ErrorMessage     string            `json:"error_message"`
	Culprit          string            `json:"culprit"`
	ActorID          string            `json:"actor_id"`
	SessionID        string            `json:"session_id"`
	PageURL          string            `json:"page_url"`
	TraceID          string            `json:"trace_id"`
	OccurrenceWeight uint32            `json:"occurrence_weight"`
	Frames           []StackFrame      `json:"frames,omitempty"`
	FramesJSON       string            `json:"frames_json"`
	Breadcrumbs      []Breadcrumb      `json:"breadcrumbs,omitempty"`
	BreadcrumbsJSON  string            `json:"breadcrumbs_json"`
	Tags             map[string]string `json:"tags,omitempty"`
	TagsJSON         string            `json:"tags_json"`
	ExtraJSON        string            `json:"extra_json"`
	Browser          string            `json:"browser"`
	OS               string            `json:"os"`
	DeviceType       string            `json:"device_type"`
}

func (e *ExceptionEvent) EncodeJSONFields() {
	if e.FramesJSON == "" {
		if b, err := json.Marshal(e.Frames); err == nil {
			e.FramesJSON = string(b)
		} else {
			e.FramesJSON = "[]"
		}
	}
	if e.BreadcrumbsJSON == "" {
		if b, err := json.Marshal(e.Breadcrumbs); err == nil {
			e.BreadcrumbsJSON = string(b)
		} else {
			e.BreadcrumbsJSON = "[]"
		}
	}
	if e.TagsJSON == "" {
		if b, err := json.Marshal(e.Tags); err == nil && len(e.Tags) > 0 {
			e.TagsJSON = string(b)
		} else {
			e.TagsJSON = "{}"
		}
	}
	if e.ExtraJSON == "" {
		e.ExtraJSON = "{}"
	}
	if e.OccurrenceWeight == 0 {
		e.OccurrenceWeight = 1
	}
}

type LogRecord struct {
	TS        string `json:"ts"`
	Project   string `json:"project"`
	Env       string `json:"env"`
	Release   string `json:"release"`
	Level     string `json:"level"`
	Message   string `json:"message"`
	ActorID   string `json:"actor_id"`
	SessionID string `json:"session_id"`
	PageURL   string `json:"page_url"`
	TraceID   string `json:"trace_id"`
	AttrsJSON string `json:"attrs_json"`
}

type VitalRecord struct {
	TS         string  `json:"ts"`
	Project    string  `json:"project"`
	Env        string  `json:"env"`
	Release    string  `json:"release"`
	MetricName string  `json:"metric_name"`
	Value      float64 `json:"value"`
	Rating     string  `json:"rating"`
	PagePath   string  `json:"page_path"`
	ActorID    string  `json:"actor_id"`
	SessionID  string  `json:"session_id"`
	Browser    string  `json:"browser"`
	DeviceType string  `json:"device_type"`
}

type ReplaySegment struct {
	TS              string `json:"ts"`
	Project         string `json:"project"`
	Env             string `json:"env"`
	Release         string `json:"release"`
	SessionID       string `json:"session_id"`
	ActorID         string `json:"actor_id"`
	Seq             uint32 `json:"seq"`
	StartMS         int64  `json:"start_ms"`
	EndMS           int64  `json:"end_ms"`
	EventsCount     uint32 `json:"events_count"`
	HasFullSnapshot uint8  `json:"has_full_snapshot"`
	PageURL         string `json:"page_url"`
	ViewportW       uint16 `json:"viewport_w"`
	ViewportH       uint16 `json:"viewport_h"`
	EventsJSON      string `json:"events_json"`
}

type RelayBatch struct {
	Events         []Event          `json:"events,omitempty"`
	Exceptions     []ExceptionEvent `json:"exceptions,omitempty"`
	Logs           []LogRecord      `json:"logs,omitempty"`
	Vitals         []VitalRecord    `json:"vitals,omitempty"`
	ReplaySegments []ReplaySegment  `json:"replay_segments,omitempty"`
}

func (b *RelayBatch) Len() int {
	return len(b.Events) + len(b.Exceptions) + len(b.Logs) + len(b.Vitals) + len(b.ReplaySegments)
}
