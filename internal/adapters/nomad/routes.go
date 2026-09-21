package nomad

import (
	"fmt"
	"github.com/LiuTianjie/luma/internal/deployment"
	"go.yaml.in/yaml/v3"
	"net"
	"strconv"
)

// Dynamic map keys here are Traefik router/service names, not shared application state.
type routeDocument struct {
	HTTP *routeSection `yaml:"http,omitempty"`
	TCP  *routeSection `yaml:"tcp,omitempty"`
}
type routeSection struct {
	Routers  map[string]router       `yaml:"routers"`
	Services map[string]routeService `yaml:"services"`
}
type router struct {
	Rule        string   `yaml:"rule"`
	Priority    int      `yaml:"priority,omitempty"`
	EntryPoints []string `yaml:"entryPoints"`
	TLS         *tls     `yaml:"tls,omitempty"`
	Service     string   `yaml:"service"`
}
type tls struct {
	CertResolver string `yaml:"certResolver"`
}
type routeService struct {
	LoadBalancer loadBalancer `yaml:"loadBalancer"`
}
type loadBalancer struct {
	Servers []upstream `yaml:"servers"`
}
type upstream struct {
	URL     string `yaml:"url,omitempty"`
	Address string `yaml:"address,omitempty"`
}

func compileRoute(s deployment.Service, c deployment.RenderConfig) (string, error) {
	if s.Exposure != "tailscale-relay" && s.Exposure != "tcp-relay" {
		return "", nil
	}
	name := s.Slug()
	port := s.PublishPort
	if port == 0 {
		port = s.Port
	}
	host := s.Node
	if host == "" {
		host = "auto-" + s.Region + "-node"
	}
	r := router{Service: name}
	servers := []upstream{}
	doc := routeDocument{}
	if s.Exposure == "tailscale-relay" {
		r.Rule = "Host(`" + s.Domain + "`)"
		r.Priority = s.Relay.Priority
		r.EntryPoints = []string{c.Entrypoint}
		r.TLS = &tls{c.CertResolver}
		if len(s.Relay.URLs) > 0 {
			for _, value := range s.Relay.URLs {
				servers = append(servers, upstream{URL: value})
			}
		} else {
			value := s.Relay.URL
			if value == "" {
				if s.Relay.Host != "" {
					host = s.Relay.Host
				}
				if s.Relay.Port > 0 {
					port = s.Relay.Port
				}
				value = s.Relay.Scheme + "://" + net.JoinHostPort(host, strconv.Itoa(port))
			}
			servers = append(servers, upstream{URL: value})
		}
	} else {
		r.Rule = "HostSNI(`*`)"
		r.EntryPoints = []string{fmt.Sprintf("tcp-%d", port)}
		if len(s.TCP.Addresses) > 0 {
			for _, value := range s.TCP.Addresses {
				servers = append(servers, upstream{Address: value})
			}
		} else {
			value := s.TCP.Address
			if value == "" {
				if s.TCP.Host != "" {
					host = s.TCP.Host
				}
				if s.TCP.Port > 0 {
					port = s.TCP.Port
				}
				value = net.JoinHostPort(host, strconv.Itoa(port))
			}
			servers = append(servers, upstream{Address: value})
		}
	}
	section := &routeSection{Routers: map[string]router{name: r}, Services: map[string]routeService{name: {LoadBalancer: loadBalancer{Servers: servers}}}}
	if s.Exposure == "tailscale-relay" {
		doc.HTTP = section
	} else {
		doc.TCP = section
	}
	data, err := yaml.Marshal(doc)
	return string(data), err
}
