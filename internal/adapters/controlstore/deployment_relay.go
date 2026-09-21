package controlstore

import (
	"encoding/json"
	"fmt"
	"slices"

	"github.com/LiuTianjie/luma/internal/deployment"
	"go.yaml.in/yaml/v3"
)

// Read just the historical routing fields; legacy manifests may contain storage
// fields that are intentionally no longer accepted by the Go deployment decoder.
func relayReservations(d document) ([]deployment.RelayReservation, error) {
	result := []deployment.RelayReservation{}
	for _, bucket := range []string{"services", "compose"} {
		kind := "service"
		if bucket == "compose" {
			kind = "compose"
		}
		keys := []string{}
		for key := range d.object(bucket) {
			keys = append(keys, key)
		}
		slices.Sort(keys)
		for _, key := range keys {
			raw := d.object(bucket)[key]
			var r struct {
				Name, Status, Manifest string
				TCPRelayPorts          []int `json:"tcpRelayPorts"`
			}
			if err := json.Unmarshal(raw, &r); err != nil {
				return nil, fmt.Errorf("invalid deployment relay record: %s", key)
			}
			ports := r.TCPRelayPorts
			if len(ports) == 0 && r.Manifest != "" {
				type service struct {
					Exposure    string `yaml:"exposure"`
					Port        int    `yaml:"port"`
					PublishPort int    `yaml:"publishPort"`
				}
				var manifest struct {
					service  `yaml:",inline"`
					Services map[string]service `yaml:"services"`
				}
				if yaml.Unmarshal([]byte(r.Manifest), &manifest) == nil {
					add := func(s service) {
						if s.Exposure == "tcp-relay" {
							p := s.PublishPort
							if p == 0 {
								p = s.Port
							}
							if p > 0 && !slices.Contains(ports, p) {
								ports = append(ports, p)
							}
						}
					}
					if kind == "service" {
						add(manifest.service)
					} else {
						for _, s := range manifest.Services {
							add(s)
						}
					}
				}
			}
			slices.Sort(ports)
			result = append(result, deployment.RelayReservation{Kind: kind, Slug: key, Name: r.Name, Status: r.Status, Ports: ports})
		}
	}
	return result, nil
}
