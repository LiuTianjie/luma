import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from luma.cdn import (
    AliyunApiError,
    _param_value,
    acs3_signature,
    ensure_certificate,
    ensure_record,
    ensure_site,
    parse_cdn_block,
    purge_requests,
)
from luma.cloudflare import sync_dns
from luma.config import LumaConfig
from luma.control.server import _purge_cdn_step, handle_deployment, handle_service_remove
from luma.control.state import init_state, load_state, mutate_state
from luma.errors import LumaError
from luma.service import ServiceSpec, load_service
from tests.support import _restore_env, _set_env


class FakeEsa:
    """In-memory Alibaba Cloud ESA account with one plan."""

    def __init__(self, *, sites=None, records=None, cert_after_polls=1):
        self.sites = dict(sites or {})
        self.records = dict(records or {})
        self.global_configs = set()
        self.cert_polls = 0
        self.cert_after_polls = cert_after_polls
        self.cert_applied = False
        self.calls = []

    def esa(self, action, **params):
        self.calls.append((action, params))
        if action == "ListUserRatePlanInstances":
            return {"InstanceInfo": [{"InstanceId": "sp-1", "PlanName": "basic", "Coverages": "domestic,overseas,global", "Sites": []}]}
        if action == "ListSites":
            site = self.sites.get(params["SiteName"])
            return {"Sites": [site] if site else []}
        if action == "CreateSite":
            self.sites[params["SiteName"]] = {"SiteId": 77, "SiteName": params["SiteName"], "Status": "pending", "AccessType": "CNAME", "Coverage": params["Coverage"], "VerifyCode": "verify_abc"}
            return {"SiteId": 77, "VerifyCode": "verify_abc"}
        if action == "VerifySite":
            for site in self.sites.values():
                site["Status"] = "active"
            return {"Passed": True}
        if action in {"ListOriginRules", "ListCompressionRules"}:
            return {"Configs": [{"ConfigId": 1, "OriginScheme": "follow"}] if action in self.global_configs else []}
        if action == "CreateOriginRule":
            self.global_configs.add("ListOriginRules")
            return {"ConfigId": 1}
        if action == "CreateCompressionRule":
            self.global_configs.add("ListCompressionRules")
            return {"ConfigId": 2}
        if action == "UpdateManagedTransform":
            return {}
        if action == "ListRecords":
            record = self.records.get(params["RecordName"])
            return {"Records": [record] if record else []}
        if action in {"CreateRecord", "UpdateRecord"}:
            name = params.get("RecordName") or next(n for n, r in self.records.items() if r["RecordId"] == params["RecordId"])
            self.records[name] = {
                "RecordId": 501, "RecordName": name, "RecordType": "A/AAAA", "Proxied": params["Proxied"],
                "Data": {"Value": params["Data"]["value"]}, "RecordCname": f"{name}.cnamezone.com",
            }
            return {"RecordId": 501}
        if action == "DeleteRecord":
            self.records = {name: r for name, r in self.records.items() if r["RecordId"] != params["RecordId"]}
            return {}
        if action == "ListCertificatesByRecord":
            if self.cert_applied:
                self.cert_polls += 1
            ready = self.cert_applied and self.cert_polls >= self.cert_after_polls
            status = "configured" if ready else ("applying" if self.cert_applied else "none")
            return {"Result": [{"RecordName": params["RecordName"], "Status": status, "Certificates": [{"Type": "free"}] if ready else []}]}
        if action == "ApplyCertificate":
            self.cert_applied = True
            return {"Result": [{"Domain": params["Domains"], "Status": "Applying"}]}
        if action == "PurgeCaches":
            return {"TaskId": "9001"}
        raise AssertionError(f"unexpected ESA action {action}")

    def actions(self):
        return [action for action, _params in self.calls]


