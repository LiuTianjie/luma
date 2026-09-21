package manifest

import (
	"github.com/LiuTianjie/luma/internal/deployment"
	"strings"
)

func decodeMounts(r *reader, value any) ([]deployment.VolumeMount, bool) {
	if value == nil {
		return nil, false
	}
	list, ok := value.([]any)
	if !ok {
		r.fail("volumes must be a list")
		return nil, false
	}
	var mounts []deployment.VolumeMount
	for _, value := range list {
		var m deployment.VolumeMount
		switch v := value.(type) {
		case string:
			parts := strings.Split(v, ":")
			if len(parts) == 1 {
				continue
			}
			m.Source, m.Target = parts[0], parts[1]
			m.ReadOnly = len(parts) > 2 && parts[2] == "ro"
		case map[string]any:
			m.Source = r.text(v["source"], "volume.source", "")
			if m.Source == "" {
				continue
			}
			m.Target = r.text(v["target"], "volume.target", "")
			m.Kind = strings.ToLower(r.text(v["type"], "volume.type", ""))
			m.ReadOnly = r.boolean(v["read_only"], "volume.read_only")
		default:
			r.fail("volume entry must be a string or mapping")
			continue
		}
		if m.Source == "" || m.Target == "" {
			r.fail("volume mount requires source and target")
			continue
		}
		if m.Kind != "volume" && m.Kind != "bind" {
			m.Kind = "volume"
			if strings.ContainsAny(m.Source[:1], "/.~") {
				m.Kind = "bind"
			}
		}
		mounts = append(mounts, m)
	}
	return mounts, len(list) > 0
}
