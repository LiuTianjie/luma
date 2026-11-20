package observe

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

type ProjectCounters struct {
	Events     uint64
	Pageviews  uint64
	Exceptions uint64
	Logs       uint64
	Vitals     uint64
}

type Metrics struct {
	mu                     sync.RWMutex
	byProject              map[string]*ProjectCounters
	droppedTotal           atomic.Uint64
	rateLimitedTotal       atomic.Uint64
	relayRawBytesTotal     atomic.Uint64
	relayCompBytesTotal    atomic.Uint64
	clickhouseErrorsTotal  atomic.Uint64
	startUnixNano          int64
}

func NewMetrics() *Metrics {
	return &Metrics{
		byProject:     make(map[string]*ProjectCounters),
		startUnixNano: time.Now().UTC().UnixNano(),
	}
}

func (m *Metrics) getOrCreateLocked(project string) *ProjectCounters {
	c, ok := m.byProject[project]
	if !ok {
		c = &ProjectCounters{}
		m.byProject[project] = c
	}
	return c
}

func (m *Metrics) RecordEvents(project string, total, pageviews uint64) {
	if m == nil || project == "" {
		return
	}
	m.mu.Lock()
	c := m.getOrCreateLocked(project)
	c.Events += total
	c.Pageviews += pageviews
	m.mu.Unlock()
}

func (m *Metrics) RecordExceptions(project string, n uint64) {
	if m == nil || project == "" || n == 0 {
		return
	}
	m.mu.Lock()
	m.getOrCreateLocked(project).Exceptions += n
	m.mu.Unlock()
}

func (m *Metrics) RecordLogs(project string, n uint64) {
	if m == nil || project == "" || n == 0 {
		return
	}
	m.mu.Lock()
	m.getOrCreateLocked(project).Logs += n
	m.mu.Unlock()
}

func (m *Metrics) RecordVitals(project string, n uint64) {
	if m == nil || project == "" || n == 0 {
		return
	}
	m.mu.Lock()
	m.getOrCreateLocked(project).Vitals += n
	m.mu.Unlock()
}

func (m *Metrics) AddDropped(n uint64) {
	if m != nil {
		m.droppedTotal.Add(n)
	}
}

func (m *Metrics) AddRateLimited(n uint64) {
	if m != nil {
		m.rateLimitedTotal.Add(n)
	}
}

func (m *Metrics) AddRelayBytes(rawBytes, compBytes uint64) {
	if m != nil {
		m.relayRawBytesTotal.Add(rawBytes)
		m.relayCompBytesTotal.Add(compBytes)
	}
}

func (m *Metrics) IncClickHouseError() {
	if m != nil {
		m.clickhouseErrorsTotal.Add(1)
	}
}

func (m *Metrics) Snapshot() (map[string]ProjectCounters, uint64, uint64, uint64, uint64, uint64) {
	m.mu.RLock()
	cp := make(map[string]ProjectCounters, len(m.byProject))
	for k, v := range m.byProject {
		cp[k] = *v
	}
	m.mu.RUnlock()
	return cp,
		m.droppedTotal.Load(),
		m.rateLimitedTotal.Load(),
		m.relayRawBytesTotal.Load(),
		m.relayCompBytesTotal.Load(),
		m.clickhouseErrorsTotal.Load()
}

