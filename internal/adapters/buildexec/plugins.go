package buildexec

import (
	"os"
	"os/exec"
	"path/filepath"
)

func PluginDirectories() []string {
	dirs := []string{"/usr/local/lib/docker/cli-plugins", "/usr/lib/docker/cli-plugins", "/usr/libexec/docker/cli-plugins", "/opt/homebrew/lib/docker/cli-plugins", "/Applications/Docker.app/Contents/Resources/cli-plugins", "/Applications/OrbStack.app/Contents/MacOS/cli-plugins"}
	if userDir, err := os.UserHomeDir(); err == nil {
		dirs = append(dirs, filepath.Join(userDir, ".docker/cli-plugins"))
	}
	if path, err := exec.LookPath("docker-buildx"); err == nil {
		dirs = append(dirs, filepath.Dir(path))
	}
	found := []string{}
	for _, dir := range dirs {
		if info, err := os.Stat(filepath.Join(dir, "docker-buildx")); err == nil && !info.IsDir() && info.Mode()&0111 != 0 {
			found = append(found, dir)
		}
	}
	return found
}
