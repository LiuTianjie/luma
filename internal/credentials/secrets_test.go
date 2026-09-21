package credentials

import (
	"errors"
	"reflect"
	"testing"
)

func TestScopedResolutionNeverFallsBackToGlobal(t *testing.T) {
	snapshot := SecretSnapshot{Global: map[string]string{"TOKEN": "global", "SECOND": "other"}, Scoped: map[string]string{"TOKEN": "scoped"}, HasScoped: true}
	names := []string{"SECOND", "TOKEN"}
	if _, err := ResolveSecrets("app", names, names, nil, snapshot); !errors.Is(err, ErrSecretInput) {
		t.Fatal(err)
	}
	got, err := ResolveSecrets("app", names, names, map[string]string{"SECOND": "incoming", "UNUSED": "discard"}, snapshot)
	if err != nil || !got.Info.Scoped || got.Info.Imported != 1 || got.Values["TOKEN"] != "scoped" || got.Values["SECOND"] != "incoming" {
		t.Fatal(got.Info, err)
	}
	if !reflect.DeepEqual(got.Imports, map[string]string{"SECOND": "incoming"}) {
		t.Fatal("wrong imported names")
	}
	if snapshot.Scoped["SECOND"] != "" {
		t.Fatal("resolution mutated snapshot")
	}
	if _, err := ResolveSecrets("fresh", []string{"TOKEN"}, []string{"TOKEN"}, map[string]string{}, SecretSnapshot{Global: snapshot.Global}); !errors.Is(err, ErrSecretInput) {
		t.Fatal("empty env import fell back", err)
	}
}
func TestGlobalSecretSubstitutionIsLiteralAndSinglePass(t *testing.T) {
	names := SecretReferences("env: ${TOKEN}", "command: ${SHELL_VAR}")
	r, err := ResolveSecrets("app", names, []string{"TOKEN"}, nil, SecretSnapshot{Global: map[string]string{"TOKEN": "quote\"\\\n${SECOND}"}})
	if err != nil {
		t.Fatal(err)
	}
	got, err := SubstituteSecrets("prefix:${TOKEN}", r.Values)
	if err != nil || got != "prefix:quote\"\\\n${SECOND}" {
		t.Fatal("nonliteral substitution", err)
	}
}
