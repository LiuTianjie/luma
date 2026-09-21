package nomad

import (
	"fmt"
	"math"
	"regexp"
	"strconv"
	"strings"

	"github.com/LiuTianjie/luma/internal/deployment"
)

var memoryPattern = regexp.MustCompile(`^(\d+(?:\.\d+)?)\s*([KMGkmg]?)([Ii]?)[Bb]?$`)
var durationPattern = regexp.MustCompile(`^(\d+(?:\.\d+)?)\s*(ns|us|ms|s|m|h)?$`)

func positiveRounded(value float64, label string) (int, error) {
	if math.IsNaN(value) || math.IsInf(value, 0) || value <= 0 || value >= float64(math.MaxInt) {
		return 0, fmt.Errorf("%s must be a finite positive value within supported range", label)
	}
	return max(1, int(math.RoundToEven(value))), nil
}
func cpu(value string) (int, error) {
	v, err := strconv.ParseFloat(strings.TrimSpace(value), 64)
	if err != nil {
		return 0, fmt.Errorf("invalid resources.cpus value: %q", value)
	}
	return positiveRounded(v*1000, "resources.cpus")
}
func memory(value string) (int, error) {
	m := memoryPattern.FindStringSubmatch(strings.TrimSpace(value))
	if m == nil {
		return 0, fmt.Errorf("cannot parse memory value: %q", value)
	}
	v, err := strconv.ParseFloat(m[1], 64)
	if err != nil {
		return 0, fmt.Errorf("invalid resources.memory")
	}
	factor := map[string]float64{"": 1.0 / (1024 * 1024), "K": 1.0 / 1024, "M": 1, "G": 1024}[strings.ToUpper(m[2])]
	return positiveRounded(v*factor, "resources.memory")
}
func duration(value string) (int64, error) {
	m := durationPattern.FindStringSubmatch(strings.TrimSpace(value))
	if m == nil {
		return 0, fmt.Errorf("cannot parse duration: %q", value)
	}
	v, err := strconv.ParseFloat(m[1], 64)
	if err != nil {
		return 0, fmt.Errorf("invalid duration")
	}
	factor := map[string]float64{"ns": 1, "us": 1e3, "ms": 1e6, "": 1e9, "s": 1e9, "m": 60e9, "h": 3600e9}[m[2]]
	n := v * factor
	if math.IsInf(n, 0) || n >= float64(math.MaxInt64) {
		return 0, fmt.Errorf("duration exceeds supported range")
	}
	return int64(n), nil
}

func resources(s deployment.Resources) (Resources, []string, error) {
	result := Resources{CPU: 100, MemoryMB: 256}
	var err error
	if s.Limits.CPUs != nil {
		if _, err := cpu(*s.Limits.CPUs); err != nil {
			return result, nil, err
		}
	}
	if s.Reservations.CPUs != nil {
		result.CPU, err = cpu(*s.Reservations.CPUs)
		if err != nil {
			return result, nil, err
		}
	}
	if s.Limits.Memory != nil {
		result.MemoryMaxMB, err = memory(*s.Limits.Memory)
		if err != nil {
			return result, nil, err
		}
		result.MemoryMB = min(256, result.MemoryMaxMB)
	}
	if s.Reservations.Memory != nil {
		result.MemoryMB, err = memory(*s.Reservations.Memory)
		if err != nil {
			return result, nil, err
		}
	}
	if result.MemoryMaxMB > 0 && result.MemoryMB > result.MemoryMaxMB {
		return result, nil, fmt.Errorf("resources.reservations.memory must not exceed resources.limits.memory")
	}
	var warnings []string
	if s.Limits.CPUs != nil {
		warnings = append(warnings, fmt.Sprintf("limits.cpus is not enforced: Luma uses elastic CPU sharing, not an independent CPU hard limit; scheduling reservation is %d MHz. Set reservations.cpus for the scheduling share and remove limits.cpus.", result.CPU))
	}
	return result, warnings, nil
}