func (m *Metrics) RenderPrometheus() string {
	proj, dropped, rateLimited, rawB, compB, chErr := m.Snapshot()
	names := make([]string, 0, len(proj))
	for k := range proj {
		names = append(names, k)
	}
	sort.Strings(names)

	var sb strings.Builder
	sb.WriteString("# HELP luma_eye_events_total Total ingested client events by project.\n")
	sb.WriteString("# TYPE luma_eye_events_total counter\n")
	for _, p := range names {
		fmt.Fprintf(&sb, "luma_eye_events_total{project=%q} %d\n", p, proj[p].Events)
	}
	sb.WriteString("# HELP luma_eye_pageviews_total Total ingested pageviews by project.\n")
	sb.WriteString("# TYPE luma_eye_pageviews_total counter\n")
	for _, p := range names {
		fmt.Fprintf(&sb, "luma_eye_pageviews_total{project=%q} %d\n", p, proj[p].Pageviews)
	}
	sb.WriteString("# HELP luma_eye_exceptions_total Total ingested exceptions by project.\n")
	sb.WriteString("# TYPE luma_eye_exceptions_total counter\n")
	for _, p := range names {
		fmt.Fprintf(&sb, "luma_eye_exceptions_total{project=%q} %d\n", p, proj[p].Exceptions)
	}
	sb.WriteString("# HELP luma_eye_logs_total Total ingested client logs by project.\n")
	sb.WriteString("# TYPE luma_eye_logs_total counter\n")
	for _, p := range names {
		fmt.Fprintf(&sb, "luma_eye_logs_total{project=%q} %d\n", p, proj[p].Logs)
	}
	sb.WriteString("# HELP luma_eye_vitals_total Total ingested web vitals by project.\n")
	sb.WriteString("# TYPE luma_eye_vitals_total counter\n")
	for _, p := range names {
		fmt.Fprintf(&sb, "luma_eye_vitals_total{project=%q} %d\n", p, proj[p].Vitals)
	}
	sb.WriteString("# HELP luma_eye_dropped_total Telemetry items dropped due to full queue.\n")
	sb.WriteString("# TYPE luma_eye_dropped_total counter\n")
	fmt.Fprintf(&sb, "luma_eye_dropped_total %d\n", dropped)
	sb.WriteString("# HELP luma_eye_rate_limited_total Requests rejected by rate limiter.\n")
	sb.WriteString("# TYPE luma_eye_rate_limited_total counter\n")
	fmt.Fprintf(&sb, "luma_eye_rate_limited_total %d\n", rateLimited)
	sb.WriteString("# HELP luma_eye_relay_uncompressed_bytes_total Uncompressed batch bytes relayed.\n")
	sb.WriteString("# TYPE luma_eye_relay_uncompressed_bytes_total counter\n")
	fmt.Fprintf(&sb, "luma_eye_relay_uncompressed_bytes_total %d\n", rawB)
	sb.WriteString("# HELP luma_eye_relay_compressed_bytes_total Zstd compressed batch bytes relayed.\n")
	sb.WriteString("# TYPE luma_eye_relay_compressed_bytes_total counter\n")
	fmt.Fprintf(&sb, "luma_eye_relay_compressed_bytes_total %d\n", compB)
	sb.WriteString("# HELP luma_eye_clickhouse_errors_total Failed ClickHouse insert batches.\n")
	sb.WriteString("# TYPE luma_eye_clickhouse_errors_total counter\n")
	fmt.Fprintf(&sb, "luma_eye_clickhouse_errors_total %d\n", chErr)
	return sb.String()
}

type OTLPPusher struct {
	endpoint    string
	headers     map[string]string
	serviceName string
	resource    map[string]string
	metrics     *Metrics
	httpClient  *http.Client
}

func NewOTLPPusher(endpoint string, headers map[string]string, serviceName string, resource map[string]string, metrics *Metrics) *OTLPPusher {
	if strings.TrimSpace(endpoint) == "" {
		return nil
	}
	return &OTLPPusher{
		endpoint:    strings.TrimRight(strings.TrimSpace(endpoint), "/"),
		headers:     headers,
		serviceName: serviceName,
		resource:    resource,
		metrics:     metrics,
		httpClient:  &http.Client{Timeout: 5 * time.Second},
	}
}

func (p *OTLPPusher) Start(ctx context.Context, interval time.Duration) {
	if p == nil {
		return
	}
	if interval <= 0 {
		interval = 30 * time.Second
	}
	ticker := time.NewTicker(interval)
	go func() {
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				pushCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
				_ = p.PushOnce(pushCtx)
				cancel()
			}
		}
	}()
}

