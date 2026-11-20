package clickhouse

import (
	"context"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/liutianjie/luma-eye/internal/model"
)

type OverviewStats struct {
	Project        string            `json:"project"`
	Window         string            `json:"window"`
	ActiveUsers5m  uint64            `json:"active_users_5m"`
	UniqueVisitors uint64            `json:"unique_visitors"`
	Pageviews      uint64            `json:"pageviews"`
	CustomEvents   uint64            `json:"custom_events"`
	Sessions       uint64            `json:"sessions"`
	ErrorCount     uint64            `json:"error_count"`
	CrashFreeRate  float64           `json:"crash_free_rate"`
	Timeseries     []TimeseriesPoint `json:"timeseries"`
	TopPages       []DimensionCount  `json:"top_pages"`
	TopEvents      []DimensionCount  `json:"top_events"`
	TopReferrers   []DimensionCount  `json:"top_referrers"`
	TopBrowsers    []DimensionCount  `json:"top_browsers"`
	TopOS          []DimensionCount  `json:"top_os"`
	TopDevices     []DimensionCount  `json:"top_devices"`
	TopCountries   []DimensionCount  `json:"top_countries"`
	TopSources     []DimensionCount  `json:"top_sources"`
	TopMediums     []DimensionCount  `json:"top_mediums"`
	TopCampaigns   []DimensionCount  `json:"top_campaigns"`
	TopChannels    []DimensionCount  `json:"top_channels"`
	TopPlatforms   []DimensionCount  `json:"top_platforms"`
	TopLanguages   []DimensionCount  `json:"top_languages"`
	VitalsP75      []VitalSummary    `json:"vitals_p75"`
}

type TimeseriesPoint struct {
	Hour      string `json:"hour"`
	Pageviews uint64 `json:"pageviews"`
	Events    uint64 `json:"events"`
	Visitors  uint64 `json:"visitors"`
	Errors    uint64 `json:"errors"`
}

type DimensionCount struct {
	Key   string `json:"key"`
	Count uint64 `json:"count"`
	Users uint64 `json:"users,omitempty"`
}

type VitalSummary struct {
	MetricName string  `json:"metric_name"`
	P75        float64 `json:"p75"`
	Samples    uint64  `json:"samples"`
}

type FunnelStepResult struct {
	Step           string  `json:"step"`
	StepIndex      int     `json:"step_index"`
	Users          uint64  `json:"users"`
	ConversionRate float64 `json:"conversion_rate"`
	StepRate       float64 `json:"step_rate"`
}

type FunnelResult struct {
	Project           string             `json:"project"`
	WindowSeconds     int                `json:"window_seconds"`
	Since             string             `json:"since"`
	Steps             []FunnelStepResult `json:"steps"`
	OverallConversion float64            `json:"overall_conversion"`
}

type RetentionRow struct {
	CohortDate  string    `json:"cohort_date"`
	CohortUsers uint64    `json:"cohort_users"`
	DayCounts   []uint64  `json:"day_counts"`
	DayRates    []float64 `json:"day_rates"`
}

type RetentionResult struct {
	Project string         `json:"project"`
	Days    int            `json:"days"`
	Rows    []RetentionRow `json:"rows"`
}

type IssueSummary struct {
	Project      string            `json:"project"`
	Fingerprint  string            `json:"fingerprint"`
	Status       model.IssueStatus `json:"status"`
	Level        string            `json:"level"`
	ErrorType    string            `json:"error_type"`
	ErrorMessage string            `json:"error_message"`
	Culprit      string            `json:"culprit"`
	LastRelease  string            `json:"last_release"`
	FirstSeen    string            `json:"first_seen"`
	LastSeen     string            `json:"last_seen"`
	Count        uint64            `json:"count"`
	Users        uint64            `json:"users"`
}

type IssueOccurrence struct {
	TS          string             `json:"ts"`
	ActorID     string             `json:"actor_id"`
	SessionID   string             `json:"session_id"`
	Env         string             `json:"env"`
	Release     string             `json:"release"`
	Level       string             `json:"level"`
	PageURL     string             `json:"page_url"`
	TraceID     string             `json:"trace_id"`
	Browser     string             `json:"browser"`
	OS          string             `json:"os"`
	DeviceType  string             `json:"device_type"`
	Frames      []model.StackFrame `json:"frames"`
	Breadcrumbs []model.Breadcrumb `json:"breadcrumbs"`
	Tags        map[string]string  `json:"tags"`
	Extra       map[string]any     `json:"extra,omitempty"`
}

