// Package history owns bounded history queries and portable pagination cursors.
package history

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"
	"unicode/utf16"
	"unicode/utf8"
)

var (
	ErrInvalid      = errors.New("invalid history query")
	ErrNotFound     = errors.New("history record not found")
	ErrStoredRecord = errors.New("stored history record is invalid")
)

func invalid(message string) error { return fmt.Errorf("%w: %s", ErrInvalid, message) }

type Filters struct {
	App    string `json:"app"`
	Status string `json:"status"`
	Source string `json:"source"`
	Kind   string `json:"kind"`
	Since  *int64 `json:"since"`
	Until  *int64 `json:"until"`
}

type ListQuery struct {
	Limit          int
	Filters        Filters
	InsertionOrder bool
	Scope          string
	Cursor         *ListCursor
}

type ListCursor struct {
	Watermark int64
	CreatedAt int64
	ID, Kind  string
	Insertion int64
}

type DetailQuery struct {
	Limit                int
	Kind, ID, Scope      string
	After                int64
	Through              *int64
	IncludeQueuePosition bool
}

func InternalKind(kind string) string {
	if kind == "build" {
		return "buildRuns"
	}
	if kind == "deployment" {
		return "deploymentEvents"
	}
	return ""
}

func PublicKind(kind string) string {
	if kind == "buildRuns" {
		return "build"
	}
	if kind == "deploymentEvents" {
		return "deployment"
	}
	return ""
}

func parseLimit(values map[string]string) (int, error) {
	raw, exists := values["limit"]
	if !exists {
		return 50, nil
	}
	raw = strings.TrimSpace(raw)
	n, err := strconv.Atoi(raw)
	if err != nil || strconv.Itoa(n) != raw || n < 1 || n > 100 {
		return 0, invalid("limit must be an integer between 1 and 100")
	}
	return n, nil
}

func parseTime(raw, name string) (*int64, error) {
	if raw == "" {
		return nil, nil
	}
	raw = strings.TrimSpace(raw)
	n, err := strconv.ParseInt(raw, 10, 64)
	if err != nil || strings.HasPrefix(raw, "-") || strings.HasPrefix(raw, "+") {
		stamp, parseErr := time.Parse(time.RFC3339Nano, raw)
		if parseErr != nil {
			return nil, invalid(name + " must be Unix seconds or an RFC3339 timestamp with timezone")
		}
		n = stamp.Unix()
	}
	if n < 0 || n > 253402300799 {
		return nil, invalid(name + " must be Unix seconds or an RFC3339 timestamp with timezone")
	}
	return &n, nil
}

func ParseList(values map[string]string, fixedKind string, insertionOrder bool) (ListQuery, error) {
	q := ListQuery{InsertionOrder: insertionOrder}
	var err error
	if q.Limit, err = parseLimit(values); err != nil {
		return q, err
	}
	f := Filters{App: strings.TrimSpace(values["app"]), Status: strings.TrimSpace(values["status"]), Source: strings.TrimSpace(values["source"]), Kind: strings.TrimSpace(values["kind"])}
	if fixedKind != "" {
		if f.Kind != "" && f.Kind != fixedKind {
			return q, invalid("kind does not match this history endpoint")
		}
		f.Kind = fixedKind
	}
	if f.Kind != "" && InternalKind(f.Kind) == "" {
		return q, invalid("kind must be build or deployment")
	}
	if f.Source != "" && f.Source != "build" && f.Source != "cli" && f.Source != "dashboard" {
		return q, invalid("source must be build, cli, or dashboard")
	}
	if utf8.RuneCountInString(f.App) > 255 || utf8.RuneCountInString(f.Status) > 80 {
		return q, invalid("history app or status filter is too long")
	}
	if f.Since, err = parseTime(values["since"], "since"); err != nil {
		return q, err
	}
	if f.Until, err = parseTime(values["until"], "until"); err != nil {
		return q, err
	}
	if f.Since != nil && f.Until != nil && *f.Since > *f.Until {
		return q, invalid("since must not be later than until")
	}
	q.Filters = f
	order := "timeline"
	if insertionOrder {
		order = "deployment-insertion"
	}
	q.Scope = fingerprint(map[string]any{"list": map[string]any{"app": f.App, "status": f.Status, "source": f.Source, "kind": f.Kind, "since": f.Since, "until": f.Until}, "order": order})
	cursor, err := decodeCursor(values["cursor"], q.Scope)
	if err != nil || cursor == nil {
		return q, err
	}
	c := &ListCursor{}
	var ok bool
	if c.Watermark, ok = unsignedInteger(cursor["watermark"]); !ok {
		return q, invalid("invalid history cursor")
	}
	key, ok := cursor["key"].([]any)
	wanted := 3
	if insertionOrder {
		wanted = 2
	}
	if !ok || len(key) != wanted {
		return q, invalid("invalid history cursor")
	}
	if c.CreatedAt, ok = unsignedInteger(key[0]); !ok {
		return q, invalid("invalid history cursor")
	}
	if insertionOrder {
		if c.Insertion, ok = unsignedInteger(key[1]); !ok {
			return q, invalid("invalid history cursor")
		}
	} else {
		if c.ID, ok = key[1].(string); !ok {
			return q, invalid("invalid history cursor")
		}
		if c.Kind, ok = key[2].(string); !ok || PublicKind(c.Kind) == "" {
			return q, invalid("invalid history cursor")
		}
	}
	q.Cursor = c
	return q, nil
}