func (p *OTLPPusher) PushOnce(ctx context.Context) error {
	if p == nil || p.endpoint == "" {
		return nil
	}
	payload, err := p.BuildPayload(time.Now().UTC())
	if err != nil {
		return err
	}
	target := p.endpoint
	if !strings.HasSuffix(target, "/v1/metrics") {
		target += "/v1/metrics"
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, target, bytes.NewReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	for k, v := range p.headers {
		req.Header.Set(k, v)
	}
	resp, err := p.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body)
	if resp.StatusCode >= 300 {
		return fmt.Errorf("otlp push status %d", resp.StatusCode)
	}
	return nil
}

func (p *OTLPPusher) BuildPayload(now time.Time) ([]byte, error) {
	proj, dropped, _, _, _, chErr := p.metrics.Snapshot()
	nowNs := strconv.FormatInt(now.UnixNano(), 10)
	startNs := strconv.FormatInt(p.metrics.startUnixNano, 10)

	resAttrs := []map[string]any{
		{"key": "service.name", "value": map[string]any{"stringValue": p.serviceName}},
	}
	for k, v := range p.resource {
		if k == "service.name" {
			continue
		}
		resAttrs = append(resAttrs, map[string]any{
			"key":   k,
			"value": map[string]any{"stringValue": v},
		})
	}

	makeSum := func(name string, pts []map[string]any) map[string]any {
		return map[string]any{
			"name": name,
			"sum": map[string]any{
				"aggregationTemporality": 2, // CUMULATIVE
				"isMonotonic":            true,
				"dataPoints":             pts,
			},
		}
	}

	var eventPts, pvPts, excPts, logPts, vitalPts []map[string]any
	for slug, c := range proj {
		attrs := []map[string]any{
			{"key": "project", "value": map[string]any{"stringValue": slug}},
		}
		eventPts = append(eventPts, map[string]any{"startTimeUnixNano": startNs, "timeUnixNano": nowNs, "asInt": strconv.FormatUint(c.Events, 10), "attributes": attrs})
		pvPts = append(pvPts, map[string]any{"startTimeUnixNano": startNs, "timeUnixNano": nowNs, "asInt": strconv.FormatUint(c.Pageviews, 10), "attributes": attrs})
		excPts = append(excPts, map[string]any{"startTimeUnixNano": startNs, "timeUnixNano": nowNs, "asInt": strconv.FormatUint(c.Exceptions, 10), "attributes": attrs})
		logPts = append(logPts, map[string]any{"startTimeUnixNano": startNs, "timeUnixNano": nowNs, "asInt": strconv.FormatUint(c.Logs, 10), "attributes": attrs})
		vitalPts = append(vitalPts, map[string]any{"startTimeUnixNano": startNs, "timeUnixNano": nowNs, "asInt": strconv.FormatUint(c.Vitals, 10), "attributes": attrs})
	}

	metricsList := []map[string]any{
		makeSum("luma_eye_dropped_total", []map[string]any{{"startTimeUnixNano": startNs, "timeUnixNano": nowNs, "asInt": strconv.FormatUint(dropped, 10)}}),
		makeSum("luma_eye_clickhouse_errors_total", []map[string]any{{"startTimeUnixNano": startNs, "timeUnixNano": nowNs, "asInt": strconv.FormatUint(chErr, 10)}}),
	}
	if len(eventPts) > 0 {
		metricsList = append(metricsList,
			makeSum("luma_eye_events_total", eventPts),
			makeSum("luma_eye_pageviews_total", pvPts),
			makeSum("luma_eye_exceptions_total", excPts),
			makeSum("luma_eye_logs_total", logPts),
			makeSum("luma_eye_vitals_total", vitalPts),
		)
	}

	doc := map[string]any{
		"resourceMetrics": []map[string]any{
			{
				"resource": map[string]any{"attributes": resAttrs},
				"scopeMetrics": []map[string]any{
					{
						"scope":   map[string]any{"name": "luma-eye", "version": "1.0.0"},
						"metrics": metricsList,
					},
				},
			},
		},
	}
	return json.Marshal(doc)
}