type IssueDetail struct {
	Summary     IssueSummary       `json:"summary"`
	OccurredAt  string             `json:"occurred_at"`
	ActorID     string             `json:"actor_id"`
	SessionID   string             `json:"session_id"`
	Env         string             `json:"env"`
	Release     string             `json:"release"`
	PageURL     string             `json:"page_url"`
	TraceID     string             `json:"trace_id"`
	Browser     string             `json:"browser"`
	OS          string             `json:"os"`
	DeviceType  string             `json:"device_type"`
	Frames      []model.StackFrame `json:"frames"`
	Breadcrumbs []model.Breadcrumb `json:"breadcrumbs"`
	Tags        map[string]string  `json:"tags"`
	Extra       map[string]any     `json:"extra,omitempty"`
	Occurrences []IssueOccurrence  `json:"occurrences"`
}

type LogView struct {
	TS        string         `json:"ts"`
	Project   string         `json:"project"`
	Env       string         `json:"env"`
	Release   string         `json:"release"`
	Level     string         `json:"level"`
	Message   string         `json:"message"`
	ActorID   string         `json:"actor_id"`
	SessionID string         `json:"session_id"`
	PageURL   string         `json:"page_url"`
	TraceID   string         `json:"trace_id"`
	Attrs     map[string]any `json:"attrs,omitempty"`
}

func ParseSince(raw string, fallback time.Duration) time.Duration {
	raw = strings.TrimSpace(strings.ToLower(raw))
	if raw == "" {
		return fallback
	}
	if strings.HasSuffix(raw, "d") {
		n, err := strconv.Atoi(strings.TrimSuffix(raw, "d"))
		if err == nil && n > 0 {
			return time.Duration(n) * 24 * time.Hour
		}
	}
	if d, err := time.ParseDuration(raw); err == nil && d > 0 {
		return d
	}
	return fallback
}

