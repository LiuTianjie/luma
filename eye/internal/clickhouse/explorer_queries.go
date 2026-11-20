package clickhouse

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/liutianjie/luma-eye/internal/model"
)

type PortfolioProjectStats struct {
	Project        *model.Project `json:"project"`
	ActiveUsers5m  uint64         `json:"active_users_5m"`
	UniqueVisitors uint64         `json:"unique_visitors"`
	Pageviews      uint64         `json:"pageviews"`
	CustomEvents   uint64         `json:"custom_events"`
	Sessions       uint64         `json:"sessions"`
	ErrorCount     uint64         `json:"error_count"`
	OpenIssues     int            `json:"open_issues"`
	CrashFreeRate  float64        `json:"crash_free_rate"`
	LCPP75         float64        `json:"lcp_p75"`
	INPP75         float64        `json:"inp_p75"`
	CLSP75         float64        `json:"cls_p75"`
	LastEventAt    string         `json:"last_event_at"`
	Sparkline      []uint64       `json:"sparkline"`
}

type PortfolioOverview struct {
	Window               string                  `json:"window"`
	TotalProjects        int                     `json:"total_projects"`
	ActiveProjects       int                     `json:"active_projects"`
	ActiveUsers5m        uint64                  `json:"active_users_5m"`
	UniqueVisitors       uint64                  `json:"unique_visitors"`
	Pageviews            uint64                  `json:"pageviews"`
	CustomEvents         uint64                  `json:"custom_events"`
	Sessions             uint64                  `json:"sessions"`
	ErrorCount           uint64                  `json:"error_count"`
	OpenIssues           int                     `json:"open_issues"`
	OverallCrashFreeRate float64                 `json:"overall_crash_free_rate"`
	Projects             []PortfolioProjectStats `json:"projects"`
}

type PageMetricRow struct {
	PagePath      string  `json:"page_path"`
	Pageviews     uint64  `json:"pageviews"`
	Users         uint64  `json:"users"`
	AvgDurationMS float64 `json:"avg_duration_ms"`
	P75DurationMS float64 `json:"p75_duration_ms"`
	ErrorCount    uint64  `json:"error_count"`
	LCPP75        float64 `json:"lcp_p75"`
	INPP75        float64 `json:"inp_p75"`
	CLSP75        float64 `json:"cls_p75"`
	FCPP75        float64 `json:"fcp_p75"`
	TTFBP75       float64 `json:"ttfb_p75"`
	VitalSamples  uint64  `json:"vital_samples"`
}

type EventQueryFilter struct {
	Since     time.Duration
	EventType string
	EventName string
	ActorID   string
	SessionID string
	Country   string
	Search    string
	Limit     int
}

type EventView struct {
	TS          string         `json:"ts"`
	EventType   string         `json:"event_type"`
	EventName   string         `json:"event_name"`
	ActorID     string         `json:"actor_id"`
	SessionID   string         `json:"session_id"`
	Env         string         `json:"env"`
	Release     string         `json:"release"`
	PagePath    string         `json:"page_path"`
	PageURL     string         `json:"page_url"`
	Referrer    string         `json:"referrer"`
	UTMSource   string         `json:"utm_source"`
	UTMMedium   string         `json:"utm_medium"`
	UTMCampaign string         `json:"utm_campaign"`
	DurationMS  uint32         `json:"duration_ms"`
	Browser     string         `json:"browser"`
	OS          string         `json:"os"`
	DeviceType  string         `json:"device_type"`
	Country     string         `json:"country"`
	TraceID     string         `json:"trace_id"`
	Props       map[string]any `json:"props,omitempty"`
}

type SessionQueryFilter struct {
	Since    time.Duration
	ActorID  string
	Country  string
	Search   string
	HasError bool
	Limit    int
}

type SessionSummary struct {
	SessionID    string `json:"session_id"`
	ActorID      string `json:"actor_id"`
	StartTS      string `json:"start_ts"`
	LastTS       string `json:"last_ts"`
	DurationSec  uint64 `json:"duration_sec"`
	Pageviews    uint64 `json:"pageviews"`
	CustomEvents uint64 `json:"custom_events"`
	TotalEvents  uint64 `json:"total_events"`
	ErrorCount   uint64 `json:"error_count"`
	LogCount     uint64 `json:"log_count"`
	EntryPage    string `json:"entry_page"`
	ExitPage     string `json:"exit_page"`
	Browser      string `json:"browser"`
	OS           string `json:"os"`
	DeviceType   string `json:"device_type"`
	Country      string `json:"country"`
	Env          string `json:"env"`
	Release      string `json:"release"`
	HasReplay    bool   `json:"has_replay"`
}

type TimelineItem struct {
	ID           string             `json:"id"`
	Kind         string             `json:"kind"` // pageview | custom | identify | lifecycle | vital | log | exception
	TS           string             `json:"ts"`
	TSMillis     int64              `json:"ts_ms"`
	OffsetMS     int64              `json:"offset_ms"`
	SessionID    string             `json:"session_id"`
	ActorID      string             `json:"actor_id"`
	Title        string             `json:"title"`
	Subtitle     string             `json:"subtitle,omitempty"`
	Level        string             `json:"level,omitempty"`
	PagePath     string             `json:"page_path,omitempty"`
	PageURL      string             `json:"page_url,omitempty"`
	DurationMS   uint32             `json:"duration_ms,omitempty"`
	TraceID      string             `json:"trace_id,omitempty"`
	MetricName   string             `json:"metric_name,omitempty"`
	MetricValue  float64            `json:"metric_value,omitempty"`
	MetricRating string             `json:"metric_rating,omitempty"`
	Fingerprint  string             `json:"fingerprint,omitempty"`
	ErrorType    string             `json:"error_type,omitempty"`
	ErrorMessage string             `json:"error_message,omitempty"`
	Culprit      string             `json:"culprit,omitempty"`
	Props        map[string]any     `json:"props,omitempty"`
	Tags         map[string]string  `json:"tags,omitempty"`
	Frames       []model.StackFrame `json:"frames,omitempty"`
	Breadcrumbs  []model.Breadcrumb `json:"breadcrumbs,omitempty"`
}

