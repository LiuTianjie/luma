package deployment

import (
	"fmt"
	"slices"
)

// RelayReservation is global: Traefik's TCP entrypoints are shared by all nodes.
type RelayReservation struct {
	Kind, Slug, Name, Status string
	Ports                    []int
}

func RelayPorts(services []Service) []int {
	ports := []int{}
	for _, s := range services {
		if s.Exposure == "tcp-relay" {
			port := s.PublishPort
			if port == 0 {
				port = s.Port
			}
			ports = append(ports, port)
		}
	}
	return ports
}

// CheckRelayPorts is also called inside the repository write transaction.
func CheckRelayPorts(kind, slug string, ports []int, reservations []RelayReservation) error {
	seen := map[int]bool{}
	for _, port := range ports {
		if port < 1 || port > 65535 || port == 80 || port == 443 {
			return inputError(fmt.Sprintf("invalid or reserved tcp-relay port: %d", port))
		}
		if seen[port] {
			return inputError(fmt.Sprintf("duplicate tcp-relay port in deployment: %d", port))
		}
		seen[port] = true
		for _, r := range reservations {
			if r.Kind == kind && r.Slug == slug {
				continue
			}
			if r.Status != "" && r.Status != "active" && r.Status != "pending" {
				continue
			}
			if slices.Contains(r.Ports, port) {
				return inputError(fmt.Sprintf("tcp-relay port %d is already used by %s/%s", port, r.Kind, r.Slug))
			}
		}
	}
	return nil
}

func RelayRefreshPorts(ports []int, reservations []RelayReservation) []int {
	result := []int{}
	for _, port := range ports {
		active := false
		for _, r := range reservations {
			active = active || r.Status == "active" && slices.Contains(r.Ports, port)
		}
		if !active && !slices.Contains(result, port) {
			result = append(result, port)
		}
	}
	slices.Sort(result)
	return result
}

// ResolveRelay uses the canonical node selected by Control. Explicit upstreams
// remain supported and do not require a deployment node pin.
func ResolveRelay(s Service, nodes map[string]NodeAddress) (Service, error) {
	if s.Exposure != "tailscale-relay" && s.Exposure != "tcp-relay" {
		return s, nil
	}
	if s.Exposure == "tailscale-relay" && (s.Relay.URL != "" || len(s.Relay.URLs) > 0 || s.Relay.Host != "") {
		return s, nil
	}
	if s.Exposure == "tcp-relay" && (s.TCP.Address != "" || len(s.TCP.Addresses) > 0 || s.TCP.Host != "") {
		return s, nil
	}
	if s.Node == "" {
		return s, inputError(s.Exposure + " requires node or an explicit upstream")
	}
	n, ok := nodes[s.Node]
	if !ok || n.Address == "" {
		return s, inputError("missing relay address for node " + s.Node)
	}
	port := s.PublishPort
	if port == 0 {
		port = s.Port
	}
	if s.Exposure == "tailscale-relay" {
		s.Relay.Host, s.Relay.Port = n.Address, port
	} else {
		s.TCP.Host, s.TCP.Port = n.Address, port
	}
	return s, nil
}
