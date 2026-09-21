package storage

import (
	"errors"
	"fmt"
	"slices"
	"strings"
)

var ErrOwnership = errors.New("local storage ownership conflict")

type Mount struct {
	Service string `json:"service,omitempty"`
	Type    string `json:"type"`
	Source  string `json:"source"`
	Target  string `json:"target"`
}
type Binding struct {
	Node   string  `json:"node"`
	NodeID string  `json:"nodeId,omitempty"`
	Region string  `json:"region"`
	Mounts []Mount `json:"mounts"`
}
type Node struct {
	Name, ID, Region string
	Aliases          []string
	Ready, Builder   bool
}
type PlacementEvidence struct {
	Exists bool
	// OwnerIDs includes running/unknown allocations, or the most recent failed
	// allocations when none are active. An offline allocation still owns data.
	OwnerIDs, PinnedNames, PinnedIDs []string
	Mounts                           []Mount
	UnsupportedStorage               bool
}
type OwnershipRequest struct {
	Region         string
	Requested      []string
	Previous       Binding
	PreviousRecord bool
	Current        []Mount
	Runtime        PlacementEvidence
	Nodes          []Node
}

func Ownership(r OwnershipRequest) (Binding, error) {
	conflict := func(message string) (Binding, error) { return Binding{}, fmt.Errorf("%w: %s", ErrOwnership, message) }
	if r.Runtime.UnsupportedStorage {
		return conflict("existing job uses remote or unsupported storage; inspect and migrate its data explicitly")
	}
	if err := GuardSources(r.Previous.Mounts, r.Current); err != nil {
		return Binding{}, err
	}
	if err := GuardSources(r.Runtime.Mounts, r.Current); err != nil {
		return Binding{}, err
	}
	resolve := func(value string, idOnly bool) (Node, error) {
		matches := []Node{}
		for _, n := range r.Nodes {
			if value != "" && (n.ID == value || !idOnly && (n.Name == value || slices.Contains(n.Aliases, value))) {
				matches = append(matches, n)
			}
		}
		if len(matches) != 1 {
			return Node{}, fmt.Errorf("%w: cannot uniquely identify data node %s; restore its registration", ErrOwnership, value)
		}
		return matches[0], nil
	}
	owners := map[string]Node{}
	add := func(value string, idOnly bool) error {
		if value == "" {
			return nil
		}
		n, err := resolve(value, idOnly)
		if err != nil {
			return err
		}
		owners[n.Name] = n
		return nil
	}
	for _, v := range append(slices.Clone(r.Runtime.PinnedIDs), r.Runtime.OwnerIDs...) {
		if err := add(v, true); err != nil {
			return Binding{}, err
		}
	}
	for _, v := range append(slices.Clone(r.Runtime.PinnedNames), r.Previous.Node) {
		if err := add(v, false); err != nil {
			return Binding{}, err
		}
	}
	if err := add(r.Previous.NodeID, true); err != nil {
		return Binding{}, err
	}
	if len(owners) > 1 {
		return conflict("stored binding, runtime constraints or allocations disagree about the data node")
	}
	if len(owners) == 0 && (r.PreviousRecord || r.Runtime.Exists && len(r.Runtime.Mounts) > 0) {
		return conflict("cannot establish the node holding existing data; do not create a replacement empty volume")
	}
	requested := map[string]Node{}
	for _, v := range r.Requested {
		if v == "" {
			continue
		}
		n, err := resolve(v, false)
		if err != nil {
			return Binding{}, err
		}
		requested[n.Name] = n
	}
	if len(requested) > 1 {
		return conflict("a deployment's local storage must belong to one deployment node")
	}
	var selected Node
	for _, n := range owners {
		selected = n
	}
	for _, n := range requested {
		if selected.Name != "" && selected.Name != n.Name {
			return conflict("data belongs to node " + selected.Name + "; migrate and verify it before moving to " + n.Name)
		}
		selected = n
	}
	if selected.Name == "" {
		candidates := slices.Clone(r.Nodes)
		slices.SortFunc(candidates, func(a, b Node) int { return strings.Compare(a.Name, b.Name) })
		for _, n := range candidates {
			if n.Ready && !n.Builder && n.Region == r.Region && n.ID != "" {
				selected = n
				break
			}
		}
	}
	if selected.Name == "" || selected.ID == "" || !selected.Ready {
		return conflict("the data node must be ready and registered; an unavailable owner cannot be replaced automatically")
	}
	if selected.Region != r.Region || r.Previous.Region != "" && r.Previous.Region != r.Region {
		return conflict("the deployment region differs from its data node")
	}
	mounts := slices.Clone(r.Current)
	if len(mounts) == 0 {
		mounts = slices.Clone(r.Previous.Mounts)
	}
	return Binding{Node: selected.Name, NodeID: selected.ID, Region: r.Region, Mounts: mounts}, nil
}

func GuardSources(previous, current []Mount) error {
	old := map[string]Mount{}
	for _, m := range previous {
		key := m.Service + "\x00" + m.Target
		if p, ok := old[key]; ok && (p.Type != m.Type || p.Source != m.Source) {
			return fmt.Errorf("%w: ambiguous prior mount for %s", ErrOwnership, m.Target)
		}
		old[key] = m
	}
	for _, m := range current {
		if p, ok := old[m.Service+"\x00"+m.Target]; ok && (p.Type != m.Type || p.Source != m.Source) {
			return fmt.Errorf("%w: mount source changed for %s; migrate and verify the data before replacing its mount", ErrOwnership, m.Target)
		}
	}
	return nil
}