type TimelineResponse struct {
	Project      string           `json:"project"`
	ActorID      string           `json:"actor_id"`
	SessionID    string           `json:"session_id"`
	Browser      string           `json:"browser"`
	OS           string           `json:"os"`
	DeviceType   string           `json:"device_type"`
	Country      string           `json:"country"`
	Env          string           `json:"env"`
	Release      string           `json:"release"`
	StartTS      string           `json:"start_ts"`
	EndTS        string           `json:"end_ts"`
	DurationMS   int64            `json:"duration_ms"`
	TotalItems   int              `json:"total_items"`
	ErrorCount   int              `json:"error_count"`
	HasReplay    bool             `json:"has_replay"`
	Items        []TimelineItem   `json:"items"`
	UserSessions []SessionSummary `json:"user_sessions"`
}

func (c *Client) GetPortfolioOverview(
	ctx context.Context,
	projects []*model.Project,
	statesByProject map[string]map[string]model.IssueState,
	since time.Duration,
) (*PortfolioOverview, error) {
	sinceSec := int(since.Seconds())
	if sinceSec <= 0 {
		sinceSec = 86400
	}
	out := &PortfolioOverview{
		Window:               since.String(),
		TotalProjects:        len(projects),
		OverallCrashFreeRate: 100.0,
		Projects:             make([]PortfolioProjectStats, 0, len(projects)),
	}
	if len(projects) == 0 {
		return out, nil
	}
	if c == nil {
		for _, p := range projects {
			out.Projects = append(out.Projects, PortfolioProjectStats{
				Project:       p,
				CrashFreeRate: 100.0,
				Sparkline:     []uint64{},
			})
		}
		return out, nil
	}

	params := map[string]string{
		"sinceSec": strconv.Itoa(sinceSec),
	}

	type projTotalRow struct {
		Project   string `json:"project"`
		UV        uint64 `json:"uv"`
		Sessions  uint64 `json:"sessions"`
		Pageviews uint64 `json:"pageviews"`
		Custom    uint64 `json:"custom_events"`
	}
	totals, err := QueryJSON[projTotalRow](ctx, c, `
SELECT
    project,
    uniqCombinedMerge(12)(uv_state) AS uv,
    uniqCombinedMerge(12)(session_state) AS sessions,
    sumIf(event_count, event_type = 'pageview') AS pageviews,
    sumIf(event_count, event_type = 'custom') AS custom_events
FROM {{DB}}.events_hourly
WHERE hour >= toStartOfHour(now('UTC') - toIntervalSecond({sinceSec:UInt32}))
GROUP BY project
`, params)
	if err != nil {
		return nil, err
	}
	totalMap := make(map[string]projTotalRow, len(totals))
	for _, r := range totals {
		totalMap[r.Project] = r
	}

	type activeRow struct {
		Project string `json:"project"`
		Active  uint64 `json:"active"`
		LastTS  string `json:"last_ts"`
	}
	actives, _ := QueryJSON[activeRow](ctx, c, `
SELECT
    project,
    uniqExactIf(actor_id, actor_id != '' AND ts >= now('UTC') - INTERVAL 5 MINUTE) AS active,
    formatDateTime(max(ts), '%Y-%m-%dT%H:%i:%SZ') AS last_ts
FROM {{DB}}.events
WHERE ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
GROUP BY project
`, params)
	activeMap := make(map[string]activeRow, len(actives))
	for _, r := range actives {
		activeMap[r.Project] = r
	}

	type errRow struct {
		Project         string `json:"project"`
		Errors          uint64 `json:"errors"`
		ErroredSessions uint64 `json:"errored_sessions"`
	}
	errs, _ := QueryJSON[errRow](ctx, c, `
SELECT
    project,
    sum(toUInt64(occurrence_weight)) AS errors,
    uniqExactIf(session_id, session_id != '') AS errored_sessions
FROM {{DB}}.exceptions
WHERE ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
GROUP BY project
`, params)
	errMap := make(map[string]errRow, len(errs))
	for _, r := range errs {
		errMap[r.Project] = r
	}

	type issueRow struct {
		Project     string `json:"project"`
		Fingerprint string `json:"fingerprint"`
	}
	issueRows, _ := QueryJSON[issueRow](ctx, c, `
SELECT
    project,
    fingerprint
FROM {{DB}}.issue_rollups
GROUP BY project, fingerprint
HAVING max(last_seen) >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
`, params)
	openByProj := make(map[string]int)
	for _, ir := range issueRows {
		st := model.IssueOpen
		if pm, ok := statesByProject[ir.Project]; ok {
			if s, ok2 := pm[ir.Fingerprint]; ok2 && s.Status != "" {
				st = s.Status
			}
		}
		if st == model.IssueOpen {
			openByProj[ir.Project]++
		}
	}

	type vitalProjRow struct {
		Project string  `json:"project"`
		LCP     float64 `json:"lcp_p75"`
		INP     float64 `json:"inp_p75"`
		CLS     float64 `json:"cls_p75"`
	}
	vitalRows, _ := QueryJSON[vitalProjRow](ctx, c, `
SELECT
    project,
    if(isFinite(quantileExactIf(0.75)(value, metric_name = 'LCP')), quantileExactIf(0.75)(value, metric_name = 'LCP'), 0) AS lcp_p75,
    if(isFinite(quantileExactIf(0.75)(value, metric_name = 'INP')), quantileExactIf(0.75)(value, metric_name = 'INP'), 0) AS inp_p75,
    if(isFinite(quantileExactIf(0.75)(value, metric_name = 'CLS')), quantileExactIf(0.75)(value, metric_name = 'CLS'), 0) AS cls_p75
FROM {{DB}}.vitals
WHERE ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
GROUP BY project
`, params)
	vitalMap := make(map[string]vitalProjRow, len(vitalRows))
	for _, r := range vitalRows {
		vitalMap[r.Project] = r
	}

	// 24-hour sparkline buckets across projects
	type sparkRow struct {
		Project string `json:"project"`
		Bucket  int    `json:"bucket"`
		Count   uint64 `json:"cnt"`
	}
	sparkRows, _ := QueryJSON[sparkRow](ctx, c, `
SELECT
    project,
    toInt32(dateDiff('hour', toStartOfHour(now('UTC')) - INTERVAL 23 HOUR, hour)) AS bucket,
    sum(event_count) AS cnt
FROM {{DB}}.events_hourly
WHERE hour >= toStartOfHour(now('UTC')) - INTERVAL 23 HOUR
GROUP BY project, bucket
`, nil)
	sparkMap := make(map[string][]uint64)
	for _, r := range sparkRows {
		if r.Bucket < 0 || r.Bucket >= 24 {
			continue
		}
		arr, ok := sparkMap[r.Project]
		if !ok {
			arr = make([]uint64, 24)
			sparkMap[r.Project] = arr
		}
		arr[r.Bucket] = r.Count
	}

	var totalSessions, totalErroredSessions uint64
	for _, p := range projects {
		tot := totalMap[p.Slug]
		act := activeMap[p.Slug]
		er := errMap[p.Slug]
		vt := vitalMap[p.Slug]
		openCnt := openByProj[p.Slug]

		cf := 100.0
		if tot.Sessions > 0 {
			crashed := er.ErroredSessions
			if crashed > tot.Sessions {
				crashed = tot.Sessions
			}
			cf = float64(tot.Sessions-crashed) * 100.0 / float64(tot.Sessions)
			totalSessions += tot.Sessions
			totalErroredSessions += crashed
		}

		spark := sparkMap[p.Slug]
		if spark == nil {
			spark = make([]uint64, 24)
		}

		if tot.Pageviews > 0 || tot.Custom > 0 || er.Errors > 0 {
			out.ActiveProjects++
		}
		out.ActiveUsers5m += act.Active
		out.UniqueVisitors += tot.UV
		out.Pageviews += tot.Pageviews
		out.CustomEvents += tot.Custom
		out.Sessions += tot.Sessions
		out.ErrorCount += er.Errors
		out.OpenIssues += openCnt

		out.Projects = append(out.Projects, PortfolioProjectStats{
			Project:        p,
			ActiveUsers5m:  act.Active,
			UniqueVisitors: tot.UV,
			Pageviews:      tot.Pageviews,
			CustomEvents:   tot.Custom,
			Sessions:       tot.Sessions,
			ErrorCount:     er.Errors,
			OpenIssues:     openCnt,
			CrashFreeRate:  cf,
			LCPP75:         vt.LCP,
			INPP75:         vt.INP,
			CLSP75:         vt.CLS,
			LastEventAt:    act.LastTS,
			Sparkline:      spark,
		})
	}
	if totalSessions > 0 {
		out.OverallCrashFreeRate = float64(totalSessions-totalErroredSessions) * 100.0 / float64(totalSessions)
	}
	return out, nil
}

