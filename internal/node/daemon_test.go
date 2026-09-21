package node

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestDockerRegistryMergePreservesUnknownSettings(t *testing.T) {
	old := []byte(`{"data-root":"/data/docker","features":{"containerd-snapshotter":true},"insecure-registries":["old.test:5000"]}`)
	updated, changed, err := AddInsecureRegistry(old, "new.test:5000")
	if err != nil || !changed {
		t.Fatal(err)
	}
	var got map[string]any
	if json.Unmarshal(updated, &got) != nil || got["data-root"] != "/data/docker" {
		t.Fatal(string(updated))
	}
	if _, ok := got["features"]; !ok {
		t.Fatal("unknown setting was lost")
	}
	again, changed, err := AddInsecureRegistry(updated, "new.test:5000")
	if err != nil || changed || string(again) != string(updated) {
		t.Fatal("unchanged daemon file was rewritten")
	}
	for _, invalid := range []string{`null`, `[]`, `{"data-root":`, `{"insecure-registries":{}}`, `{"debug":true,"debug":false}`, `{} {}`} {
		if _, _, err := AddInsecureRegistry([]byte(invalid), "new.test"); err == nil {
			t.Fatalf("clobbered invalid config %s", invalid)
		}
	}
}
func TestSystemdEnvironmentEscapesDirectivesAndSpecifiers(t *testing.T) {
	line, err := SystemdEnvironment("HTTP_PROXY", `http://user:p%25"word@proxy.test:7890`)
	if err != nil || !strings.Contains(line, `p%%25\"word`) {
		t.Fatalf("%s %v", line, err)
	}
	if _, err := SystemdEnvironment("HTTP_PROXY", "http://proxy\nExecStart=bad"); err == nil {
		t.Fatal("accepted a second unit directive")
	}
}
