package cloudflare

import (
	"context"
	"encoding/json"
	"github.com/LiuTianjie/luma/internal/deployment"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
)

type fixtureSecrets map[string]string

func (f fixtureSecrets) GlobalSecrets(_ context.Context, names []string) (map[string]string, error) {
	m := map[string]string{}
	for _, n := range names {
		if v, ok := f[n]; ok {
			m[n] = v
		}
	}
	return m, nil
}
func TestSyncCreatesAndUpdatesWithStoredCredential(t *testing.T) {
	methods := []string{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer stored" {
			t.Error("did not use current stored credential")
		}
		methods = append(methods, r.Method)
		w.Header().Set("Content-Type", "application/json")
		if r.Method == "GET" {
			if r.URL.Query().Get("name") == "new.example.test" {
				w.Write([]byte(`{"success":true,"result":[]}`))
			} else {
				w.Write([]byte(`{"success":true,"result":[{"id":"existing"}]}`))
			}
			return
		}
		var record deployment.DNSRecord
		if json.NewDecoder(r.Body).Decode(&record) != nil || record.Content != "192.0.2.1" || record.TTL != 1 {
			t.Error("incorrect DNS body")
		}
		if r.Method == "PUT" && r.URL.Path != "/zones/zone/dns_records/existing" {
			t.Error(r.URL.Path)
		}
		w.Write([]byte(`{"success":true,"result":{"id":"saved"}}`))
	}))
	defer server.Close()
	c, err := New(server.URL, fixtureSecrets{"TOKEN": "stored", "ZONE": "zone"}, func(string) string { return "old-env" })
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	config := deployment.DNSConfig{TokenEnv: "TOKEN", ZoneIDEnv: "ZONE"}
	records := []deployment.DNSRecord{{Name: "new.example.test", Type: "A", Content: "192.0.2.1", TTL: 1}, {Name: "old.example.test", Type: "A", Content: "192.0.2.1", TTL: 1}}
	messages, err := c.Sync(context.Background(), config, records)
	if err != nil || len(messages) != 2 || !reflect.DeepEqual(methods, []string{"GET", "POST", "GET", "PUT"}) {
		t.Fatal(messages, err, methods)
	}
}