func ParseDetail(kind, id string, values map[string]string) (DetailQuery, error) {
	q := DetailQuery{Kind: kind, ID: id, After: -1}
	if InternalKind(kind) == "" {
		return q, invalid("kind must be build or deployment")
	}
	var err error
	if q.Limit, err = parseLimit(values); err != nil {
		return q, err
	}
	q.Scope = fingerprint(map[string]any{"detail": []string{kind, id}})
	cursor, err := decodeCursor(values["cursor"], q.Scope)
	if err != nil {
		return q, err
	}
	if raw, exists := values["after"]; exists {
		if cursor != nil {
			return q, invalid("after cannot be combined with cursor")
		}
		q.After, err = strconv.ParseInt(raw, 10, 64)
		if err != nil || q.After < -1 || strconv.FormatInt(q.After, 10) != raw {
			return q, invalid("after must be an integer position at least -1")
		}
	}
	if cursor != nil {
		var ok bool
		if q.After, ok = unsignedInteger(cursor["after"]); !ok {
			return q, invalid("invalid history cursor")
		}
		through, ok := unsignedInteger(cursor["through"])
		if !ok || q.After > through {
			return q, invalid("invalid history cursor")
		}
		q.Through = &through
	}
	return q, nil
}

func unsignedInteger(value any) (int64, bool) {
	number, ok := value.(json.Number)
	if !ok {
		return 0, false
	}
	n, err := number.Int64()
	return n, err == nil && n >= 0
}

func decodeCursor(raw, scope string) (map[string]any, error) {
	if raw == "" {
		return nil, nil
	}
	if len(raw) > 4096 {
		return nil, invalid("invalid history cursor")
	}
	data, err := base64.URLEncoding.DecodeString(raw + strings.Repeat("=", (4-len(raw)%4)%4))
	if err != nil || !json.Valid(data) {
		return nil, invalid("invalid history cursor")
	}
	var value map[string]any
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	if err := decoder.Decode(&value); err != nil || value == nil {
		return nil, invalid("invalid history cursor")
	}
	v, ok := unsignedInteger(value["v"])
	if !ok || v != 1 || value["scope"] != scope {
		return nil, invalid("history cursor does not match the requested filters or record")
	}
	return value, nil
}

// Python's existing cursor fingerprint uses sorted, ASCII-escaped JSON. Preserve
// it so pagination can resume on either implementation, including Unicode apps.
func fingerprint(value any) string {
	var b bytes.Buffer
	enc := json.NewEncoder(&b)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(value)
	var ascii strings.Builder
	for _, r := range strings.TrimSuffix(b.String(), "\n") {
		if r <= 127 {
			ascii.WriteRune(r)
		} else if r <= 0xffff {
			fmt.Fprintf(&ascii, "\\u%04x", r)
		} else {
			a, z := utf16.EncodeRune(r)
			fmt.Fprintf(&ascii, "\\u%04x\\u%04x", a, z)
		}
	}
	digest := sha256.Sum256([]byte(ascii.String()))
	return hex.EncodeToString(digest[:])[:24]
}

func encodeCursor(value any) string {
	data, _ := json.Marshal(value)
	return base64.RawURLEncoding.EncodeToString(data)
}
