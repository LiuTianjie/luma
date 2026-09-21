package registryhttp

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/LiuTianjie/luma/internal/registry"
)

func TestManifestResolutionBasicAndBearerCredentials(t *testing.T) {
	for _, bearer := range []bool{false, true} {
		t.Run(fmt.Sprint(bearer), func(t *testing.T) {
			var manifests, tokens atomic.Int32
			digest := "sha256:" + strings.Repeat("a", 64)
			var endpoint string
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/token" {
					tokens.Add(1)
					u, p, ok := r.BasicAuth()
					if !ok || u != "user" || p != "fixture-private" || r.URL.Query().Get("scope") != "repository:team/app:pull" || r.URL.Query().Get("service") != "fixture" {
						t.Error("wrong token credentials or pull scope")
						w.WriteHeader(403)
						return
					}
					fmt.Fprint(w, `{"token":"fixture-access-token"}`)
					return
				}
				authorized := r.Header.Get("Authorization") == "Bearer fixture-access-token"
				if !bearer {
					u, p, ok := r.BasicAuth()
					authorized = ok && u == "user" && p == "fixture-private"
				}
				if !authorized {
					challenge := `Basic realm="fixture"`
					if bearer {
						challenge = `Bearer realm="` + endpoint + `/token",service="fixture",scope="repository:team/app:pull"`
					}
					w.Header().Set("WWW-Authenticate", challenge)
					w.WriteHeader(401)
					return
				}
				if r.URL.Path == "/v2/" {
					return
				}
				if r.URL.Path != "/v2/team/app/manifests/latest" || r.Method != "HEAD" {
					t.Error("unexpected manifest operation", r.Method, r.URL.Path)
					w.WriteHeader(404)
					return
				}
				manifests.Add(1)
				w.Header().Set("Content-Type", "application/vnd.oci.image.index.v1+json")
				w.Header().Set("Content-Length", "123")
				w.Header().Set("Docker-Content-Digest", digest)
			}))
			defer server.Close()
			endpoint = server.URL
			host := strings.TrimPrefix(endpoint, "http://")
			got, err := (Resolver{}).Resolve(context.Background(), host+"/team/app:latest", registry.RuntimeAuth{ServerAddress: host, Username: "user", Password: "fixture-private"})
			if err != nil || got != host+"/team/app@"+digest || manifests.Load() != 1 {
				t.Fatal(got, err, manifests.Load())
			}
			if bearer && tokens.Load() != 1 {
				t.Fatal("token exchange not used")
			}
		})
	}
}

func TestRegistryFailuresDoNotExposeCredentialsOrTrustWrongDigest(t *testing.T) {
	for _, invalidDigest := range []bool{false, true} {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if !invalidDigest {
				w.WriteHeader(403)
				fmt.Fprint(w, "reflected fixture-private")
				return
			}
			if r.URL.Path == "/v2/" {
				return
			}
			w.Header().Set("Content-Type", "application/vnd.oci.image.manifest.v1+json")
			w.Header().Set("Content-Length", "1")
			w.Header().Set("Docker-Content-Digest", "sha256:wrong")
		}))
		host := strings.TrimPrefix(server.URL, "http://")
		_, err := (Resolver{}).Resolve(context.Background(), host+"/app", registry.RuntimeAuth{Username: "user", Password: "fixture-private"})
		if err == nil || strings.Contains(err.Error(), "fixture-private") {
			t.Fatal(err)
		}
		_, err = (Resolver{}).Resolve(context.Background(), host+"/app", registry.RuntimeAuth{ServerAddress: "unrelated.test", Username: "user", Password: "fixture-private"})
		if err == nil {
			t.Fatal("accepted unrelated registry credential")
		}
		server.Close()
	}
}
