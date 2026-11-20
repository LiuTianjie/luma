package clickhouse

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"sync/atomic"
	"testing"
	"time"

	"github.com/liutianjie/luma-eye/internal/model"
)

func TestListIssuesPaginatesWhenFilteringByStatus(t *testing.T) {
	// Total 250 issues in ClickHouse: first 220 are resolved, last 30 are open.
	type fakeRow struct {
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

	allRows := make([]fakeRow, 250)
	states := make(map[string]model.IssueState, 250)
	for i := 0; i < 250; i++ {
		fp := fmt.Sprintf("fp_%03d", i)
		allRows[i] = fakeRow{
			Fingerprint:  fp,
			FirstSeen:    "2026-11-18T00:00:00Z",
			LastSeen:     "2026-11-18T12:00:00Z",
			Count:        uint64(250 - i),
			Users:        1,
			ErrorType:    "Error",
			ErrorMessage: "msg " + fp,
			Level:        "error",
		}
		if i < 220 {
			states[fp] = model.IssueState{Project: "demo", Fingerprint: fp, Status: model.IssueResolved}
		} else {
			states[fp] = model.IssueState{Project: "demo", Fingerprint: fp, Status: model.IssueOpen}
		}
	}

	var queries atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		queries.Add(1)
		lim, _ := strconv.Atoi(r.URL.Query().Get("param_limit"))
		off, _ := strconv.Atoi(r.URL.Query().Get("param_offset"))
		var page []fakeRow
		if off < len(allRows) {
			end := off + lim
			if end > len(allRows) {
				end = len(allRows)
			}
			page = allRows[off:end]
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"data": page,
			"rows": len(page),
		})
	}))
	defer srv.Close()

	cli, err := NewClient(srv.URL, "luma_eye", "", "")
	if err != nil {
		t.Fatal(err)
	}

	// Ask for 10 open issues: must paginate past the first 220 resolved issues and return 10 open issues!
	got, err := cli.ListIssues(context.Background(), "demo", 7*24*time.Hour, 10, states, string(model.IssueOpen))
	if err != nil {
		t.Fatalf("ListIssues failed: %v", err)
	}
	if len(got) != 10 {
		t.Fatalf("expected 10 open issues, got %d (queries=%d)", len(got), queries.Load())
	}
	for i, item := range got {
		wantFP := fmt.Sprintf("fp_%03d", 220+i)
		if item.Fingerprint != wantFP || item.Status != model.IssueOpen {
			t.Fatalf("item[%d] = %s (%s), want %s (open)", i, item.Fingerprint, item.Status, wantFP)
		}
	}
	if queries.Load() != 2 {
		t.Fatalf("expected 2 paginated queries (offset 0 and offset 200), got %d", queries.Load())
	}

	// Unfiltered query ("all") should issue only 1 query with exact requested limit
	queries.Store(0)
	allGot, err := cli.ListIssues(context.Background(), "demo", 7*24*time.Hour, 10, states, "all")
	if err != nil {
		t.Fatalf("ListIssues(all) failed: %v", err)
	}
	if len(allGot) != 10 || queries.Load() != 1 {
		t.Fatalf("expected 10 rows in 1 query for unfiltered ListIssues, got len=%d queries=%d", len(allGot), queries.Load())
	}
}
