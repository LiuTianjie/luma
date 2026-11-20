package clickhouse

import (
	"bytes"
	"context"
	_ "embed"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/klauspost/compress/zstd"
)

//go:embed schema.sql
var schemaSQL string

var validDBName = regexp.MustCompile(`^[a-zA-Z0-9_]+$`)

type Client struct {
	baseURL       string
	db            string
	user          string
	password      string
	retentionDays int
	httpClient    *http.Client
	zstdEnc       *zstd.Encoder
}

func NewClient(baseURL, db, user, password string) (*Client, error) {
	if db == "" {
		db = "luma_eye"
	}
	if !validDBName.MatchString(db) {
		return nil, fmt.Errorf("invalid ClickHouse database name %q", db)
	}
	enc, err := zstd.NewWriter(nil, zstd.WithEncoderLevel(zstd.SpeedDefault))
	if err != nil {
		return nil, fmt.Errorf("init zstd encoder: %w", err)
	}
	return &Client{
		baseURL:       strings.TrimRight(baseURL, "/"),
		db:            db,
		user:          user,
		password:      password,
		retentionDays: 7,
		httpClient: &http.Client{
			Timeout: 20 * time.Second,
			Transport: &http.Transport{
				MaxIdleConns:        64,
				MaxIdleConnsPerHost: 32,
				IdleConnTimeout:     90 * time.Second,
			},
		},
		zstdEnc: enc,
	}, nil
}

func (c *Client) SetRetentionDays(days int) {
	if days < 1 {
		days = 1
	}
	c.retentionDays = days
}

func (c *Client) DB() string {
	return c.db
}

func (c *Client) Ping(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+"/ping", nil)
	if err != nil {
		return err
	}
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return fmt.Errorf("clickhouse ping status %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	return nil
}

func (c *Client) EnsureSchema(ctx context.Context) error {
	days := c.retentionDays
	if days < 1 {
		days = 7
	}
	daysStr := strconv.Itoa(days)
	sqlText := strings.ReplaceAll(schemaSQL, "{{DB}}", c.db)
	sqlText = strings.ReplaceAll(sqlText, "{{RETENTION_DAYS}}", daysStr)
	stmts := strings.Split(sqlText, ";")
	for _, stmt := range stmts {
		stmt = strings.TrimSpace(stmt)
		if stmt == "" {
			continue
		}
		if err := c.Exec(ctx, stmt); err != nil {
			return fmt.Errorf("apply schema statement (%s...): %w", firstLine(stmt), err)
		}
	}
	// Best-effort TTL alignment when LUMA_EYE_RETENTION_DAYS changes on an existing DB.
	alterStmts := []string{
		fmt.Sprintf("ALTER TABLE %s.events MODIFY TTL toDateTime(ts) + INTERVAL %d DAY DELETE", c.db, days),
		fmt.Sprintf("ALTER TABLE %s.events_hourly MODIFY TTL hour + INTERVAL %d DAY DELETE", c.db, days),
		fmt.Sprintf("ALTER TABLE %s.exceptions MODIFY TTL toDateTime(ts) + INTERVAL %d DAY DELETE", c.db, days),
		fmt.Sprintf("ALTER TABLE %s.issue_rollups MODIFY TTL toDateTime(last_seen) + INTERVAL %d DAY DELETE", c.db, days),
		fmt.Sprintf("ALTER TABLE %s.logs MODIFY TTL toDateTime(ts) + INTERVAL %d DAY DELETE", c.db, days),
		fmt.Sprintf("ALTER TABLE %s.vitals MODIFY TTL toDateTime(ts) + INTERVAL %d DAY DELETE", c.db, days),
		fmt.Sprintf("ALTER TABLE %s.replay_segments MODIFY TTL toDateTime(ts) + INTERVAL %d DAY DELETE", c.db, days),
	}
	for _, stmt := range alterStmts {
		_ = c.Exec(ctx, stmt)
	}
	return nil
}

func (c *Client) Exec(ctx context.Context, query string) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/", strings.NewReader(query))
	if err != nil {
		return err
	}
	c.setAuth(req)
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("clickhouse exec status %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	return nil
}

func (c *Client) InsertJSONEachRow(ctx context.Context, table string, rows any) error {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)

	switch slice := rows.(type) {
	case nil:
		return nil
	default:
		// Marshal slice elements as NDJSON lines.
		rawSlice, err := json.Marshal(slice)
		if err != nil {
			return err
		}
		var elems []json.RawMessage
		if err := json.Unmarshal(rawSlice, &elems); err != nil {
			return err
		}
		if len(elems) == 0 {
			return nil
		}
		for _, el := range elems {
			buf.Write(el)
			buf.WriteByte('\n')
		}
	}

	compressed := c.zstdEnc.EncodeAll(buf.Bytes(), make([]byte, 0, buf.Len()/2))

	q := url.Values{}
	q.Set("query", fmt.Sprintf("INSERT INTO %s.%s FORMAT JSONEachRow", c.db, table))
	q.Set("date_time_input_format", "best_effort")

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/?"+q.Encode(), bytes.NewReader(compressed))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Encoding", "zstd")
	req.Header.Set("Content-Type", "application/x-ndjson")
	c.setAuth(req)

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("clickhouse insert %s status %d: %s", table, resp.StatusCode, strings.TrimSpace(string(body)))
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	return nil
}

type jsonResponse[T any] struct {
	Data []T `json:"data"`
	Rows int `json:"rows"`
}

func QueryJSON[T any](ctx context.Context, c *Client, sqlQuery string, params map[string]string) ([]T, error) {
	if c == nil {
		return nil, fmt.Errorf("clickhouse client is not initialized")
	}
	q := url.Values{}
	q.Set("default_format", "JSON")
	q.Set("output_format_json_quote_64bit_integers", "0")
	for k, v := range params {
		q.Set("param_"+k, v)
	}
	fullSQL := strings.ReplaceAll(sqlQuery, "{{DB}}", c.db)

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/?"+q.Encode(), strings.NewReader(fullSQL))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "text/plain; charset=utf-8")
	c.setAuth(req)

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 32<<20))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 300 {
		return nil, fmt.Errorf("clickhouse query status %d: %s", resp.StatusCode, strings.TrimSpace(string(raw)))
	}
	var parsed jsonResponse[T]
	if err := json.Unmarshal(raw, &parsed); err != nil {
		return nil, fmt.Errorf("decode clickhouse json: %w", err)
	}
	return parsed.Data, nil
}

func (c *Client) setAuth(req *http.Request) {
	if c.user != "" {
		req.Header.Set("X-ClickHouse-User", c.user)
	}
	if c.password != "" {
		req.Header.Set("X-ClickHouse-Key", c.password)
	}
}

func firstLine(s string) string {
	line, _, _ := strings.Cut(s, "\n")
	if len(line) > 72 {
		return line[:72]
	}
	return line
}