func (c *Client) GetOverview(ctx context.Context, project string, since time.Duration) (*OverviewStats, error) {
	sinceSec := int(since.Seconds())
	if sinceSec <= 0 {
		sinceSec = 86400
	}
	params := map[string]string{
		"project":  project,
		"sinceSec": strconv.Itoa(sinceSec),
	}

	out := &OverviewStats{
		Project:       project,
		Window:        since.String(),
		CrashFreeRate: 100.0,
	}

	// 1. Aggregate totals from events_hourly
	type totalRow struct {
		UV        uint64 `json:"uv"`
		Sessions  uint64 `json:"sessions"`
		Pageviews uint64 `json:"pageviews"`
		Custom    uint64 `json:"custom_events"`
	}
	totals, err := QueryJSON[totalRow](ctx, c, `
SELECT
    uniqCombinedMerge(12)(uv_state) AS uv,
    uniqCombinedMerge(12)(session_state) AS sessions,
    sumIf(event_count, event_type = 'pageview') AS pageviews,
    sumIf(event_count, event_type = 'custom') AS custom_events
FROM {{DB}}.events_hourly
WHERE project = {project:String}
  AND hour >= toStartOfHour(now('UTC') - toIntervalSecond({sinceSec:UInt32}))
`, params)
	if err != nil {
		return nil, err
	}
	if len(totals) > 0 {
		out.UniqueVisitors = totals[0].UV
		out.Sessions = totals[0].Sessions
		out.Pageviews = totals[0].Pageviews
		out.CustomEvents = totals[0].Custom
	}

	// 2. Active users in last 5 minutes
	type activeRow struct {
		Active uint64 `json:"active"`
	}
	actives, err := QueryJSON[activeRow](ctx, c, `
SELECT uniqExactIf(actor_id, actor_id != '') AS active
FROM {{DB}}.events
WHERE project = {project:String}
  AND ts >= now('UTC') - INTERVAL 5 MINUTE
`, params)
	if err == nil && len(actives) > 0 {
		out.ActiveUsers5m = actives[0].Active
	}

	// 3. Error count & impacted sessions
	type errRow struct {
		Errors          uint64 `json:"errors"`
		ErroredSessions uint64 `json:"errored_sessions"`
	}
	errs, err := QueryJSON[errRow](ctx, c, `
SELECT
    sum(toUInt64(occurrence_weight)) AS errors,
    uniqExactIf(session_id, session_id != '') AS errored_sessions
FROM {{DB}}.exceptions
WHERE project = {project:String}
  AND ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
`, params)
	if err == nil && len(errs) > 0 {
		out.ErrorCount = errs[0].Errors
		if out.Sessions > 0 {
			crashed := errs[0].ErroredSessions
			if crashed > out.Sessions {
				crashed = out.Sessions
			}
			out.CrashFreeRate = float64(out.Sessions-crashed) * 100.0 / float64(out.Sessions)
		}
	}

	// 4. Hourly timeseries
	type tsEventRow struct {
		Hour      string `json:"hour_str"`
		Pageviews uint64 `json:"pageviews"`
		Events    uint64 `json:"events"`
		Visitors  uint64 `json:"visitors"`
	}
	evSeries, err := QueryJSON[tsEventRow](ctx, c, `
SELECT
    formatDateTime(eh.hour, '%Y-%m-%d %H:00:00') AS hour_str,
    sumIf(eh.event_count, eh.event_type = 'pageview') AS pageviews,
    sum(eh.event_count) AS events,
    uniqCombinedMerge(12)(eh.uv_state) AS visitors
FROM {{DB}}.events_hourly AS eh
WHERE eh.project = {project:String}
  AND eh.hour >= toStartOfHour(now('UTC') - toIntervalSecond({sinceSec:UInt32}))
GROUP BY hour_str
ORDER BY hour_str ASC
`, params)
	if err == nil {
		type errHourRow struct {
			Hour   string `json:"hour_str"`
			Errors uint64 `json:"errors"`
		}
		errSeries, _ := QueryJSON[errHourRow](ctx, c, `
SELECT
    formatDateTime(toStartOfHour(e.ts), '%Y-%m-%d %H:00:00') AS hour_str,
    sum(toUInt64(e.occurrence_weight)) AS errors
FROM {{DB}}.exceptions AS e
WHERE e.project = {project:String}
  AND e.ts >= toStartOfHour(now('UTC') - toIntervalSecond({sinceSec:UInt32}))
GROUP BY hour_str
`, params)
		errByHour := make(map[string]uint64, len(errSeries))
		for _, r := range errSeries {
			errByHour[r.Hour] = r.Errors
		}
		for _, r := range evSeries {
			out.Timeseries = append(out.Timeseries, TimeseriesPoint{
				Hour:      r.Hour,
				Pageviews: r.Pageviews,
				Events:    r.Events,
				Visitors:  r.Visitors,
				Errors:    errByHour[r.Hour],
			})
		}
	}

	// 5. Breakdowns
	out.TopPages, _ = QueryJSON[DimensionCount](ctx, c, `
SELECT page_path AS key, count() AS count, uniqExact(actor_id) AS users
FROM {{DB}}.events
WHERE project = {project:String} AND event_type = 'pageview' AND ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
GROUP BY key ORDER BY count DESC LIMIT 10
`, params)

	out.TopEvents, _ = QueryJSON[DimensionCount](ctx, c, `
SELECT event_name AS key, count() AS count, uniqExact(actor_id) AS users
FROM {{DB}}.events
WHERE project = {project:String} AND event_type = 'custom' AND ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
GROUP BY key ORDER BY count DESC LIMIT 10
`, params)

	out.TopReferrers, _ = QueryJSON[DimensionCount](ctx, c, `
SELECT if(referrer = '', '(direct)', referrer) AS key, count() AS count, uniqExact(actor_id) AS users
FROM {{DB}}.events
WHERE project = {project:String} AND event_type = 'pageview' AND ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
GROUP BY key ORDER BY count DESC LIMIT 10
`, params)

	out.TopBrowsers, _ = QueryJSON[DimensionCount](ctx, c, `
SELECT browser AS key, uniqExact(actor_id) AS users, count() AS count
FROM {{DB}}.events
WHERE project = {project:String} AND ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
GROUP BY key ORDER BY users DESC LIMIT 8
`, params)

	out.TopOS, _ = QueryJSON[DimensionCount](ctx, c, `
SELECT os AS key, uniqExact(actor_id) AS users, count() AS count
FROM {{DB}}.events
WHERE project = {project:String} AND ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
GROUP BY key ORDER BY users DESC LIMIT 8
`, params)

	out.TopDevices, _ = QueryJSON[DimensionCount](ctx, c, `
SELECT device_type AS key, uniqExact(actor_id) AS users, count() AS count
FROM {{DB}}.events
WHERE project = {project:String} AND ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
GROUP BY key ORDER BY users DESC LIMIT 6
`, params)

	out.TopCountries, _ = QueryJSON[DimensionCount](ctx, c, `
SELECT if(country = '', '(unknown)', country) AS key, count() AS count, uniqExact(actor_id) AS users
FROM {{DB}}.events
WHERE project = {project:String} AND event_type = 'pageview' AND ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
GROUP BY key ORDER BY count DESC LIMIT 20
`, params)
	out.TopSources, _ = QueryJSON[DimensionCount](ctx, c, `
SELECT if(utm_source = '', '(unknown)', utm_source) AS key, count() AS count, uniqExact(actor_id) AS users
FROM {{DB}}.events
WHERE project = {project:String} AND event_type = 'pageview' AND ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
GROUP BY key ORDER BY count DESC LIMIT 20
`, params)
	out.TopMediums, _ = QueryJSON[DimensionCount](ctx, c, `
SELECT if(utm_medium = '', '(unknown)', utm_medium) AS key, count() AS count, uniqExact(actor_id) AS users
FROM {{DB}}.events
WHERE project = {project:String} AND event_type = 'pageview' AND ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
GROUP BY key ORDER BY count DESC LIMIT 20
`, params)
	out.TopCampaigns, _ = QueryJSON[DimensionCount](ctx, c, `
SELECT if(utm_campaign = '', '(unknown)', utm_campaign) AS key, count() AS count, uniqExact(actor_id) AS users
FROM {{DB}}.events
WHERE project = {project:String} AND event_type = 'pageview' AND ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
GROUP BY key ORDER BY count DESC LIMIT 20
`, params)
	out.TopChannels, _ = QueryJSON[DimensionCount](ctx, c, `
SELECT if(JSONExtractString(props_json, 'channel') = '', '(unknown)', JSONExtractString(props_json, 'channel')) AS key, count() AS count, uniqExact(actor_id) AS users
FROM {{DB}}.events
WHERE project = {project:String} AND event_type = 'pageview' AND ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
GROUP BY key ORDER BY count DESC LIMIT 20
`, params)
	out.TopPlatforms, _ = QueryJSON[DimensionCount](ctx, c, `
SELECT if(JSONExtractString(props_json, 'client_platform') = '', '(unknown)', JSONExtractString(props_json, 'client_platform')) AS key, count() AS count, uniqExact(actor_id) AS users
FROM {{DB}}.events
WHERE project = {project:String} AND event_type = 'pageview' AND ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
GROUP BY key ORDER BY count DESC LIMIT 20
`, params)
	out.TopLanguages, _ = QueryJSON[DimensionCount](ctx, c, `
SELECT if(JSONExtractString(props_json, 'browser_language') = '', '(unknown)', JSONExtractString(props_json, 'browser_language')) AS key, count() AS count, uniqExact(actor_id) AS users
FROM {{DB}}.events
WHERE project = {project:String} AND event_type = 'pageview' AND ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
GROUP BY key ORDER BY count DESC LIMIT 20
`, params)

	// 6. Web Vitals P75
	out.VitalsP75, _ = QueryJSON[VitalSummary](ctx, c, `
SELECT
    metric_name,
    quantileExact(0.75)(value) AS p75,
    count() AS samples
FROM {{DB}}.vitals
WHERE project = {project:String} AND ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
GROUP BY metric_name
ORDER BY metric_name ASC
`, params)

	return out, nil
}

