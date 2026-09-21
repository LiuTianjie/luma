// Package routeprobe reports public HTTP reachability independently of Nomad's
// allocation health. It never restarts applications or changes infrastructure.
package routeprobe

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"io"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/LiuTianjie/luma/internal/deployment"
)

type Prober struct {
	http             *http.Client
	Settle, Interval time.Duration
}

func New() *Prober {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	return &Prober{http: &http.Client{Transport: transport, Timeout: 15 * time.Second, CheckRedirect: func(req *http.Request, via []*http.Request) error {
		if len(via) >= 5 || req.URL.Scheme != "https" {
			return http.ErrUseLastResponse
		}
		return nil
	}}, Settle: 30 * time.Second, Interval: time.Second}
}
func (p *Prober) Close() { p.http.CloseIdleConnections() }
func (p *Prober) request(ctx context.Context, method, target string) (int, http.Header, []byte, error) {
	req, err := http.NewRequestWithContext(ctx, method, target, nil)
	if err != nil {
		return 0, nil, nil, err
	}
	req.Header.Set("User-Agent", "luma-control-route-probe")
	r, err := p.http.Do(req)
	if err != nil {
		return 0, nil, nil, err
	}
	defer r.Body.Close()
	body, err := io.ReadAll(io.LimitReader(r.Body, 4096))
	return r.StatusCode, r.Header, body, err
}
func (p *Prober) Probe(ctx context.Context, s deployment.Service) (deployment.RouteHealth, error) {
	if s.Exposure == "tcp-relay" {
		return p.probeTCP(ctx, s)
	}
	h := deployment.RouteHealth{Kind: "public-route", Target: "https://" + s.Domain + "/", CheckedAt: time.Now().Unix()}
	settle := p.Settle
	if settle <= 0 {
		settle = 30 * time.Second
	}
	probeCtx, cancel := context.WithTimeout(ctx, settle)
	defer cancel()
	for {
		status, headers, body, err := p.request(probeCtx, "HEAD", h.Target)
		if err != nil {
			if ctx.Err() != nil {
				return h, ctx.Err()
			}
			if h.Status == "unhealthy" {
				return h, fmt.Errorf("%w: %s", deployment.ErrRuntime, h.Message)
			}
			h.Status = "inconclusive"
			h.Message = "Public route probe inconclusive: " + h.Target
			return h, nil
		}
		if status == 404 && len(body) == 0 {
			_, headers, body, err = p.request(probeCtx, "GET", h.Target)
			if err != nil {
				h.Status = "inconclusive"
				h.Message = "Public route probe inconclusive: " + h.Target
				return h, nil
			}
		}
		missing := status == 404 && strings.Contains(strings.ToLower(headers.Get("Server")), "traefik") && strings.EqualFold(strings.TrimSpace(string(body)), "404 page not found")
		if status != 502 && status != 503 && status != 504 && !missing {
			h.Status = "ready"
			h.Message = fmt.Sprintf("Public route reachable: %s -> HTTP %d", h.Target, status)
			return h, nil
		}
		h.Status = "unhealthy"
		h.Message = fmt.Sprintf("Public route unhealthy: %s -> HTTP %d", h.Target, status)
		if missing {
			h.Message += " (Traefik router not found)"
		}
		interval := p.Interval
		if interval <= 0 {
			interval = time.Second
		}
		timer := time.NewTimer(interval)
		select {
		case <-probeCtx.Done():
			timer.Stop()
			return h, fmt.Errorf("%w: %s", deployment.ErrRuntime, h.Message)
		case <-timer.C:
		}
	}
}

// TrustPEM adds an explicit private CA without weakening hostname verification.
func (p *Prober) TrustPEM(pem []byte) error {
	roots, err := x509.SystemCertPool()
	if err != nil {
		roots = x509.NewCertPool()
	}
	if !roots.AppendCertsFromPEM(pem) {
		return fmt.Errorf("route CA file contains no certificates")
	}
	transport := p.http.Transport.(*http.Transport)
	transport.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12, RootCAs: roots}
	return nil
}

func (p *Prober) probeTCP(ctx context.Context, s deployment.Service) (deployment.RouteHealth, error) {
	port := s.PublishPort
	if port == 0 {
		port = s.Port
	}
	h := deployment.RouteHealth{Kind: "public-tcp", Target: net.JoinHostPort(s.Domain, strconv.Itoa(port)), CheckedAt: time.Now().Unix()}
	dialer := net.Dialer{Timeout: 5 * time.Second}
	conn, err := dialer.DialContext(ctx, "tcp", h.Target)
	if err != nil {
		if ctx.Err() != nil {
			return h, ctx.Err()
		}
		h.Status, h.Message = "inconclusive", "Public TCP probe inconclusive: "+h.Target+"; verify the Traefik entrypoint and firewall"
		return h, nil
	}
	conn.Close()
	h.Status, h.Message = "ready", "Public TCP connection established: "+h.Target
	return h, nil
}