func (c *Client) GetPageMetrics(ctx context.Context, project string, since time.Duration, limit int) ([]PageMetricRow, error) {
	if limit <= 0 || limit > 200 {
		limit = 50
	}
	sinceSec := int(since.Seconds())
	if sinceSec <= 0 {
		sinceSec = 86400
	}
	params := map[string]string{
		"project":  project,
		"sinceSec": strconv.Itoa(sinceSec),
		"limit":    strconv.Itoa(limit),
	}

	type pvRow struct {
		PagePath string  `json:"page_path"`
		PV       uint64  `json:"pageviews"`
		Users    uint64  `json:"users"`
		AvgDur   float64 `json:"avg_dur"`
		P75Dur   float64 `json:"p75_dur"`
	}
	pvRows, err := QueryJSON[pvRow](ctx, c, `
SELECT
    if(page_path = '', '/', page_path) AS page_path,
    count() AS pageviews,
    uniqExactIf(actor_id, actor_id != '') AS users,
    if(isFinite(avgIf(duration_ms, duration_ms > 0)), avgIf(duration_ms, duration_ms > 0), 0) AS avg_dur,
    if(isFinite(quantileExactIf(0.75)(duration_ms, duration_ms > 0)), quantileExactIf(0.75)(duration_ms, duration_ms > 0), 0) AS p75_dur
FROM {{DB}}.events
WHERE project = {project:String}
  AND event_type = 'pageview'
  AND ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
GROUP BY page_path
ORDER BY pageviews DESC
LIMIT {limit:UInt32}
`, params)
	if err != nil {
		return nil, err
	}

	type vitalPageRow struct {
		PagePath string  `json:"page_path"`
		LCP      float64 `json:"lcp"`
		INP      float64 `json:"inp"`
		CLS      float64 `json:"cls"`
		FCP      float64 `json:"fcp"`
		TTFB     float64 `json:"ttfb"`
		Samples  uint64  `json:"samples"`
	}
	vRows, _ := QueryJSON[vitalPageRow](ctx, c, `
SELECT
    if(page_path = '', '/', page_path) AS page_path,
    if(isFinite(quantileExactIf(0.75)(value, metric_name = 'LCP')), quantileExactIf(0.75)(value, metric_name = 'LCP'), 0) AS lcp,
    if(isFinite(quantileExactIf(0.75)(value, metric_name = 'INP')), quantileExactIf(0.75)(value, metric_name = 'INP'), 0) AS inp,
    if(isFinite(quantileExactIf(0.75)(value, metric_name = 'CLS')), quantileExactIf(0.75)(value, metric_name = 'CLS'), 0) AS cls,
    if(isFinite(quantileExactIf(0.75)(value, metric_name = 'FCP')), quantileExactIf(0.75)(value, metric_name = 'FCP'), 0) AS fcp,
    if(isFinite(quantileExactIf(0.75)(value, metric_name = 'TTFB')), quantileExactIf(0.75)(value, metric_name = 'TTFB'), 0) AS ttfb,
    count() AS samples
FROM {{DB}}.vitals
WHERE project = {project:String}
  AND ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
GROUP BY page_path
`, params)
	vitalByPage := make(map[string]vitalPageRow, len(vRows))
	for _, r := range vRows {
		vitalByPage[r.PagePath] = r
	}

	type errPageRow struct {
		PagePath string `json:"page_path"`
		Errors   uint64 `json:"errors"`
	}
	eRows, _ := QueryJSON[errPageRow](ctx, c, `
SELECT
    if(startsWith(page_url, '/'), splitByChar('?', page_url)[1], if(path(page_url) = '', '/', path(page_url))) AS page_path,
    sum(toUInt64(occurrence_weight)) AS errors
FROM {{DB}}.exceptions
WHERE project = {project:String}
  AND ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
GROUP BY page_path
`, params)
	errByPage := make(map[string]uint64, len(eRows))
	for _, r := range eRows {
		errByPage[r.PagePath] = r.Errors
	}

	seen := make(map[string]bool, len(pvRows))
	out := make([]PageMetricRow, 0, len(pvRows))
	for _, r := range pvRows {
		seen[r.PagePath] = true
		vt := vitalByPage[r.PagePath]
		out = append(out, PageMetricRow{
			PagePath:      r.PagePath,
			Pageviews:     r.PV,
			Users:         r.Users,
			AvgDurationMS: r.AvgDur,
			P75DurationMS: r.P75Dur,
			ErrorCount:    errByPage[r.PagePath],
			LCPP75:        vt.LCP,
			INPP75:        vt.INP,
			CLSP75:        vt.CLS,
			FCPP75:        vt.FCP,
			TTFBP75:       vt.TTFB,
			VitalSamples:  vt.Samples,
		})
	}
	for _, vr := range vRows {
		if seen[vr.PagePath] {
			continue
		}
		out = append(out, PageMetricRow{
			PagePath:     vr.PagePath,
			ErrorCount:   errByPage[vr.PagePath],
			LCPP75:       vr.LCP,
			INPP75:       vr.INP,
			CLSP75:       vr.CLS,
			FCPP75:       vr.FCP,
			TTFBP75:      vr.TTFB,
			VitalSamples: vr.Samples,
		})
	}
	return out, nil
}