func (c *Client) GetFunnel(ctx context.Context, project string, steps []string, windowSec int, since time.Duration) (*FunnelResult, error) {
	if len(steps) < 2 {
		return nil, fmt.Errorf("funnel requires at least 2 steps")
	}
	if len(steps) > 10 {
		return nil, fmt.Errorf("funnel supports at most 10 steps")
	}
	if windowSec <= 0 {
		windowSec = 3600
	}
	sinceSec := int(since.Seconds())
	if sinceSec <= 0 {
		sinceSec = 7 * 86400
	}

	params := map[string]string{
		"project":   project,
		"windowSec": strconv.Itoa(windowSec),
		"sinceSec":  strconv.Itoa(sinceSec),
	}

	conds := make([]string, len(steps))
	inNames := make([]string, len(steps))
	for i, step := range steps {
		pName := fmt.Sprintf("step_%d", i)
		params[pName] = strings.TrimSpace(step)
		conds[i] = fmt.Sprintf("(event_name = {%s:String} OR page_path = {%s:String})", pName, pName)
		inNames[i] = fmt.Sprintf("{%s:String}", pName)
	}

	query := fmt.Sprintf(`
SELECT level, count() AS users
FROM (
    SELECT
        actor_id,
        windowFunnel({windowSec:UInt32})(toDateTime(ts), %s) AS level
    FROM {{DB}}.events
    WHERE project = {project:String}
      AND actor_id != ''
      AND ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
      AND (event_name IN (%s) OR page_path IN (%s))
    GROUP BY actor_id
)
WHERE level > 0
GROUP BY level
ORDER BY level ASC
`, strings.Join(conds, ", "), strings.Join(inNames, ", "), strings.Join(inNames, ", "))

	type levelRow struct {
		Level int    `json:"level"`
		Users uint64 `json:"users"`
	}
	rows, err := QueryJSON[levelRow](ctx, c, query, params)
	if err != nil {
		return nil, err
	}

	// Users who reached level >= K passed step K (index K-1).
	atOrAbove := make([]uint64, len(steps))
	for _, r := range rows {
		for idx := 0; idx < r.Level && idx < len(steps); idx++ {
			atOrAbove[idx] += r.Users
		}
	}

	res := &FunnelResult{
		Project:       project,
		WindowSeconds: windowSec,
		Since:         since.String(),
		Steps:         make([]FunnelStepResult, len(steps)),
	}
	firstUsers := atOrAbove[0]
	for i, step := range steps {
		u := atOrAbove[i]
		overall := 0.0
		if firstUsers > 0 {
			overall = float64(u) * 100.0 / float64(firstUsers)
		}
		stepRate := 100.0
		if i > 0 {
			if atOrAbove[i-1] > 0 {
				stepRate = float64(u) * 100.0 / float64(atOrAbove[i-1])
			} else {
				stepRate = 0
			}
		}
		res.Steps[i] = FunnelStepResult{
			Step:           step,
			StepIndex:      i + 1,
			Users:          u,
			ConversionRate: overall,
			StepRate:       stepRate,
		}
	}
	if firstUsers > 0 {
		res.OverallConversion = float64(atOrAbove[len(steps)-1]) * 100.0 / float64(firstUsers)
	}
	return res, nil
}