class CdnManifestTests(unittest.TestCase):
    def test_short_and_full_forms(self):
        self.assertIsNone(parse_cdn_block("esa").purge)
        spec = parse_cdn_block({"provider": "esa", "scope": "overseas", "purge": ["/", "/blog/*", "/"]})
        self.assertEqual((spec.scope, spec.purge), ("overseas", ("/", "/blog/*")))
        self.assertEqual(parse_cdn_block({"provider": "esa", "purge": []}).purge, ())
        self.assertIsNone(parse_cdn_block(None))
        self.assertIsNone(parse_cdn_block(False))

    def test_invalid_blocks_are_rejected(self):
        for raw, message in [
            ("aliyun", "provider must be one of"),
            ({"provider": "esa", "scope": "mars"}, "scope must be one of"),
            ({"provider": "esa", "purge": "/"}, "purge must be a list"),
            ({"provider": "esa", "purge": ["index.html"]}, "must be URL paths"),
            ({"provider": "esa", "purge": ["/a*b"]}, "trailing /\\* directory"),
            ({"provider": "esa", "ttl": 1}, "unsupported cdn field"),
        ]:
            with self.subTest(raw=raw), self.assertRaisesRegex(LumaError, message):
                parse_cdn_block(raw)

    def test_purge_requests(self):
        self.assertEqual(purge_requests("a.example.com", None), [{"Type": "hostname", "Content": {"Hostnames": ["a.example.com"]}, "Force": False}])
        self.assertEqual(
            purge_requests("a.example.com", ["/", "/sitemap.xml", "/blog/*"]),
            [
                {"Type": "file", "Content": {"Files": ["https://a.example.com/", "https://a.example.com/sitemap.xml"]}},
                {"Type": "directory", "Content": {"Directories": ["https://a.example.com/blog/"]}, "Force": False},
            ],
        )
        self.assertEqual(purge_requests("a.example.com", ()), [])

    def test_object_parameters_are_sent_as_json_strings(self):
        self.assertEqual(_param_value({"Hostnames": ["a.example.com"]}), '{"Hostnames":["a.example.com"]}')
        self.assertEqual(_param_value(True), "true")

    def _load(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.yaml"
            path.write_text(yaml.safe_dump(data), encoding="utf-8")
            return load_service(path)

    def test_service_manifest_accepts_cdn_on_traefik_exposures_only(self):
        base = {"name": "web", "image": "nginx", "region": "cn", "domain": "web.example.com", "port": 80, "cdn": "esa"}
        self.assertEqual(self._load({**base, "exposure": "cn-edge"}).cdn.provider, "esa")
        with self.assertRaisesRegex(LumaError, "cdn requires exposure"):
            self._load({**base, "exposure": "tcp-relay"})

    def test_compose_sidecar_cdn(self):
        from luma.compose import _load_compose_services

        services = _load_compose_services({"web": {"exposure": "cn-edge", "domain": "w.example.com", "port": 80, "cdn": {"provider": "esa", "purge": ["/"]}}})
        self.assertEqual(services["web"].cdn.purge, ("/",))
        with self.assertRaisesRegex(LumaError, "services.web.cdn.provider"):
            _load_compose_services({"web": {"cdn": "other"}})


class AcsSignatureTests(unittest.TestCase):
    def test_matches_alibaba_cloud_documentation_vector(self):
        headers = {
            "x-acs-action": "RunInstances",
            "x-acs-version": "2014-05-26",
            "x-acs-date": "2023-10-26T10:22:32Z",
            "x-acs-signature-nonce": "3156853299f313e23d1673dc12e1703d",
            "x-acs-content-sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        }
        authorization, signature = acs3_signature(
            method="POST",
            host="ecs.cn-shanghai.aliyuncs.com",
            headers=headers,
            query="ImageId=win2019_1809_x64_dtc_zh-cn_40G_alibase_20230811.vhd&RegionId=cn-shanghai",
            body=b"",
            key_id="YourAccessKeyId",
            key_secret="YourAccessKeySecret",
        )
        self.assertEqual(signature, "06563a9e1b43f5dfe96b81484da74bceab24a1d853912eee15083a6f0f3283c0")
        self.assertTrue(authorization.startswith("ACS3-HMAC-SHA256 Credential=YourAccessKeyId,SignedHeaders=host;x-acs-action;"))


class EsaOperationTests(unittest.TestCase):
    def test_new_site_is_created_on_the_plan_and_verified_by_txt(self):
        client = FakeEsa()
        published = []
        site, notes = ensure_site(
            client, site_name="example.com", scope="global",
            publish_txt=lambda name, value: published.append((name, value)) or name, sleep=lambda _s: None,
        )
        self.assertEqual(site["Status"], "active")
        create = next(params for action, params in client.calls if action == "CreateSite")
        self.assertEqual((create["AccessType"], create["Coverage"], create["InstanceId"]), ("CNAME", "global", "sp-1"))
        self.assertEqual(published, [("_esaauth.example.com", "verify_abc")])
        self.assertIn("site example.com created (global)", notes)

    def test_ns_access_sites_are_refused(self):
        client = FakeEsa(sites={"example.com": {"SiteId": 1, "SiteName": "example.com", "Status": "active", "AccessType": "NS"}})
        with self.assertRaisesRegex(LumaError, "CNAME-access sites only"):
            ensure_site(client, site_name="example.com", scope="global", publish_txt=lambda *_: "", sleep=lambda _s: None)

    def test_record_is_created_proxied_and_updated_when_the_edge_moves(self):
        client = FakeEsa()
        record, notes = ensure_record(client, site_id=77, domain="app.example.com", origin="203.0.113.10")
        self.assertEqual(record["RecordCname"], "app.example.com.cnamezone.com")
        create = next(params for action, params in client.calls if action == "CreateRecord")
        self.assertEqual((create["Type"], create["Proxied"], create["BizName"], create["Data"]), ("A/AAAA", True, "web", {"value": "203.0.113.10"}))
        _record, notes = ensure_record(client, site_id=77, domain="app.example.com", origin="203.0.113.10")
        self.assertEqual(notes, [])
        _record, notes = ensure_record(client, site_id=77, domain="app.example.com", origin="198.51.100.7")
        self.assertEqual(notes, ["record updated -> 198.51.100.7"])

    def test_certificate_is_applied_once_and_awaited(self):
        client = FakeEsa(cert_after_polls=2)
        message = ensure_certificate(client, site_id=77, domain="app.example.com", sleep=lambda _s: None)
        self.assertEqual(message, "certificate issued (Let's Encrypt)")
        self.assertEqual(client.actions().count("ApplyCertificate"), 1)
        self.assertEqual(ensure_certificate(client, site_id=77, domain="app.example.com", sleep=lambda _s: None), "certificate ready")
        self.assertEqual(client.actions().count("ApplyCertificate"), 1)

    def test_duplicate_certificate_application_is_tolerated(self):
        client = FakeEsa()
        original = client.esa

        def esa(action, **params):
            if action == "ApplyCertificate":
                client.cert_applied = True
                raise AliyunApiError(action, 400, "InvalidParameter.Domains", "already submitted")
            return original(action, **params)

        client.esa = esa
        self.assertEqual(ensure_certificate(client, site_id=77, domain="app.example.com", sleep=lambda _s: None), "certificate issued (Let's Encrypt)")


class SiteDefaultsTests(unittest.TestCase):
    def test_existing_http_origin_rule_is_switched_to_follow(self):
        from luma.cdn import configure_site

        client = MagicMock()
        client.esa.side_effect = lambda action, **params: {
            "ListOriginRules": {"Configs": [{"ConfigId": 5, "OriginScheme": "http"}]},
            "ListCompressionRules": {"Configs": [{"ConfigId": 6}]},
        }.get(action, {})
        applied = configure_site(client, 77)
        self.assertIn("origin follow (was http)", applied)
        calls = [(call.args[0], call.kwargs) for call in client.esa.call_args_list]
        self.assertIn(("UpdateOriginRule", {"SiteId": 77, "ConfigId": 5, "OriginScheme": "follow"}), calls)
        self.assertNotIn("CreateCompressionRule", [name for name, _ in calls])


class CdnDnsTests(unittest.TestCase):
    def _service(self):
        return ServiceSpec(source=Path("a.yaml"), name="web", image="nginx", region="cn", public=True, exposure="cn-edge", domain="web.example.com", port=80)

    def _config(self):
        return LumaConfig(path=None, raw={"providers": {"dns": {"type": "cloudflare", "zone": "example.com", "zoneId": "z1", "edgeTarget": "203.0.113.10"}}})

    def test_switching_to_esa_replaces_the_a_record_in_place(self):
        client = MagicMock()
        client.request.side_effect = lambda method, path, body=None: (
            {"result": [{"id": "r1", "type": "A", "content": "203.0.113.10"}, {"id": "t1", "type": "TXT", "content": "x"}]} if method == "GET" else {"result": {}}
        )
        with patch("luma.cloudflare.CloudflareClient", return_value=client):
            message = sync_dns(self._config(), self._service(), secrets={"CLOUDFLARE_API_TOKEN": "t"}, override={"target": "web.example.com.cnamezone.com", "type": "CNAME", "proxied": False})
        self.assertEqual(message, "DNS updated: web.example.com -> web.example.com.cnamezone.com")
        writes = [call.args for call in client.request.call_args_list if call.args[0] != "GET"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][:2], ("PUT", "/zones/z1/dns_records/r1"))
        self.assertEqual((writes[0][2]["type"], writes[0][2]["proxied"]), ("CNAME", False))

    def test_disabling_esa_turns_the_cname_back_into_an_a_record(self):
        client = MagicMock()
        client.request.side_effect = lambda method, path, body=None: (
            {"result": [{"id": "c1", "type": "CNAME", "content": "x.cnamezone.com"}]} if method == "GET" else {"result": {}}
        )
        with patch("luma.cloudflare.CloudflareClient", return_value=client):
            sync_dns(self._config(), self._service(), secrets={"CLOUDFLARE_API_TOKEN": "t"})
        put = next(call.args for call in client.request.call_args_list if call.args[0] == "PUT")
        self.assertEqual((put[1], put[2]["type"], put[2]["content"]), ("/zones/z1/dns_records/c1", "A", "203.0.113.10"))


class PurgeStepTests(unittest.TestCase):
    def _service(self):
        return ServiceSpec(source=Path("a.yaml"), name="web", image="nginx", region="cn", public=True, exposure="cn-edge", domain="web.example.com", port=80, cdn=parse_cdn_block("esa"))

    def test_provider_failure_is_a_warning(self):
        steps = []
        with patch("luma.control.server.purge_service_cdn", side_effect=LumaError("QuotaExceeded")):
            message = _purge_cdn_step(steps, None, {}, self._service(), prepared={}, skip_orchestrator=False)
        self.assertEqual(steps[-1]["status"], "warning")
        self.assertIn("deployment kept", message)

    def test_unexpected_errors_propagate_unwrapped(self):
        steps = []
        with patch("luma.control.server.purge_service_cdn", side_effect=KeyError("boom")):
            with self.assertRaises(KeyError):
                _purge_cdn_step(steps, None, {}, self._service(), prepared={}, skip_orchestrator=False)
        self.assertEqual(steps[-1]["status"], "fail")

    def test_new_record_is_not_purged(self):
        steps = []
        with patch("luma.control.server.purge_service_cdn") as purge:
            _purge_cdn_step(steps, None, {}, self._service(), prepared={"created": True}, skip_orchestrator=False)
        purge.assert_not_called()
        self.assertIn("no cache yet", steps[-1]["message"])


class CdnDeploymentFlowTests(unittest.TestCase):
    SECRETS = ("CLOUDFLARE_API_TOKEN", "LUMA_ALIBABA_CLOUD_ACCESS_KEY_ID", "LUMA_ALIBABA_CLOUD_ACCESS_KEY_SECRET")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # handle_deployment copies global secrets into os.environ; restore them.
        self.old_env = {name: os.environ.get(name) for name in (*self.SECRETS, "LUMA_CONTROL_STATE_DIR", "LUMA_CONTROL_CONFIG")}
        _set_env("LUMA_CONTROL_STATE_DIR", str(self.root / "state"))
        _set_env("LUMA_CONTROL_CONFIG", str(self.root / "luma.yaml"))
        self.state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
        (self.root / "luma.yaml").write_text(yaml.safe_dump({
            "providers": {"dns": {"type": "cloudflare", "zone": "example.com", "zoneId": "z1"}},
            "defaults": {"stackRoot": str(self.root / "stacks")},
            "nodes": {"edge": {"host": "edge", "publicIp": "203.0.113.10", "region": "cn", "roles": ["edge"]}},
        }), encoding="utf-8")

    def tearDown(self):
        for name, value in self.old_env.items():
            _restore_env(name, value)
        self.tmp.cleanup()

    def _add_secrets(self):
        def add(current):
            current.setdefault("secrets", {}).update({
                "CLOUDFLARE_API_TOKEN": "cf",
                "LUMA_ALIBABA_CLOUD_ACCESS_KEY_ID": "LTAIexample",
                "LUMA_ALIBABA_CLOUD_ACCESS_KEY_SECRET": "secret",
            })

        mutate_state(add)

    def test_deploy_prepares_points_dns_issues_certificate_purges_and_remove_deletes(self):
        self._add_secrets()
        manifest = yaml.safe_dump({
            "name": "web", "image": "nginx:alpine", "region": "cn", "exposure": "cn-edge",
            "domain": "web.example.com", "port": 80, "cdn": "esa",
        })
        fake = FakeEsa()
        sync = MagicMock(return_value="DNS updated")
        txt = MagicMock(side_effect=lambda config, name, content, secrets=None: name)
        probe_error = urllib.error.HTTPError("https://web.example.com/", 404, "not found", {}, None)
        with patch("luma.control.cdn.AliyunClient", return_value=fake), patch(
            "luma.control.cdn.upsert_txt_record", txt
        ), patch("luma.cdn.time.sleep"), patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="ok"), patch(
            "luma.control.server.sync_dns", sync
        ), patch("luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"), patch(
            "luma.control.server.urllib.request.urlopen", side_effect=probe_error
        ):
            first = handle_deployment(self.state["deployToken"], {"manifest": manifest, "sourceName": "web.yaml"})
            second = handle_deployment(self.state["deployToken"], {"manifest": manifest, "sourceName": "web.yaml"})

        names = [step["name"] for step in first["steps"]]
        self.assertLess(names.index("Prepare CDN"), names.index("Sync DNS"))
        self.assertLess(names.index("Sync DNS"), names.index("ESA certificate"))
        self.assertLess(names.index("ESA certificate"), names.index("Deploy Nomad job"))
        self.assertLess(names.index("Probe public route"), names.index("Purge CDN"))
        self.assertEqual(txt.call_args.args[1:3], ("_esaauth.example.com", "verify_abc"))
        self.assertEqual(sync.call_args.kwargs["override"], {"target": "web.example.com.cnamezone.com", "type": "CNAME", "proxied": False})
        self.assertIn("no cache yet", first["cdn"]["purge"])
        self.assertIn("whole hostname", second["cdn"]["purge"])
        self.assertEqual(fake.actions().count("CreateSite"), 1)
        self.assertEqual(fake.actions().count("CreateOriginRule"), 1)
        origin = next(params for action, params in fake.calls if action == "CreateOriginRule")
        self.assertEqual(origin["OriginScheme"], "follow")
        purge = next(params for action, params in fake.calls if action == "PurgeCaches")
        self.assertEqual((purge["SiteId"], purge["Type"], purge["Content"]), (77, "hostname", {"Hostnames": ["web.example.com"]}))
        record = load_state()["cdnDomains"]["web.example.com"]
        self.assertEqual((record["service"], record["siteId"], record["lastPurge"]["taskIds"]), ("web", 77, ["9001"]))

        delete = MagicMock(return_value="DNS deleted")
        with patch("luma.control.cdn.AliyunClient", return_value=fake), patch(
            "luma.control.server.delete_dns", delete
        ), patch("luma.control.server.remove_from_nomad", return_value="removed"):
            removed = handle_service_remove(self.state["deployToken"], {"name": "web"})
        self.assertEqual(delete.call_args.kwargs["override"]["type"], "CNAME")
        steps = {step["name"]: step for step in removed["steps"]}
        self.assertEqual(steps["Delete CDN domain"]["message"], "ESA record web.example.com deleted")
        self.assertNotIn("web.example.com", load_state().get("cdnDomains") or {})
        self.assertEqual(fake.records, {})

    def test_cli_deploy_is_blocked_without_esa_keys(self):
        manifest = yaml.safe_dump({"name": "web", "image": "nginx", "region": "cn", "exposure": "cn-edge", "domain": "web.example.com", "port": 80, "cdn": "esa"})
        with self.assertRaisesRegex(LumaError, "cdn: Alibaba Cloud ESA credentials"):
            handle_deployment(self.state["deployToken"], {"manifest": manifest, "sourceName": "web.yaml"})


class CdnRenderTests(unittest.TestCase):
    def test_traefik_access_log_keeps_the_esa_client_address(self):
        from luma.nomad_render import render_traefik_job

        job = render_traefik_job(image="traefik:v3", acme_email="ops@example.com", as_json=False)
        task = (job.get("Job") or job)["TaskGroups"][0]["Tasks"][0]
        self.assertIn("--accesslog.fields.headers.names.Ali-Real-Client-Ip=keep", task["Config"]["args"])


if __name__ == "__main__":
    unittest.main()