func (c *Client) ListEvents(ctx context.Context, project string, f EventQueryFilter) ([]EventView, error) {
	limit := f.Limit
	if limit <= 0 || limit > 300 {
		limit = 100
	}
	sinceSec := int(f.Since.Seconds())
	if sinceSec <= 0 {
		sinceSec = 86400
	}
	params := map[string]string{
		"project":  project,
		"sinceSec": strconv.Itoa(sinceSec),
		"limit":    strconv.Itoa(limit),
	}

	clauses := []string{
		"e.project = {project:String}",
		"e.ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})",
	}
	if et := strings.ToLower(strings.TrimSpace(f.EventType)); et != "" && et != "all" {
		params["eventType"] = et
		clauses = append(clauses, "e.event_type = {eventType:String}")
	}
	if en := strings.TrimSpace(f.EventName); en != "" {
		params["eventName"] = en
		clauses = append(clauses, "e.event_name = {eventName:String}")
	}
	if aid := strings.TrimSpace(f.ActorID); aid != "" {
		params["actorID"] = aid
		clauses = append(clauses, "e.actor_id = {actorID:String}")
	}
	if sid := strings.TrimSpace(f.SessionID); sid != "" {
		params["sessionID"] = sid
		clauses = append(clauses, "e.session_id = {sessionID:String}")
	}
	if cc := strings.ToUpper(strings.TrimSpace(f.Country)); cc != "" && cc != "ALL" {
		if cc == "(UNKNOWN)" || cc == "UNKNOWN" {
			clauses = append(clauses, "e.country = ''")
		} else {
			params["country"] = cc
			clauses = append(clauses, "e.country = {country:String}")
		}
	}
	if q := strings.TrimSpace(f.Search); q != "" {
		params["search"] = q
		clauses = append(clauses, "(positionCaseInsensitive(e.event_name, {search:String}) > 0 OR positionCaseInsensitive(e.page_path, {search:String}) > 0 OR positionCaseInsensitive(e.page_url, {search:String}) > 0 OR positionCaseInsensitive(e.actor_id, {search:String}) > 0 OR positionCaseInsensitive(e.session_id, {search:String}) > 0 OR positionCaseInsensitive(e.props_json, {search:String}) > 0)")
	}

	type rawEv struct {
		TS          string `json:"ts_str"`
		EventType   string `json:"event_type"`
		EventName   string `json:"event_name"`
		ActorID     string `json:"actor_id"`
		SessionID   string `json:"session_id"`
		Env         string `json:"env"`
		Release     string `json:"release"`
		PagePath    string `json:"page_path"`
		PageURL     string `json:"page_url"`
		Referrer    string `json:"referrer"`
		UTMSource   string `json:"utm_source"`
		UTMMedium   string `json:"utm_medium"`
		UTMCampaign string `json:"utm_campaign"`
		DurationMS  uint32 `json:"duration_ms"`
		Browser     string `json:"browser"`
		OS          string `json:"os"`
		DeviceType  string `json:"device_type"`
		Country     string `json:"country"`
		TraceID     string `json:"trace_id"`
		PropsJSON   string `json:"props_json"`
	}
	sqlText := fmt.Sprintf(`
SELECT
    formatDateTime(e.ts, '%%Y-%%m-%%dT%%H:%%i:%%S.%%fZ') AS ts_str,
    e.event_type,
    e.event_name,
    e.actor_id,
    e.session_id,
    e.env,
    e.release,
    e.page_path,
    e.page_url,
    e.referrer,
    e.utm_source,
    e.utm_medium,
    e.utm_campaign,
    e.duration_ms,
    e.browser,
    e.os,
    e.device_type,
    e.country,
    e.trace_id,
    e.props_json
FROM {{DB}}.events AS e
WHERE %s
ORDER BY e.ts DESC
LIMIT {limit:UInt32}
`, strings.Join(clauses, " AND "))

	rows, err := QueryJSON[rawEv](ctx, c, sqlText, params)
	if err != nil {
		return nil, err
	}
	out := make([]EventView, 0, len(rows))
	for _, r := range rows {
		ev := EventView{
			TS:          r.TS,
			EventType:   r.EventType,
			EventName:   r.EventName,
			ActorID:     r.ActorID,
			SessionID:   r.SessionID,
			Env:         r.Env,
			Release:     r.Release,
			PagePath:    r.PagePath,
			PageURL:     r.PageURL,
			Referrer:    r.Referrer,
			UTMSource:   r.UTMSource,
			UTMMedium:   r.UTMMedium,
			UTMCampaign: r.UTMCampaign,
			DurationMS:  r.DurationMS,
			Browser:     r.Browser,
			OS:          r.OS,
			DeviceType:  r.DeviceType,
			Country:     r.Country,
			TraceID:     r.TraceID,
		}
		if r.PropsJSON != "" && r.PropsJSON != "{}" {
			_ = json.Unmarshal([]byte(r.PropsJSON), &ev.Props)
		}
		out = append(out, ev)
	}
	return out, nil
}