func (c *Client) GetRetention(ctx context.Context, project string, days int) (*RetentionResult, error) {
	if days <= 0 || days > 30 {
		days = 7
	}
	params := map[string]string{
		"project": project,
		"days":    strconv.Itoa(days),
	}

	type rawRetRow struct {
		CohortDate string `json:"cohort_date"`
		DayOffset  int    `json:"day_offset"`
		Users      uint64 `json:"users"`
	}
	rows, err := QueryJSON[rawRetRow](ctx, c, `
WITH
    first_seen AS (
        SELECT actor_id, min(toDate(ts)) AS cohort_day
        FROM {{DB}}.events
        WHERE project = {project:String}
          AND actor_id != ''
          AND ts >= toDate(now('UTC')) - toIntervalDay({days:UInt32} * 2)
        GROUP BY actor_id
    ),
    activity AS (
        SELECT DISTINCT actor_id, toDate(ts) AS active_day
        FROM {{DB}}.events
        WHERE project = {project:String}
          AND actor_id != ''
          AND ts >= toDate(now('UTC')) - toIntervalDay({days:UInt32} * 2)
    )
SELECT
    toString(f.cohort_day) AS cohort_date,
    dateDiff('day', f.cohort_day, a.active_day) AS day_offset,
    uniqExact(f.actor_id) AS users
FROM first_seen f
INNER JOIN activity a ON f.actor_id = a.actor_id
WHERE f.cohort_day >= toDate(now('UTC')) - toIntervalDay({days:UInt32})
  AND day_offset >= 0
  AND day_offset <= {days:UInt32}
GROUP BY cohort_date, day_offset
ORDER BY cohort_date DESC, day_offset ASC
`, params)
	if err != nil {
		return nil, err
	}

	byCohort := make(map[string][]uint64)
	var cohortOrder []string
	for _, r := range rows {
		arr, ok := byCohort[r.CohortDate]
		if !ok {
			arr = make([]uint64, days+1)
			byCohort[r.CohortDate] = arr
			cohortOrder = append(cohortOrder, r.CohortDate)
		}
		if r.DayOffset >= 0 && r.DayOffset <= days {
			arr[r.DayOffset] = r.Users
		}
	}

	res := &RetentionResult{
		Project: project,
		Days:    days,
		Rows:    make([]RetentionRow, 0, len(cohortOrder)),
	}
	for _, date := range cohortOrder {
		counts := byCohort[date]
		base := counts[0]
		rates := make([]float64, len(counts))
		for i, cnt := range counts {
			if base > 0 {
				rates[i] = float64(cnt) * 100.0 / float64(base)
			}
		}
		res.Rows = append(res.Rows, RetentionRow{
			CohortDate:  date,
			CohortUsers: base,
			DayCounts:   counts,
			DayRates:    rates,
		})
	}
	return res, nil
}

