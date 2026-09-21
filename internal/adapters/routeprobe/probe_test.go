package routeprobe

import (
	"context"
	"github.com/LiuTianjie/luma/internal/deployment"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestPublicRouteDistinguishesApplication404AndTraefikMiss(t *testing.T) {
	for _, traefik := range []bool{false, true} {
		t.Run(map[bool]string{false: "application", true: "missing-router"}[traefik], func(t *testing.T) {
			server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if traefik {
					w.Header().Set("Server", "Traefik")
				}
				w.WriteHeader(404)
				if r.Method == "GET" {
					w.Write([]byte("404 page not found\n"))
				}
			}))
			defer server.Close()
			p := New()
			defer p.Close()
			p.http = server.Client()
			p.Interval = 100 * time.Millisecond
			p.Settle = 2 * time.Second
			health, err := p.Probe(context.Background(), deployment.Service{Domain: strings.TrimPrefix(server.URL, "https://")})
			if traefik {
				if err == nil || health.Status != "unhealthy" {
					t.Fatal(health, err)
				}
			} else if err != nil || health.Status != "ready" {
				t.Fatal(health, err)
			}
		})
	}
}

func TestTCPProbeUsesPublishedPort(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	p := New()
	defer p.Close()
	s := deployment.Service{Exposure: "tcp-relay", Domain: "127.0.0.1", Port: 1234, PublishPort: port}
	health, err := p.Probe(context.Background(), s)
	if err != nil || health.Status != "ready" || health.Kind != "public-tcp" {
		t.Fatal(health, err)
	}
	listener.Close()
	health, err = p.Probe(context.Background(), s)
	if err != nil || health.Status != "inconclusive" {
		t.Fatal(health, err)
	}
}