func (c *Client) ListSessions(ctx context.Context, project string, f SessionQueryFilter) ([]SessionSummary, error) {
	limit := f.Limit
	if limit <= 0 || limit > 200 {
		limit = 80
	}
	sinceSec := int(f.Since.Seconds())
	if sinceSec <= 0 {
		sinceSec = 7 * 86400
	}
	params := map[string]string{
		"project":  project,
		"sinceSec": strconv.Itoa(sinceSec),
		"limit":    strconv.Itoa(limit),
	}

	var having []string
	if f.HasError {
		having = append(having, "error_count > 0")
	}
	if aid := strings.TrimSpace(f.ActorID); aid != "" {
		params["actorID"] = aid
		having = append(having, "actor_id = {actorID:String}")
	}
	if cc := strings.ToUpper(strings.TrimSpace(f.Country)); cc != "" && cc != "ALL" {
		params["country"] = cc
		having = append(having, "country = {country:String}")
	}
	if q := strings.TrimSpace(f.Search); q != "" {
		params["search"] = q
		having = append(having, "(positionCaseInsensitive(actor_id, {search:String}) > 0 OR positionCaseInsensitive(session_id, {search:String}) > 0 OR positionCaseInsensitive(entry_page, {search:String}) > 0 OR positionCaseInsensitive(exit_page, {search:String}) > 0 OR positionCaseInsensitive(browser, {search:String}) > 0 OR positionCaseInsensitive(os, {search:String}) > 0 OR positionCaseInsensitive(country, {search:String}) > 0)")
	}
	havingSQL := ""
	if len(having) > 0 {
		havingSQL = "HAVING " + strings.Join(having, " AND ")
	}

	sqlText := fmt.Sprintf(`
SELECT
    session_key AS session_id,
    anyLastIf(actor_id, actor_id != '') AS actor_id,
    formatDateTime(min(ts), '%%Y-%%m-%%dT%%H:%%i:%%SZ') AS start_ts,
    formatDateTime(max(ts), '%%Y-%%m-%%dT%%H:%%i:%%SZ') AS last_ts,
    toUInt64(greatest(0, dateDiff('second', min(ts), max(ts)))) AS duration_sec,
    sum(is_pv) AS pageviews,
    sum(is_custom) AS custom_events,
    sum(is_ev) AS total_events,
    sum(err_w) AS error_count,
    sum(is_log) AS log_count,
    argMinIf(page_label, ts, page_label != '') AS entry_page,
    argMaxIf(page_label, ts, page_label != '') AS exit_page,
    anyLastIf(browser, browser != '') AS browser,
    anyLastIf(os, os != '') AS os,
    anyLastIf(device_type, device_type != '') AS device_type,
    anyLastIf(country, country != '') AS country,
    anyLastIf(env, env != '') AS env,
    anyLastIf(release, release != '') AS release,
    toBool(max(is_replay) > 0) AS has_replay
FROM (
    SELECT
        if(session_id != '', session_id, concat('actor:', actor_id)) AS session_key,
        actor_id,
        ts,
        toUInt64(event_type = 'pageview') AS is_pv,
        toUInt64(event_type = 'custom') AS is_custom,
        toUInt64(1) AS is_ev,
        toUInt64(0) AS err_w,
        toUInt64(0) AS is_log,
        toUInt64(0) AS is_replay,
        if(page_path != '', page_path, if(startsWith(page_url, '/'), splitByChar('?', page_url)[1], path(page_url))) AS page_label,
        browser,
        os,
        device_type,
        country,
        env,
        release
    FROM {{DB}}.events
    WHERE project = {project:String}
      AND (session_id != '' OR actor_id != '')
      AND ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})

    UNION ALL

    SELECT
        if(session_id != '', session_id, concat('actor:', actor_id)) AS session_key,
        actor_id,
        ts,
        toUInt64(0) AS is_pv,
        toUInt64(0) AS is_custom,
        toUInt64(0) AS is_ev,
        toUInt64(occurrence_weight) AS err_w,
        toUInt64(0) AS is_log,
        toUInt64(0) AS is_replay,
        if(startsWith(page_url, '/'), splitByChar('?', page_url)[1], path(page_url)) AS page_label,
        browser,
        os,
        device_type,
        '' AS country,
        env,
        release
    FROM {{DB}}.exceptions
    WHERE project = {project:String}
      AND (session_id != '' OR actor_id != '')
      AND ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})

    UNION ALL

    SELECT
        if(session_id != '', session_id, concat('actor:', actor_id)) AS session_key,
        actor_id,
        ts,
        toUInt64(0) AS is_pv,
        toUInt64(0) AS is_custom,
        toUInt64(0) AS is_ev,
        toUInt64(0) AS err_w,
        toUInt64(1) AS is_log,
        toUInt64(0) AS is_replay,
        if(startsWith(page_url, '/'), splitByChar('?', page_url)[1], path(page_url)) AS page_label,
        '' AS browser,
        '' AS os,
        '' AS device_type,
        '' AS country,
        env,
        release
    FROM {{DB}}.logs
    WHERE project = {project:String}
      AND (session_id != '' OR actor_id != '')
      AND ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})

    UNION ALL

    SELECT
        session_id AS session_key,
        actor_id,
        ts,
        toUInt64(0) AS is_pv,
        toUInt64(0) AS is_custom,
        toUInt64(0) AS is_ev,
        toUInt64(0) AS err_w,
        toUInt64(0) AS is_log,
        toUInt64(has_full_snapshot > 0) AS is_replay,
        if(startsWith(page_url, '/'), splitByChar('?', page_url)[1], path(page_url)) AS page_label,
        '' AS browser,
        '' AS os,
        '' AS device_type,
        '' AS country,
        env,
        release
    FROM {{DB}}.replay_segments
    WHERE project = {project:String}
      AND session_id != ''
      AND ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
)
GROUP BY session_key
%s
ORDER BY max(ts) DESC
LIMIT {limit:UInt32}
`, havingSQL)

	rows, err := QueryJSON[SessionSummary](ctx, c, sqlText, params)
	if err != nil {
		return nil, err
	}
	if rows == nil {
		return []SessionSummary{}, nil
	}
	return rows, nil
}