func (c *Client) ListIssues(ctx context.Context, project string, since time.Duration, limit int, states map[string]model.IssueState, filterStatus string) ([]IssueSummary, error) {
	if limit <= 0 || limit > 200 {
		limit = 50
	}
	sinceSec := int(since.Seconds())
	if sinceSec <= 0 {
		sinceSec = 7 * 86400
	}
	filtering := filterStatus != "" && filterStatus != "all"

	type rollupRow struct {
		Fingerprint  string `json:"fingerprint"`
		FirstSeen    string `json:"first_seen"`
		LastSeen     string `json:"last_seen"`
		Count        uint64 `json:"total_count"`
		Users        uint64 `json:"users"`
		ErrorType    string `json:"error_type"`
		ErrorMessage string `json:"error_message"`
		Culprit      string `json:"culprit"`
		Level        string `json:"level"`
		LastRelease  string `json:"last_release"`
	}

	const listIssuesSQL = `
SELECT
    fingerprint,
    formatDateTime(first_seen_ts, '%Y-%m-%dT%H:%i:%SZ') AS first_seen,
    formatDateTime(last_seen_ts, '%Y-%m-%dT%H:%i:%SZ') AS last_seen,
    total_count,
    users,
    error_type,
    error_message,
    culprit,
    level,
    last_release
FROM (
    SELECT
        fingerprint,
        min(first_seen) AS first_seen_ts,
        max(last_seen) AS last_seen_ts,
        sum(total_count) AS total_count,
        uniqCombinedMerge(12)(users_state) AS users,
        anyLast(error_type) AS error_type,
        anyLast(error_message) AS error_message,
        anyLast(culprit) AS culprit,
        anyLast(level) AS level,
        anyLast(last_release) AS last_release
    FROM {{DB}}.issue_rollups
    WHERE project = {project:String}
    GROUP BY fingerprint
    HAVING last_seen_ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
    ORDER BY last_seen_ts DESC, fingerprint ASC
    LIMIT {limit:UInt32} OFFSET {offset:UInt32}
)
`

	pageSize := limit
	if filtering {
		pageSize = limit * 4
		if pageSize < 200 {
			pageSize = 200
		}
		if pageSize > 500 {
			pageSize = 500
		}
	}
	const maxScanRows = 5000

	out := make([]IssueSummary, 0, limit)
	for offset := 0; offset < maxScanRows && len(out) < limit; {
		params := map[string]string{
			"project":  project,
			"sinceSec": strconv.Itoa(sinceSec),
			"limit":    strconv.Itoa(pageSize),
			"offset":   strconv.Itoa(offset),
		}
		rows, err := QueryJSON[rollupRow](ctx, c, listIssuesSQL, params)
		if err != nil {
			return nil, err
		}
		for _, r := range rows {
			st := model.IssueOpen
			if s, ok := states[r.Fingerprint]; ok && s.Status != "" {
				st = s.Status
			}
			if filtering && string(st) != filterStatus {
				continue
			}
			out = append(out, IssueSummary{
				Project:      project,
				Fingerprint:  r.Fingerprint,
				Status:       st,
				Level:        r.Level,
				ErrorType:    r.ErrorType,
				ErrorMessage: r.ErrorMessage,
				Culprit:      r.Culprit,
				LastRelease:  r.LastRelease,
				FirstSeen:    r.FirstSeen,
				LastSeen:     r.LastSeen,
				Count:        r.Count,
				Users:        r.Users,
			})
			if len(out) >= limit {
				break
			}
		}
		if !filtering || len(rows) < pageSize {
			break
		}
		offset += len(rows)
	}
	return out, nil
}

