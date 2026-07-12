import unittest

from luma.errors import LumaError
from luma import nomad_node as nn


class DetectTests(unittest.TestCase):
    def test_detect_os(self):
        self.assertEqual(nn.detect_os("Darwin"), "darwin")
        self.assertEqual(nn.detect_os("Linux"), "linux")

    def test_tailscale_ip_picks_ipv4(self):
        ip = nn.detect_tailscale_ip(run=lambda cmd: "100.115.5.84\n")
        self.assertEqual(ip, "100.115.5.84")

    def test_tailscale_ip_none_when_unavailable(self):
        self.assertIsNone(nn.detect_tailscale_ip(run=lambda cmd: None))

    def test_tailscale_ip_rejects_garbage(self):
        self.assertIsNone(nn.detect_tailscale_ip(run=lambda cmd: "not-an-ip\n"))

    def test_cpu_override_only_on_darwin(self):
        # Linux fingerprints CPU correctly -> no override.
        self.assertIsNone(nn.detect_cpu_total_compute("linux", run=lambda cmd: "8"))
        # Apple Silicon -> cores * heuristic.
        self.assertEqual(
            nn.detect_cpu_total_compute("darwin", run=lambda cmd: "10\n"),
            10 * nn.APPLE_MHZ_PER_CORE,
        )

    def test_cpu_override_fallback_when_sysctl_fails(self):
        val = nn.detect_cpu_total_compute("darwin", run=lambda cmd: None)
        self.assertEqual(val, 4 * nn.APPLE_MHZ_PER_CORE)


class RenderConfigTests(unittest.TestCase):
    def test_server_config_has_server_block_and_bind_all(self):
        cfg = nn.render_agent_config(
            os_name="linux", role="server", tailscale_ip="100.113.204.125",
            region="cn", node_name="manager-1",
            extra_meta={"ingress": "true", "egress": "true"},
        )
        # bind 0.0.0.0 so Control's localhost client reaches the API (a real bug
        # we hit binding only the Tailscale IP).
        self.assertIn('bind_addr = "0.0.0.0"', cfg)
        self.assertIn("bootstrap_expect = 1", cfg)
        self.assertIn('http = "100.113.204.125"', cfg)
        self.assertIn('region         = "cn"', cfg)
        self.assertIn('luma_tailscale_ip = "100.113.204.125"', cfg)
        self.assertIn('ingress = "true"', cfg)
        # volumes enabled — core components use host bind mounts.
        self.assertIn("volumes {", cfg)
        self.assertIn("enabled = true", cfg)
        # no cpu override on linux
        self.assertNotIn("cpu_total_compute", cfg)

    def test_client_config_has_retry_join_no_server_block(self):
        cfg = nn.render_agent_config(
            os_name="linux", role="client", tailscale_ip="100.69.154.50",
            region="home", node_name="lab",
            server_addrs=["100.113.204.125:4647"],
        )
        self.assertIn("server_join {", cfg)
        self.assertIn('retry_join = ["100.113.204.125:4647"]', cfg)
        self.assertIn('luma_tailscale_ip = "100.69.154.50"', cfg)
        self.assertNotIn("bootstrap_expect", cfg)

    def test_darwin_client_includes_cpu_override(self):
        cfg = nn.render_agent_config(
            os_name="darwin", role="client", tailscale_ip="100.115.5.84",
            region="home", node_name="home-mac-mini",
            server_addrs=["100.113.204.125:4647"],
            cpu_total_compute=30000,
        )
        self.assertIn("cpu_total_compute = 30000", cfg)

    def test_client_without_server_addrs_rejected(self):
        with self.assertRaises(LumaError):
            nn.render_agent_config(
                os_name="linux", role="client", tailscale_ip="100.1.1.1",
                region="home", node_name="x",
            )

    def test_missing_tailscale_ip_rejected(self):
        with self.assertRaises(LumaError):
            nn.render_agent_config(
                os_name="linux", role="server", tailscale_ip="",
                region="cn", node_name="m",
            )

    def test_bad_role_rejected(self):
        with self.assertRaises(LumaError):
            nn.render_agent_config(
                os_name="linux", role="worker", tailscale_ip="100.1.1.1",
                region="cn", node_name="m",
            )


class InstallCommandTests(unittest.TestCase):
    def test_linux_uses_systemd_and_amd64(self):
        out = nn.install_nomad_commands(os_name="linux", arch="x86_64", config_hcl="x")
        self.assertEqual(out["service_kind"], "systemd")
        self.assertIn(f"nomad_{nn.NOMAD_VERSION}_linux_amd64.zip", out["download_url"])
        self.assertIn("ExecStart=/usr/local/bin/nomad agent", out["service_unit"])

    def test_linux_systemd_unit_survives_reboot_races(self):
        unit = nn.install_nomad_commands(os_name="linux", arch="x86_64", config_hcl="x")["service_unit"]

        self.assertIn("Wants=network-online.target docker.service tailscaled.service", unit)
        self.assertIn("After=network-online.target docker.service tailscaled.service", unit)
        self.assertIn("StartLimitIntervalSec=0", unit)
        self.assertIn("Restart=always", unit)
        self.assertIn("RestartSec=5", unit)

    def test_docker_driver_allows_large_cold_image_pulls(self):
        config = nn.render_agent_config(
            os_name="linux",
            role="client",
            tailscale_ip="100.1.1.1",
            region="home",
            node_name="blg",
            server_addrs=["100.2.2.2:4647"],
        )

        self.assertIn('plugin "docker"', config)
        self.assertIn('pull_activity_timeout = "30m"', config)

    def test_darwin_uses_launchd_and_arm64(self):
        out = nn.install_nomad_commands(os_name="darwin", arch="arm64", config_hcl="x")
        self.assertEqual(out["service_kind"], "launchd")
        self.assertIn(f"nomad_{nn.NOMAD_VERSION}_darwin_arm64.zip", out["download_url"])
        # launchd PATH must include /usr/local/bin so docker driver finds docker
        self.assertIn("/usr/local/bin", out["service_unit"])
        self.assertIn("io.luma.nomad", out["service_unit"])

    def test_arch_normalization(self):
        self.assertEqual(nn._nomad_arch("aarch64"), "arm64")
        self.assertEqual(nn._nomad_arch("amd64"), "amd64")
        self.assertEqual(nn._nomad_arch("x86_64"), "amd64")

    def test_cni_plugins_url_matches_linux_arch(self):
        self.assertIn("cni-plugins-linux-amd64-v", nn.cni_plugins_url("x86_64"))
        self.assertIn("cni-plugins-linux-arm64-v", nn.cni_plugins_url("aarch64"))

    def test_version_is_pinned_stable(self):
        # Must not be a brand-new major (the 2.0.x checkpoint trap).
        self.assertEqual(nn.NOMAD_VERSION, "1.9.7")


if __name__ == "__main__":
    unittest.main()
