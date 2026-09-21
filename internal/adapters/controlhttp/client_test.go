package controlhttp_test

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"sync/atomic"
	"testing"
	"time"

	"github.com/LiuTianjie/luma/internal/adapters/controlhttp"
	"github.com/LiuTianjie/luma/internal/deployment"
)

func client(t *testing.T, server *httptest.Server) *controlhttp.Client {
	t.Helper()
	c, err := controlhttp.New(controlhttp.Options{Endpoint: server.URL, Token: "private-test-token", Insecure: true})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(c.Close)
	return c
}
func TestStreamDeliveredBeforeCompletion(t *testing.T) {
	first := make(chan struct{})
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" || r.URL.Path != "/v1/deployments/stream" || r.Header.Get("Authorization") != "Bearer private-test-token" {
			t.Error("invalid request")
		}
		w.Header().Set("Content-Type", "application/x-ndjson")
		fmt.Fprintln(w, `{"status":"start"}`)
		w.(http.Flusher).Flush()
		select {
		case <-first:
		case <-r.Context().Done():
			return
		}
		fmt.Fprintln(w, `{"status":"done","result":{"service":"app"}}`)
	}))
	defer server.Close()
	c := client(t, server)
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	events := 0
	err := c.Submit(ctx, deployment.SubmitRequest{Manifest: "name: app"}, func(e deployment.Event) error {
		events++
		if e.Status == "start" {
			close(first)
		}
		return nil
	})
	if err != nil || events != 2 {
		t.Fatalf("events=%d err=%v", events, err)
	}
}
func TestMutationsNotRetriedAndRedirectNotFollowed(t *testing.T) {
	var targetCalls atomic.Int32
	target := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { targetCalls.Add(1) }))
	defer target.Close()
	for _, status := range []int{307, 429, 503} {
		t.Run(fmt.Sprint(status), func(t *testing.T) {
			var calls atomic.Int32
			server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				w.Header().Set("Location", target.URL)
				w.WriteHeader(status)
			}))
			defer server.Close()
			err := client(t, server).Submit(context.Background(), deployment.SubmitRequest{}, func(deployment.Event) error { return nil })
			var apiErr *controlhttp.APIError
			if !errors.As(err, &apiErr) || apiErr.Status != status || calls.Load() != 1 || targetCalls.Load() != 0 {
				t.Fatalf("err=%v calls=%d leaked=%d", err, calls.Load(), targetCalls.Load())
			}
		})
	}
}
func TestCancellationStopsReading(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/x-ndjson")
		w.WriteHeader(200)
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	}))
	defer server.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	err := client(t, server).Submit(ctx, deployment.SubmitRequest{}, func(deployment.Event) error { return nil })
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("err=%v", err)
	}
}
func TestMalformedStreamsRejected(t *testing.T) {
	for _, body := range []string{"not json\n", "[]\n", `{"status":"done","result":null}` + "\n", `{"status":"done","result":[]}` + "\n"} {
		server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
		c := client(t, server)
		if err := c.Submit(context.Background(), deployment.SubmitRequest{}, func(deployment.Event) error { return nil }); err == nil {
			t.Errorf("accepted %s", body)
		}
		server.Close()
	}
}

func TestCancellationAtStreamEventCannotBecomeCleanEOF(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/x-ndjson")
		fmt.Fprintln(w, `{"status":"start"}`)
		w.(http.Flusher).Flush()
		<-ctx.Done()
	}))
	defer server.Close()
	err := client(t, server).Submit(ctx, deployment.SubmitRequest{}, func(deployment.Event) error { cancel(); return nil })
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled stream returned %v", err)
	}
}
func TestHTTPSAndResolveIPPreserveHost(t *testing.T) {
	for _, endpoint := range []string{"http://example.com", "https://user:pass@example.com", "https://example.com?token=x"} {
		if _, err := controlhttp.New(controlhttp.Options{Endpoint: endpoint, Token: "token"}); err == nil {
			t.Errorf("accepted %s", endpoint)
		}
	}
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Host[:len("cluster.invalid")] != "cluster.invalid" {
			t.Errorf("host=%s", r.Host)
		}
		fmt.Fprint(w, `{"ok":true}`)
	}))
	defer server.Close()
	u, _ := url.Parse(server.URL)
	_, port, _ := net.SplitHostPort(u.Host)
	c, err := controlhttp.New(controlhttp.Options{Endpoint: "https://cluster.invalid:" + port, Token: "token", Insecure: true, ResolveIP: "127.0.0.1"})
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	if _, err := c.Health(context.Background()); err != nil {
		t.Fatal(err)
	}
}
