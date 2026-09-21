package history

import (
	"encoding/base64"
	"errors"
	"strings"
	"testing"
)

func TestCursorRejectsTypeConfusionAndOverflow(t *testing.T) {
	base, err := ParseList(map[string]string{}, "", false)
	if err != nil {
		t.Fatal(err)
	}
	for _, suffix := range []string{
		`"watermark":true,"key":[1,"id","buildRuns"]`,
		`"watermark":1,"key":[9223372036854775808,"id","buildRuns"]`,
		`"watermark":1,"key":[1.0,"id","buildRuns"]`,
		`"watermark":-1,"key":[1,"id","buildRuns"]`,
		`"watermark":1,"key":[1,{},"buildRuns"]`,
		`"watermark":1,"key":[1,"id","nodes"]`,
	} {
		raw := `{"v":1,"scope":"` + base.Scope + `",` + suffix + `}`
		cursor := base64.RawURLEncoding.EncodeToString([]byte(raw))
		if _, err := ParseList(map[string]string{"cursor": cursor}, "", false); !errors.Is(err, ErrInvalid) {
			t.Fatalf("accepted %s: %v", raw, err)
		}
	}
	for _, raw := range []string{`null`, `[]`, `{"scope":{}}`, strings.Repeat("x", 4097)} {
		if _, err := ParseList(map[string]string{"cursor": base64.RawURLEncoding.EncodeToString([]byte(raw))}, "", false); !errors.Is(err, ErrInvalid) {
			t.Fatalf("accepted %q", raw)
		}
	}
}

func TestDetailCursorCannotCrossRecordOrResumeMode(t *testing.T) {
	q, err := ParseDetail("build", "构建", nil)
	if err != nil {
		t.Fatal(err)
	}
	cursor := encodeCursor(map[string]any{"v": 1, "scope": q.Scope, "after": 1, "through": 2})
	for _, test := range []struct {
		kind, id string
		values   map[string]string
	}{
		{"deployment", "构建", map[string]string{"cursor": cursor}},
		{"build", "other", map[string]string{"cursor": cursor}},
		{"build", "构建", map[string]string{"cursor": cursor, "after": "1"}},
	} {
		if _, err := ParseDetail(test.kind, test.id, test.values); !errors.Is(err, ErrInvalid) {
			t.Fatalf("accepted %+v", test)
		}
	}
	got, err := ParseDetail("build", "构建", map[string]string{"cursor": cursor})
	if err != nil || got.After != 1 || *got.Through != 2 {
		t.Fatalf("%+v %v", got, err)
	}
}

func TestListQueryLimitsAndUTC(t *testing.T) {
	for _, values := range []map[string]string{
		{"limit": "+1"}, {"limit": "01"}, {"limit": "1000"}, {"since": "-1"}, {"since": "+1"},
		{"since": "253402300800"}, {"since": "2026-01-01T00:00:00"},
		{"app": strings.Repeat("学", 256)},
	} {
		if _, err := ParseList(values, "", false); !errors.Is(err, ErrInvalid) {
			t.Fatalf("accepted %+v", values)
		}
	}
	q, err := ParseList(map[string]string{"since": "2023-11-15T06:13:20+08:00", "until": "1700000000", "app": strings.Repeat("学", 255)}, "", false)
	if err != nil || *q.Filters.Since != 1700000000 || q.Limit != 50 {
		t.Fatalf("%+v %v", q, err)
	}
}
