package nomad

import "github.com/LiuTianjie/luma/internal/deployment"

func composeMount(c deployment.Compose, m deployment.VolumeMount) Mount {
	mount := Mount{Type: m.Kind, Source: m.Source, Target: m.Target, Readonly: m.ReadOnly}
	if m.Kind == "volume" {
		if v, ok := c.Volumes[m.Source]; ok && v.Local() {
			mount.Type = "bind"
			mount.Source = v.LocalPath
		}
	}
	return mount
}