func (c *Client) GetIssueDetail(ctx context.Context, project, fingerprint string, states map[string]model.IssueState) (*IssueDetail, error) {
	params := map[string]string{
		"project":     project,
		"fingerprint": fingerprint,
	}

	type rollupRow struct {
		FirstSeen   string `json:"first_seen"`
		LastSeen    string `json:"last_seen"`
		TotalCount  uint64 `json:"total_count"`
		Users       uint64 `json:"users"`
		LastRelease string `json:"last_release"`
	}
	rollups, err := QueryJSON[rollupRow](ctx, c, `
SELECT
    formatDateTime(min(first_seen), '%Y-%m-%dT%H:%i:%SZ') AS first_seen,
    formatDateTime(max(last_seen), '%Y-%m-%dT%H:%i:%SZ') AS last_seen,
    sum(total_count) AS total_count,
    uniqCombinedMerge(12)(users_state) AS users,
    anyLast(last_release) AS last_release
FROM {{DB}}.issue_rollups
WHERE project = {project:String} AND fingerprint = {fingerprint:String}
GROUP BY fingerprint
`, params)
	if err != nil {
		return nil, err
	}

	type excRow struct {
		TS              string `json:"ts_str"`
		ActorID         string `json:"actor_id"`
		SessionID       string `json:"session_id"`
		Env             string `json:"env"`
		Release         string `json:"release"`
		Level           string `json:"level"`
		ErrorType       string `json:"error_type"`
		ErrorMessage    string `json:"error_message"`
		Culprit         string `json:"culprit"`
		PageURL         string `json:"page_url"`
		TraceID         string `json:"trace_id"`
		Browser         string `json:"browser"`
		OS              string `json:"os"`
		DeviceType      string `json:"device_type"`
		FramesJSON      string `json:"frames_json"`
		BreadcrumbsJSON string `json:"breadcrumbs_json"`
		TagsJSON        string `json:"tags_json"`
		ExtraJSON       string `json:"extra_json"`
	}
	rows, err := QueryJSON[excRow](ctx, c, `
SELECT
    formatDateTime(e.ts, '%Y-%m-%dT%H:%i:%SZ') AS ts_str,
    e.actor_id,
    e.session_id,
    e.env,
    e.release,
    e.level,
    e.error_type,
    e.error_message,
    e.culprit,
    e.page_url,
    e.trace_id,
    e.browser,
    e.os,
    e.device_type,
    e.frames_json,
    e.breadcrumbs_json,
    e.tags_json,
    e.extra_json
FROM {{DB}}.exceptions AS e
WHERE e.project = {project:String} AND e.fingerprint = {fingerprint:String}
ORDER BY e.ts DESC
LIMIT 15
`, params)
	if err != nil {
		return nil, err
	}
	if len(rows) == 0 {
		return nil, fmt.Errorf("issue not found")
	}
	r := rows[0]

	st := model.IssueOpen
	if s, ok := states[fingerprint]; ok && s.Status != "" {
		st = s.Status
	}

	summary := IssueSummary{
		Project:      project,
		Fingerprint:  fingerprint,
		Status:       st,
		Level:        r.Level,
		ErrorType:    r.ErrorType,
		ErrorMessage: r.ErrorMessage,
		Culprit:      r.Culprit,
		LastRelease:  r.Release,
		FirstSeen:    r.TS,
		LastSeen:     r.TS,
		Count:        uint64(len(rows)),
		Users:        1,
	}
	if len(rollups) > 0 {
		summary.FirstSeen = rollups[0].FirstSeen
		summary.LastSeen = rollups[0].LastSeen
		summary.Count = rollups[0].TotalCount
		summary.Users = rollups[0].Users
		if rollups[0].LastRelease != "" {
			summary.LastRelease = rollups[0].LastRelease
		}
	}

	occurrences := make([]IssueOccurrence, 0, len(rows))
	for _, row := range rows {
		occ := IssueOccurrence{
			TS:         row.TS,
			ActorID:    row.ActorID,
			SessionID:  row.SessionID,
			Env:        row.Env,
			Release:    row.Release,
			Level:      row.Level,
			PageURL:    row.PageURL,
			TraceID:    row.TraceID,
			Browser:    row.Browser,
			OS:         row.OS,
			DeviceType: row.DeviceType,
		}
		_ = json.Unmarshal([]byte(row.FramesJSON), &occ.Frames)
		_ = json.Unmarshal([]byte(row.BreadcrumbsJSON), &occ.Breadcrumbs)
		_ = json.Unmarshal([]byte(row.TagsJSON), &occ.Tags)
		if row.ExtraJSON != "" && row.ExtraJSON != "{}" {
			_ = json.Unmarshal([]byte(row.ExtraJSON), &occ.Extra)
		}
		if occ.Frames == nil {
			occ.Frames = []model.StackFrame{}
		}
		if occ.Breadcrumbs == nil {
			occ.Breadcrumbs = []model.Breadcrumb{}
		}
		if occ.Tags == nil {
			occ.Tags = map[string]string{}
		}
		occurrences = append(occurrences, occ)
	}

	first := occurrences[0]
	detail := &IssueDetail{
		Summary:     summary,
		OccurredAt:  first.TS,
		ActorID:     first.ActorID,
		SessionID:   first.SessionID,
		Env:         first.Env,
		Release:     first.Release,
		PageURL:     first.PageURL,
		TraceID:     first.TraceID,
		Browser:     first.Browser,
		OS:          first.OS,
		DeviceType:  first.DeviceType,
		Frames:      first.Frames,
		Breadcrumbs: first.Breadcrumbs,
		Tags:        first.Tags,
		Extra:       first.Extra,
		Occurrences: occurrences,
	}
	return detail, nil
}

