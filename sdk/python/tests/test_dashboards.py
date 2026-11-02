"""Configuration semantics and atomic delivery for standard Grafana dashboards."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from luma_observe.dashboards import DashboardConfigError, build_dashboard, main, write_dashboard


def configuration():
    return {
        "schema": 1, "uid": "application-custom", "title": "Custom application", "datasource": "custom-victoria",
        "variables": [{"name": "format", "query": "label_values(custom_requests_total, format)", "multi": True, "includeAll": True}],
        "panels": [{"title": "Application requests", "type": "timeseries", "expr": 'sum by(format)(rate(custom_requests_total{format=~"${format:regex}"}[$__rate_interval]))'}],
    }


class DashboardTests(unittest.TestCase):
    def test_custom_queries_variables_and_datasource_survive_rendering(self):
        config = configuration()
        before = deepcopy(config)
        dashboard = build_dashboard(config)
        self.assertEqual(config, before)
        self.assertEqual(dashboard["uid"], config["uid"])
        variable = dashboard["templating"]["list"][0]
        self.assertEqual(variable["query"]["query"], config["variables"][0]["query"])
        self.assertEqual(variable["allValue"], ".*")
        target = dashboard["panels"][0]["targets"][0]
        self.assertEqual(target["expr"], config["panels"][0]["expr"])
        self.assertEqual(target["datasource"]["uid"], "custom-victoria")
        self.assertTrue(target["range"])
        self.assertFalse(target["instant"])

    def test_multiple_queries_and_native_grafana_settings_are_preserved(self):
        config = configuration()
        config["panels"] = [{
            "title": "Delivery", "type": "stat", "unit": "percentunit",
            "targets": [{"expr": "application_ratio", "legend": "ready"}, {"expr": "application_other_ratio", "instant": False}],
            "thresholds": {"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "green", "value": 0.99}]},
            "options": {"colorMode": "value"},
            "fieldConfig": {"overrides": [{"matcher": {"id": "byName", "options": "ready"}, "properties": [{"id": "decimals", "value": 2}]}]},
        }]
        panel = build_dashboard(config)["panels"][0]
        self.assertEqual([target["refId"] for target in panel["targets"]], ["A", "B"])
        self.assertEqual([target["instant"] for target in panel["targets"]], [True, False])
        self.assertEqual(panel["fieldConfig"]["defaults"]["thresholds"], config["panels"][0]["thresholds"])
        self.assertEqual(panel["fieldConfig"]["overrides"], config["panels"][0]["fieldConfig"]["overrides"])
        self.assertEqual(panel["options"]["colorMode"], "value")

    def test_invalid_or_ambiguous_configuration_fails_before_delivery(self):
        cases = [
            {"schema": 2}, {"schema": True}, {"uid": "../other"}, {"refresh": "100"},
            {"panels": []}, {"panels": [{"title": "X", "expr": "up", "targets": [{"expr": "up"}]}]},
            {"panels": [{"title": "X", "expr": "up", "instant": "false"}]},
            {"panels": [{"title": "X", "expr": "up", "unit": "s", "fieldConfig": {"defaults": {"unit": "ms"}}}]},
            {"variables": [{"name": "task", "query": "a"}, {"name": "task", "query": "b"}]},
            {"panels": [{"title": "X", "expr": "up", "max": float("inf")}]},
            {"token": "unexpected-secret-setting"},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(DashboardConfigError):
                    build_dashboard({**configuration(), **changes})

    def test_atomic_replace_keeps_previous_dashboard_when_write_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "dashboard.json"
            destination.write_text("previous")
            with patch("luma_observe.dashboards.os.replace", side_effect=OSError("read-only target")):
                with self.assertRaises(OSError):
                    write_dashboard(configuration(), destination)
            self.assertEqual(destination.read_text(), "previous")
            self.assertEqual(list(Path(directory).iterdir()), [destination])
            write_dashboard(configuration(), destination)
            self.assertEqual(json.loads(destination.read_text())["uid"], "application-custom")
            self.assertEqual(list(Path(directory).iterdir()), [destination])

    def test_cli_produces_offline_provisioning_json(self):
        with tempfile.TemporaryDirectory() as directory:
            source, destination = Path(directory) / "config.json", Path(directory) / "dashboards" / "application.json"
            source.write_text(json.dumps(configuration()))
            with patch("builtins.print"):
                self.assertEqual(main([str(source), "--output", str(destination)]), 0)
            self.assertEqual(json.loads(destination.read_text())["title"], "Custom application")


if __name__ == "__main__":
    unittest.main()
