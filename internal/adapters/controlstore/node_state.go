package controlstore

import (
	"slices"
	"time"
)

func nodeAgentStatus(r document) string {
	a := r.object("agent")
	switch {
	case a.text("status") == "ready", a.text("status") == "online" && a.number("lastSeen") > 0 && time.Now().Unix()-a.number("lastSeen") <= 120:
		return "ready"
	case a.text("status") == "provisioned":
		return "provisioned"
	case a.text("status") != "":
		return "offline"
	case a.text("tokenHash") != "":
		return "provisioned"
	default:
		return "missing"
	}
}
func deploymentNodeReady(r document) bool {
	status := r.text("nomadStatus")
	if status == "" {
		status = r.text("nomadState")
	}
	eligibility := r.text("schedulingEligibility")
	if eligibility == "" {
		eligibility = r.text("nomadSchedulingEligibility")
	}
	return nodeAgentStatus(r) == "ready" && !slices.Contains([]string{"down", "missing", "offline", "disconnected", "unknown"}, status) && eligibility != "ineligible" && eligibility != "disabled" && !r.boolean("drain") && !r.boolean("nomadDrain")
}