func (c *Client) GetTimeline(ctx context.Context, project, sessionID, actorID string, since time.Duration, limit int) (*TimelineResponse, error) {
	sessionID = strings.TrimSpace(sessionID)
	actorID = strings.TrimSpace(actorID)
	if strings.HasPrefix(sessionID, "actor:") {
		if actorID == "" {
			actorID = strings.TrimPrefix(sessionID, "actor:")
		}
		sessionID = ""
	}
	if sessionID == "" && actorID == "" {
		return nil, fmt.Errorf("session_id or actor_id is required")
	}
	if limit <= 0 || limit > 600 {
		limit = 300
	}
	sinceSec := int(since.Seconds())
	if sinceSec <= 0 {
		sinceSec = 7 * 86400
	}

	params := map[string]string{
		"project":  project,
		"sinceSec": strconv.Itoa(sinceSec),
		"limit":    strconv.Itoa(limit),
	}

	var subjectCond string
	if sessionID != "" {
		params["sessionID"] = sessionID
		subjectCond = "session_id = {sessionID:String}"
	} else {
		params["actorID"] = actorID
		subjectCond = "actor_id = {actorID:String}"
	}

	res := &TimelineResponse{
		Project:      project,
		ActorID:      actorID,
		SessionID:    sessionID,
		Items:        []TimelineItem{},
		UserSessions: []SessionSummary{},
	}

	// 1. Events
	type evRow struct {
		TS        string `json:"ts_str"`
		TSMS      int64  `json:"ts_ms"`
		EventType string `json:"event_type"`
		EventName string `json:"event_name"`
		ActorID   string `json:"actor_id"`
		SessionID string `json:"session_id"`
		Env       string `json:"env"`
		Release   string `json:"release"`
		PagePath  string `json:"page_path"`
		PageURL   string `json:"page_url"`
		Referrer  string `json:"referrer"`
		Duration  uint32 `json:"duration_ms"`
		Browser   string `json:"browser"`
		OS        string `json:"os"`
		Device    string `json:"device_type"`
		Country   string `json:"country"`
		TraceID   string `json:"trace_id"`
		PropsJSON string `json:"props_json"`
	}
	evs, err := QueryJSON[evRow](ctx, c, fmt.Sprintf(`
SELECT
    formatDateTime(ts, '%%Y-%%m-%%dT%%H:%%i:%%S.%%fZ') AS ts_str,
    toUnixTimestamp64Milli(ts) AS ts_ms,
    event_type,
    event_name,
    actor_id,
    session_id,
    env,
    release,
    page_path,
    page_url,
    referrer,
    duration_ms,
    browser,
    os,
    device_type,
    country,
    trace_id,
    props_json
FROM {{DB}}.events
WHERE project = {project:String}
  AND %s
  AND ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
ORDER BY ts ASC
LIMIT {limit:UInt32}
`, subjectCond), params)
	if err != nil {
		return nil, err
	}

	for i, r := range evs {
		if res.ActorID == "" && r.ActorID != "" {
			res.ActorID = r.ActorID
		}
		if res.SessionID == "" && r.SessionID != "" {
			res.SessionID = r.SessionID
		}
		if res.Browser == "" && r.Browser != "" {
			res.Browser = r.Browser
		}
		if res.OS == "" && r.OS != "" {
			res.OS = r.OS
		}
		if res.DeviceType == "" && r.Device != "" {
			res.DeviceType = r.Device
		}
		if res.Country == "" && r.Country != "" {
			res.Country = r.Country
		}
		if res.Env == "" && r.Env != "" {
			res.Env = r.Env
		}
		if res.Release == "" && r.Release != "" {
			res.Release = r.Release
		}

		kind := r.EventType
		if kind == "" {
			kind = "custom"
		}
		title := r.EventName
		subtitle := r.PagePath
		if kind == "pageview" {
			title = defaultNonEmpty(r.PagePath, "/")
			if r.Referrer != "" {
				subtitle = "来源: " + r.Referrer
			} else {
				subtitle = r.PageURL
			}
		}

		var props map[string]any
		if r.PropsJSON != "" && r.PropsJSON != "{}" {
			_ = json.Unmarshal([]byte(r.PropsJSON), &props)
		}
		res.Items = append(res.Items, TimelineItem{
			ID:         fmt.Sprintf("ev-%d-%d", r.TSMS, i),
			Kind:       kind,
			TS:         r.TS,
			TSMillis:   r.TSMS,
			SessionID:  r.SessionID,
			ActorID:    r.ActorID,
			Title:      title,
			Subtitle:   subtitle,
			PagePath:   r.PagePath,
			PageURL:    r.PageURL,
			DurationMS: r.Duration,
			TraceID:    r.TraceID,
			Props:      props,
		})
	}

	// 2. Exceptions (with full Ring Buffer breadcrumbs + stack frames!)
	type excRow struct {
		TS              string `json:"ts_str"`
		TSMS            int64  `json:"ts_ms"`
		ActorID         string `json:"actor_id"`
		SessionID       string `json:"session_id"`
		Env             string `json:"env"`
		Release         string `json:"release"`
		Fingerprint     string `json:"fingerprint"`
		Level           string `json:"level"`
		ErrorType       string `json:"error_type"`
		ErrorMessage    string `json:"error_message"`
		Culprit         string `json:"culprit"`
		PageURL         string `json:"page_url"`
		TraceID         string `json:"trace_id"`
		Browser         string `json:"browser"`
		OS              string `json:"os"`
		Device          string `json:"device_type"`
		FramesJSON      string `json:"frames_json"`
		BreadcrumbsJSON string `json:"breadcrumbs_json"`
		TagsJSON        string `json:"tags_json"`
	}
	excs, _ := QueryJSON[excRow](ctx, c, fmt.Sprintf(`
SELECT
    formatDateTime(ts, '%%Y-%%m-%%dT%%H:%%i:%%S.%%fZ') AS ts_str,
    toUnixTimestamp64Milli(ts) AS ts_ms,
    actor_id,
    session_id,
    env,
    release,
    fingerprint,
    level,
    error_type,
    error_message,
    culprit,
    page_url,
    trace_id,
    browser,
    os,
    device_type,
    frames_json,
    breadcrumbs_json,
    tags_json
FROM {{DB}}.exceptions
WHERE project = {project:String}
  AND %s
  AND ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
ORDER BY ts ASC
LIMIT 100
`, subjectCond), params)

	for i, r := range excs {
		if res.ActorID == "" && r.ActorID != "" {
			res.ActorID = r.ActorID
		}
		if res.SessionID == "" && r.SessionID != "" {
			res.SessionID = r.SessionID
		}
		if res.Browser == "" && r.Browser != "" {
			res.Browser = r.Browser
		}
		if res.OS == "" && r.OS != "" {
			res.OS = r.OS
		}
		if res.DeviceType == "" && r.Device != "" {
			res.DeviceType = r.Device
		}

		var frames []model.StackFrame
		var bcs []model.Breadcrumb
		var tags map[string]string
		_ = json.Unmarshal([]byte(r.FramesJSON), &frames)
		_ = json.Unmarshal([]byte(r.BreadcrumbsJSON), &bcs)
		_ = json.Unmarshal([]byte(r.TagsJSON), &tags)

		res.ErrorCount++
		res.Items = append(res.Items, TimelineItem{
			ID:           fmt.Sprintf("exc-%d-%d", r.TSMS, i),
			Kind:         "exception",
			TS:           r.TS,
			TSMillis:     r.TSMS,
			SessionID:    r.SessionID,
			ActorID:      r.ActorID,
			Title:        fmt.Sprintf("%s: %s", r.ErrorType, r.ErrorMessage),
			Subtitle:     r.Culprit,
			Level:        r.Level,
			PageURL:      r.PageURL,
			TraceID:      r.TraceID,
			Fingerprint:  r.Fingerprint,
			ErrorType:    r.ErrorType,
			ErrorMessage: r.ErrorMessage,
			Culprit:      r.Culprit,
			Tags:         tags,
			Frames:       frames,
			Breadcrumbs:  bcs,
		})
	}

	// 3. Logs
	type logRow struct {
		TS        string `json:"ts_str"`
		TSMS      int64  `json:"ts_ms"`
		ActorID   string `json:"actor_id"`
		SessionID string `json:"session_id"`
		Level     string `json:"level"`
		Message   string `json:"message"`
		PageURL   string `json:"page_url"`
		TraceID   string `json:"trace_id"`
		AttrsJSON string `json:"attrs_json"`
	}
	lgs, _ := QueryJSON[logRow](ctx, c, fmt.Sprintf(`
SELECT
    formatDateTime(ts, '%%Y-%%m-%%dT%%H:%%i:%%S.%%fZ') AS ts_str,
    toUnixTimestamp64Milli(ts) AS ts_ms,
    actor_id,
    session_id,
    level,
    message,
    page_url,
    trace_id,
    attrs_json
FROM {{DB}}.logs
WHERE project = {project:String}
  AND %s
  AND ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
ORDER BY ts ASC
LIMIT 150
`, subjectCond), params)

	for i, r := range lgs {
		var attrs map[string]any
		if r.AttrsJSON != "" && r.AttrsJSON != "{}" {
			_ = json.Unmarshal([]byte(r.AttrsJSON), &attrs)
		}
		res.Items = append(res.Items, TimelineItem{
			ID:        fmt.Sprintf("log-%d-%d", r.TSMS, i),
			Kind:      "log",
			TS:        r.TS,
			TSMillis:  r.TSMS,
			SessionID: r.SessionID,
			ActorID:   r.ActorID,
			Title:     r.Message,
			Level:     r.Level,
			PageURL:   r.PageURL,
			TraceID:   r.TraceID,
			Props:     attrs,
		})
	}

	// 4. Web Vitals
	type vtRow struct {
		TS        string  `json:"ts_str"`
		TSMS      int64   `json:"ts_ms"`
		ActorID   string  `json:"actor_id"`
		SessionID string  `json:"session_id"`
		Metric    string  `json:"metric_name"`
		Value     float64 `json:"value"`
		Rating    string  `json:"rating"`
		PagePath  string  `json:"page_path"`
	}
	vts, _ := QueryJSON[vtRow](ctx, c, fmt.Sprintf(`
SELECT
    formatDateTime(ts, '%%Y-%%m-%%dT%%H:%%i:%%S.%%fZ') AS ts_str,
    toUnixTimestamp64Milli(ts) AS ts_ms,
    actor_id,
    session_id,
    metric_name,
    value,
    rating,
    page_path
FROM {{DB}}.vitals
WHERE project = {project:String}
  AND %s
  AND ts >= now('UTC') - toIntervalSecond({sinceSec:UInt32})
ORDER BY ts ASC
LIMIT 150
`, subjectCond), params)

	for i, r := range vts {
		res.Items = append(res.Items, TimelineItem{
			ID:           fmt.Sprintf("vt-%d-%d", r.TSMS, i),
			Kind:         "vital",
			TS:           r.TS,
			TSMillis:     r.TSMS,
			SessionID:    r.SessionID,
			ActorID:      r.ActorID,
			Title:        r.Metric,
			Subtitle:     r.PagePath,
			PagePath:     r.PagePath,
			MetricName:   r.Metric,
			MetricValue:  r.Value,
			MetricRating: r.Rating,
		})
	}

	// Sort unified timeline chronologically
	sort.SliceStable(res.Items, func(i, j int) bool {
		return res.Items[i].TSMillis < res.Items[j].TSMillis
	})
	if len(res.Items) > limit {
		res.Items = res.Items[:limit]
	}
	if len(res.Items) > 0 {
		baseMS := res.Items[0].TSMillis
		lastMS := res.Items[len(res.Items)-1].TSMillis
		res.StartTS = res.Items[0].TS
		res.EndTS = res.Items[len(res.Items)-1].TS
		if lastMS >= baseMS {
			res.DurationMS = lastMS - baseMS
		}
		for i := range res.Items {
			res.Items[i].OffsetMS = res.Items[i].TSMillis - baseMS
		}
	}
	res.TotalItems = len(res.Items)

	// Fetch sibling sessions of the same actor_id so user can hop across historical visits
	if res.ActorID != "" {
		siblings, err := c.ListSessions(ctx, project, SessionQueryFilter{
			Since:   7 * 24 * time.Hour,
			ActorID: res.ActorID,
			Limit:   12,
		})
		if err == nil && siblings != nil {
			res.UserSessions = siblings
			for _, s := range siblings {
				if s.SessionID == res.SessionID && s.HasReplay {
					res.HasReplay = true
				}
			}
		}
	}

	// Also check replay_segments directly when session_id was requested (handles live active sessions before fetchLater flushes)
	if res.SessionID != "" && !res.HasReplay {
		type rpMeta struct {
			Cnt     uint64 `json:"cnt"`
			ActorID string `json:"actor_id"`
			MinMS   int64  `json:"min_ms"`
			MaxMS   int64  `json:"max_ms"`
		}
		metaRows, err := QueryJSON[rpMeta](ctx, c, `
SELECT
    countIf(has_full_snapshot > 0) AS cnt,
    anyLastIf(actor_id, actor_id != '') AS actor_id,
    min(start_ms) AS min_ms,
    max(end_ms) AS max_ms
FROM {{DB}}.replay_segments
WHERE project = {project:String}
  AND session_id = {sessionID:String}
`, map[string]string{"project": project, "sessionID": res.SessionID})
		if err == nil && len(metaRows) > 0 && metaRows[0].Cnt > 0 {
			res.HasReplay = true
			if res.ActorID == "" && metaRows[0].ActorID != "" {
				res.ActorID = metaRows[0].ActorID
			}
			if len(res.Items) == 0 && metaRows[0].MinMS > 0 {
				t0 := time.UnixMilli(metaRows[0].MinMS).UTC()
				t1 := time.UnixMilli(metaRows[0].MaxMS).UTC()
				res.StartTS = t0.Format(time.RFC3339Nano)
				res.EndTS = t1.Format(time.RFC3339Nano)
				if metaRows[0].MaxMS > metaRows[0].MinMS {
					res.DurationMS = metaRows[0].MaxMS - metaRows[0].MinMS
				}
			}
		}
	}

	return res, nil
}

func defaultNonEmpty(v, fallback string) string {
	if strings.TrimSpace(v) == "" {
		return fallback
	}
	return v
}