func (c *Client) SearchLogs(ctx context.Context, project, level, search, traceID string, since time.Duration, limit int) ([]LogView, error) {
	return c.SearchLogsFiltered(ctx, project, level, search, traceID, "", "", since, limit)
}

func (c *Client) SearchLogsFiltered(ctx context.Context, project, level, search, traceID, actorID, sessionID string, since time.Duration, limit int) ([]LogView, error) {
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	sinceSec := int(since.Seconds())
	if sinceSec <= 0 {
		sinceSec = 3600
	}
	params := map[string]string{
		"project":  project,
		"sinceSec": strconv.Itoa(sinceSec),
		"limit":    strconv.Itoa(limit),
	}

	var clauses []string
	clauses = append(clauses, "l.project = {project:String}", "l.ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})")
	if level != "" && level != "all" {
		params["level"] = strings.ToLower(level)
		clauses = append(clauses, "l.level = {level:String}")
	}
	if traceID != "" {
		params["traceID"] = traceID
		clauses = append(clauses, "l.trace_id = {traceID:String}")
	}
	if actorID != "" {
		params["actorID"] = actorID
		clauses = append(clauses, "l.actor_id = {actorID:String}")
	}
	if sessionID != "" {
		params["sessionID"] = sessionID
		clauses = append(clauses, "l.session_id = {sessionID:String}")
	}
	if search != "" {
		params["search"] = search
		clauses = append(clauses, "(positionCaseInsensitive(l.message, {search:String}) > 0 OR positionCaseInsensitive(l.attrs_json, {search:String}) > 0)")
	}

	type rawLog struct {
		TS        string `json:"ts_str"`
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
	sqlText := fmt.Sprintf(`
SELECT
    formatDateTime(l.ts, '%%Y-%%m-%%dT%%H:%%i:%%SZ') AS ts_str,
    l.project,
    l.env,
    l.release,
    l.level,
    l.message,
    l.actor_id,
    l.session_id,
    l.page_url,
    l.trace_id,
    l.attrs_json
FROM {{DB}}.logs AS l
WHERE %s
ORDER BY l.ts DESC
LIMIT {limit:UInt32}
`, strings.Join(clauses, " AND "))

	rows, err := QueryJSON[rawLog](ctx, c, sqlText, params)
	if err != nil {
		return nil, err
	}
	out := make([]LogView, 0, len(rows))
	for _, r := range rows {
		lv := LogView{
			TS:        r.TS,
			Project:   r.Project,
			Env:       r.Env,
			Release:   r.Release,
			Level:     r.Level,
			Message:   r.Message,
			ActorID:   r.ActorID,
			SessionID: r.SessionID,
			PageURL:   r.PageURL,
			TraceID:   r.TraceID,
		}
		if r.AttrsJSON != "" && r.AttrsJSON != "{}" {
			_ = json.Unmarshal([]byte(r.AttrsJSON), &lv.Attrs)
		}
		out = append(out, lv)
	}
	return out, nil
}
