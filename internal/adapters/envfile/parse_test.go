package envfile

import (
	"reflect"
	"strings"
	"testing"
)

func TestParseAndScopeWithoutExpansion(t *testing.T) {
	input := `# connection and deployment values
export TOKEN='secret with spaces'
EMPTY=
VALUE=a=b # trailing comment
RAW=$(touch /never-run)
QUOTED="a\"b\\c\n$TOKEN" # comment
UNCHANGED=${TOKEN}
`
	values, err := Parse([]byte(input))
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]string{"TOKEN": "secret with spaces", "EMPTY": "", "VALUE": "a=b", "RAW": "$(touch /never-run)", "QUOTED": "a\"b\\c\\n$TOKEN", "UNCHANGED": "${TOKEN}"}
	if !reflect.DeepEqual(values, want) {
		t.Fatalf("values=%#v", values)
	}
	selected := Referenced(values, "${TOKEN} ${ABSENT}", "${EMPTY} ${TOKEN}")
	if !reflect.DeepEqual(selected, map[string]string{"TOKEN": want["TOKEN"], "EMPTY": ""}) {
		t.Fatal(selected)
	}
}
func TestInvalidValuesDoNotLeakContents(t *testing.T) {
	for _, input := range []string{"MISSING", "1BAD=value", "TOKEN='private-secret", `TOKEN="okay" 'private-secret`, "TOKEN='okay'\\"} {
		_, err := Parse([]byte(input))
		if err == nil {
			t.Fatalf("accepted malformed input")
		}
		if strings.Contains(err.Error(), "private-secret") {
			t.Fatal("leaked value")
		}
	}
}
