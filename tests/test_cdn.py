import json
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
        self.rules = {"CacheRule": [], "OriginRule": [], "HttpsApplicationConfiguration": []}
        self.next_config = 100
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
        if action == "ListCompressionRules":
            return {"Configs": [{"ConfigId": 1}] if action in self.global_configs else []}
        for kind, configs in self.rules.items():
            if action == f"List{kind}s" or (kind == "HttpsApplicationConfiguration" and action == "ListHttpsApplicationConfigurations"):
                wanted = params.get("ConfigType")
                return {"Configs": [dict(item) for item in configs if not wanted or item["ConfigType"] == wanted]}
            if action == f"Create{kind}":
                self.next_config += 1
                config_type = "rule" if "Rule" in params else "global"
                configs.append({**{k: v for k, v in params.items() if k != "SiteId"}, "ConfigId": self.next_config, "ConfigType": config_type})
                return {"ConfigId": self.next_config}
            if action == f"Update{kind}":
                item = next(c for c in configs if c["ConfigId"] == params["ConfigId"])
                item.update({k: v for k, v in params.items() if k not in {"SiteId", "ConfigId"}})
                return {}
            if action == f"Delete{kind}":
                configs[:] = [c for c in configs if c["ConfigId"] != params["ConfigId"]]
                return {}
        if action == "CreateCompressionRule":
            self.global_configs.add("ListCompressionRules")
            return {"ConfigId": 2}
        if action == "UpdateManagedTransform":
            return {}
        if action == "GetDcvDelegation":
            return {"DelegationDomain": f"{params['SiteId']}.dcv.aliyun-esa.com"}
        if action == "ListRecords":
            record = self.records.get(params["RecordName"])
            return {"Records": [record] if record else []}
        if action in {"CreateRecord", "UpdateRecord"}:
            name = params.get("RecordName") or next(n for n, r in self.records.items() if r["RecordId"] == params["RecordId"])
            self.records[name] = {
                "RecordId": 501, "RecordName": name, "RecordType": "A/AAAA", "Proxied": params["Proxied"],
                "Data": {"Value": params["Data"]["Value"]}, "RecordCname": f"{name}.cnamezone.com",
            }
            return {"RecordId": 501}
        if action == "DeleteRecord":
            self.records = {name: r for name, r in self.records.items() if r["RecordId"] != params["RecordId"]}
            return {}
        if action == "ListCertificatesByRecord":
            assert params.get("Detail") is True, "Certificates are only listed with Detail=true"
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
        self.assertEqual((create["Type"], create["Proxied"], create["BizName"], create["Data"]), ("A/AAAA", True, "web", {"Value": "203.0.113.10"}))
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


class DomainRuleTests(unittest.TestCase):
    def test_only_cache_paths_need_rules(self):
        from luma.cdn import domain_rules

        spec = parse_cdn_block({"provider": "esa", "cache": [{"path": "/blog/*", "ttl": 300}, {"path": "/robots.txt", "ttl": 86400}]})
        rules = domain_rules("www.example.com", spec.cache)
        blog, robots = rules["CacheRule"]
        from luma.cdn import _rule_prefix

        self.assertEqual(blog["RuleName"], _rule_prefix("www.example.com") + "cache1")
        self.assertEqual(blog["Sequence"], "101")
        self.assertNotEqual(_rule_prefix("a-b.example.com"), _rule_prefix("a.b.example.com"))
        self.assertEqual((blog["Rule"], blog["EdgeCacheMode"], blog["EdgeCacheTtl"]), ('(http.host eq "www.example.com" and starts_with(http.request.uri.path, "/blog/"))', "follow_origin_override", "300"))
        self.assertEqual(robots["Rule"], '(http.host eq "www.example.com" and http.request.uri.path eq "/robots.txt")')
        self.assertEqual(rules["OriginRule"], [])
        self.assertEqual(domain_rules("www.example.com", ()), {"CacheRule": [], "OriginRule": []})

    def test_site_defaults_use_global_configs_and_correct_existing_ones(self):
        from luma.cdn import configure_site

        client = FakeEsa()
        client.rules["OriginRule"].append({"ConfigId": 1, "ConfigType": "global", "OriginScheme": "http"})
        applied = configure_site(client, 77)
        self.assertIn("luma_site_origin_headers created", applied)
        self.assertIn("OriginRule global updated", applied)
        self.assertEqual(client.rules["OriginRule"], [{"ConfigId": 1, "ConfigType": "global", "OriginScheme": "https", "OriginVerify": "on"}])
        self.assertEqual(client.rules["HttpsApplicationConfiguration"][0]["HttpsForce"], "on")
        # HTML is only cached through a rule; a global cache config covers static extensions only.
        site_rule = client.rules["CacheRule"][0]
        self.assertEqual(
            (site_rule["ConfigType"], site_rule["Rule"], site_rule["BypassCache"], site_rule["EdgeCacheMode"], site_rule["Sequence"]),
            ("rule", "true", "cache_all", "follow_origin_bypass", "1"),
        )
        self.assertEqual(configure_site(client, 77), ["real client IP header"])

    def test_rule_quota_error_explains_the_shared_limit(self):
        from luma.cdn import CachePath, reconcile_domain_rules

        client = FakeEsa()
        original = client.esa

        def esa(action, **params):
            if action == "CreateCacheRule":
                raise AliyunApiError(action, 400, "ConfExceedLimit", "limit")
            return original(action, **params)

        client.esa = esa
        with self.assertRaisesRegex(LumaError, "shares it .*Basic 10"):
            reconcile_domain_rules(client, site_id=77, domain="www.example.com", cache=[CachePath("/blog/*", 300)])

    def test_reconcile_creates_updates_deletes_only_own_rules(self):
        from luma.cdn import CachePath, _rule_prefix, reconcile_domain_rules

        mine = _rule_prefix("www.example.com")
        other = _rule_prefix("www.example.com.cn") + "cache1"
        client = FakeEsa()
        client.rules["CacheRule"].append({"ConfigId": 1, "ConfigType": "rule", "RuleName": other, "Rule": "x"})
        client.rules["OriginRule"].append({"ConfigId": 2, "ConfigType": "rule", "RuleName": mine + "origin", "Rule": "x"})
        reconcile_domain_rules(client, site_id=77, domain="www.example.com", cache=[CachePath("/blog/*", 300)])
        self.assertEqual(sorted(c["RuleName"] for c in client.rules["CacheRule"]), sorted([other, mine + "cache1"]))
        self.assertEqual(client.rules["OriginRule"], [])
        self.assertEqual(reconcile_domain_rules(client, site_id=77, domain="www.example.com", cache=[CachePath("/blog/*", 300)]), [])
        changes = reconcile_domain_rules(client, site_id=77, domain="www.example.com", cache=[CachePath("/blog/*", 600)])
        self.assertEqual(changes, [mine + "cache1 updated"])
        changes = reconcile_domain_rules(client, site_id=77, domain="www.example.com", cache=[])
        self.assertEqual(changes, [mine + "cache1 deleted"])
        self.assertIn(other, [c["RuleName"] for c in client.rules["CacheRule"]])

    def test_cache_paths_are_validated(self):
        for raw, message in [
            ([{"path": "blog/*", "ttl": 1}], "must start with /"),
            ([{"path": "/a*/b", "ttl": 1}], "may only end with"),
            ([{"path": "/a", "ttl": 0}], "ttl must be seconds"),
            ([{"path": "/a", "ttl": 1, "vary": "x"}], "unsupported cdn.cache"),
            ([{"path": "/blog/*", "ttl": 1}, {"path": "/blog/a", "ttl": 2}], "overlap"),
            ([{"path": f"/p{i}", "ttl": 1} for i in range(9)], "at most 8"),
        ]:
            with self.subTest(raw=raw), self.assertRaisesRegex(LumaError, message):
                parse_cdn_block({"provider": "esa", "cache": raw})


class RetryTests(unittest.TestCase):
    def test_writes_are_not_retried_after_a_network_error(self):
        from luma.cdn import AliyunClient

        client = AliyunClient("id", "secret", sleep=lambda _s: None)
        with patch("luma.cdn.urllib.request.urlopen", side_effect=urllib.error.URLError("timed out")) as urlopen:
            with self.assertRaisesRegex(LumaError, "next deploy reconciles"):
                client.esa("CreateRecord", SiteId=1)
        self.assertEqual(urlopen.call_count, 1)
        with patch("luma.cdn.urllib.request.urlopen", side_effect=urllib.error.URLError("timed out")) as urlopen:
            with self.assertRaisesRegex(LumaError, "unavailable after 4 attempts"):
                client.esa("ListRecords", SiteId=1)
        self.assertEqual(urlopen.call_count, 4)


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
        records = MagicMock(side_effect=lambda config, name, record_type, content, secrets=None: name)
        with patch("luma.control.cdn.AliyunClient", return_value=fake), patch(
            "luma.control.cdn.upsert_txt_record", txt
        ), patch("luma.control.cdn.upsert_record", records), patch("luma.cdn.time.sleep"), patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="ok"), patch(
            "luma.control.server.sync_dns", sync
        ), patch("luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"), patch(
            "luma.control.server.urllib.request.urlopen", side_effect=probe_error
        ):
            first = handle_deployment(self.state["deployToken"], {"manifest": manifest, "sourceName": "web.yaml"})
            second = handle_deployment(self.state["deployToken"], {"manifest": manifest, "sourceName": "web.yaml"})

        names = [step["name"] for step in first["steps"]]
        self.assertLess(names.index("Prepare CDN"), names.index("Sync DNS"))
        self.assertNotIn("ESA certificate", names)
        # The certificate is issued through DCV delegation before DNS moves.
        self.assertEqual(records.call_args_list[0].args[1:4], ("_acme-challenge.web.example.com", "CNAME", "web.example.com.77.dcv.aliyun-esa.com"))
        self.assertIn("certificate issued", next(step["message"] for step in first["steps"] if step["name"] == "Prepare CDN"))
        self.assertLess(names.index("Probe public route"), names.index("Purge CDN"))
        self.assertEqual(txt.call_args.args[1:3], ("_esaauth.example.com", "verify_abc"))
        self.assertEqual(sync.call_args.kwargs["override"], {"target": "web.example.com.cnamezone.com", "type": "CNAME", "proxied": False})
        self.assertIn("no cache yet", first["cdn"]["purge"])
        self.assertIn("whole hostname", second["cdn"]["purge"])
        self.assertEqual(fake.actions().count("CreateSite"), 1)
        self.assertEqual(fake.actions().count("CreateOriginRule"), 1)
        self.assertEqual(fake.actions().count("CreateCacheRule"), 1)
        self.assertEqual([c["ConfigType"] for c in fake.rules["OriginRule"] + fake.rules["CacheRule"]], ["global", "rule"])
        purge = next(params for action, params in fake.calls if action == "PurgeCaches")
        self.assertEqual((purge["SiteId"], purge["Type"], purge["Content"]), (77, "hostname", {"Hostnames": ["web.example.com"]}))
        record = load_state()["cdnDomains"]["web.example.com"]
        self.assertEqual((record["service"], record["siteId"], record["lastPurge"]["taskIds"]), ("web", 77, ["9001"]))

        delete = MagicMock(return_value="DNS deleted")
        delete_record = MagicMock(return_value=1)
        with patch("luma.control.cdn.AliyunClient", return_value=fake), patch(
            "luma.control.server.delete_dns", delete
        ), patch("luma.control.cdn.delete_record", delete_record), patch("luma.control.server.remove_from_nomad", return_value="removed"):
            removed = handle_service_remove(self.state["deployToken"], {"name": "web"})
        self.assertEqual(delete.call_args.kwargs["override"]["type"], "CNAME")
        steps = {step["name"]: step for step in removed["steps"]}
        self.assertEqual(steps["Delete CDN domain"]["message"], "ESA record web.example.com deleted")
        self.assertEqual(delete_record.call_args.args[1:3], ("_acme-challenge.web.example.com", "CNAME"))
        self.assertNotIn("web.example.com", load_state().get("cdnDomains") or {})
        self.assertEqual(fake.records, {})
        self.assertEqual([c["RuleName"] for c in fake.rules["CacheRule"]], ["luma_site_origin_headers"])

    def test_certificate_delay_fails_before_dns_changes(self):
        self._add_secrets()
        manifest = yaml.safe_dump({"name": "web", "image": "nginx:alpine", "region": "cn", "exposure": "cn-edge", "domain": "web.example.com", "port": 80, "cdn": "esa"})
        fake = FakeEsa(cert_after_polls=10**6)
        sync = MagicMock(return_value="DNS updated")
        clock = iter(range(0, 10**7, 100))
        with patch("luma.control.cdn.AliyunClient", return_value=fake), patch(
            "luma.control.cdn.upsert_txt_record", return_value="_esaauth.example.com"
        ), patch("luma.control.cdn.upsert_record", return_value="x"), patch("luma.cdn.time.sleep"), patch(
            "luma.cdn.time.monotonic", side_effect=lambda: next(clock)
        ), patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="ok"), patch(
            "luma.control.server.sync_dns", sync
        ), patch("luma.control.server.deploy_to_nomad") as deploy:
            with self.assertRaisesRegex(LumaError, "still being issued"):
                handle_deployment(self.state["deployToken"], {"manifest": manifest, "sourceName": "web.yaml"})
        sync.assert_not_called()
        deploy.assert_not_called()

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


class CdnCheckTests(unittest.TestCase):
    def _fetcher(self, pages):
        def fetch(url):
            path = url.split("example.com", 1)[1]
            status, headers, body = pages.get(path, (404, {}, b""))
            return status, {k.lower(): v for k, v in headers.items()}, body
        return fetch

    def test_audit_samples_assets_and_classifies(self):
        from luma.cdn_check import audit_domain

        html = b'<html><script src="/app.3f2a.js"></script><link rel="stylesheet" href="/a.css"><img src="https://cdn.other.com/x.png"></html>'
        pages = {
            "/": (200, {"Content-Type": "text/html", "Set-Cookie": "sid=1"}, html),
            "/app.3f2a.js": (200, {"Content-Type": "application/javascript", "Cache-Control": "public, max-age=31536000, immutable", "Content-Encoding": "gzip"}, b"x"),
            "/a.css": (200, {"Content-Type": "text/css"}, b"x" * 2048),
            "/account/me": (200, {"Content-Type": "application/json", "Cache-Control": "public, s-maxage=60"}, b"{}"),
            "/blog/": (200, {"Content-Type": "text/html", "Cache-Control": "public, s-maxage=60", "Age": "12"}, b"<html></html>"),
        }
        rows = audit_domain("www.example.com", purge=None, cache=[{"path": "/blog/*", "ttl": 300}], extra=["/account/me"], fetch=self._fetcher(pages))
        by_path = {row["path"]: row for row in rows}
        self.assertEqual(rows[0]["verdict"], "danger")
        self.assertIn("account-like", by_path["/account/me"]["reason"])
        self.assertEqual(by_path["/"]["verdict"], "improve")
        self.assertIn("cdn.cache", by_path["/"]["reason"])
        self.assertIn("uncompressed", by_path["/a.css"]["reason"])
        self.assertEqual(by_path["/app.3f2a.js"]["verdict"], "ok")
        self.assertEqual(by_path["/blog/"]["reason"], "cacheable (cache hit)")
        self.assertNotIn("/x.png", by_path)
        self.assertEqual(by_path["/sitemap.xml"]["reason"], "HTTP 404")

    def test_cacheable_response_with_cookie_is_dangerous(self):
        from luma.cdn_check import classify

        verdict, reason = classify("/", 200, {"cache-control": "public, s-maxage=60", "set-cookie": "a=b", "content-type": "text/html"}, 10, {})
        self.assertEqual(verdict, "danger")
        self.assertEqual(classify("/", 200, {"cache-control": "private, no-store", "set-cookie": "a=b", "content-type": "text/html"}, 10, {})[0], "ok")


class ApexAndRedirectTests(unittest.TestCase):
    def test_cdn_on_the_zone_apex_is_refused_with_the_www_fix(self):
        from luma.control.cdn import _site_name

        config = LumaConfig(path=None, raw={"providers": {"dns": {"type": "cloudflare", "zone": "example.com"}}})
        with self.assertRaisesRegex(LumaError, "domain: www.example.com with redirectFrom: \\[example.com\\]"):
            _site_name(config, "example.com")
        self.assertEqual(_site_name(config, "www.example.com"), "example.com")

    def test_redirect_from_is_validated(self):
        from luma.service import parse_redirect_from

        self.assertEqual(parse_redirect_from(["Example.com.", "example.com"], domain="www.example.com", exposure="cn-edge"), (["example.com"], []))
        self.assertEqual(
            parse_redirect_from({"hosts": ["example.com"], "except": ["/api/*", "/healthz"]}, domain="www.example.com", exposure="cn-edge"),
            (["example.com"], ["/api/*", "/healthz"]),
        )
        for raw, exposure, message in [
            ("example.com", "cn-edge", "must be a list"),
            ({"hosts": ["example.com"], "except": ["api/*"]}, "cn-edge", "except entries must be URL paths"),
            ({"hosts": ["example.com"], "paths": []}, "cn-edge", "unsupported redirectFrom field"),
            (["www.example.com"], "cn-edge", "must not repeat domain"),
            (["not a host"], "cn-edge", "must be hostnames"),
            (["example.com"], "tcp-relay", "requires exposure"),
        ]:
            with self.subTest(raw=raw), self.assertRaisesRegex(LumaError, message):
                parse_redirect_from(raw, domain="www.example.com", exposure=exposure)

    def test_edge_tags_redirect_get_and_pass_other_methods(self):
        from luma.nomad_render import redirect_router_tags

        config = LumaConfig(path=None, raw={"providers": {"dns": {"type": "cloudflare", "zone": "example.com"}}, "defaults": {"acmeDomains": ["example.com"]}})
        tags = redirect_router_tags(config, "web", "www.example.com", ["example.com"], service="web")
        self.assertIn("traefik.http.routers.web-redirect.rule=(Host(`example.com`)) && (Method(`GET`) || Method(`HEAD`))", tags)
        self.assertIn("traefik.http.routers.web-redirect.middlewares=web-to-primary", tags)
        self.assertIn("traefik.http.routers.web-alias.rule=Host(`example.com`)", tags)
        self.assertIn("traefik.http.routers.web-alias.service=web", tags)
        self.assertNotIn("traefik.http.routers.web-alias.middlewares=web-to-primary", tags)
        self.assertIn("traefik.http.middlewares.web-to-primary.redirectregex.replacement=https://www.example.com$1", tags)
        self.assertIn("traefik.http.middlewares.web-to-primary.redirectregex.permanent=true", tags)
        self.assertIn("traefik.http.routers.web-redirect.tls.domains[0].sans=example.com", tags)
        kept = redirect_router_tags(config, "web", "www.example.com", ["example.com"], service="web", except_paths=["/api/*", "/healthz"])
        self.assertIn(
            "traefik.http.routers.web-redirect.rule=(Host(`example.com`)) && (Method(`GET`) || Method(`HEAD`)) && !(PathPrefix(`/api/`) || Path(`/healthz`))",
            kept,
        )
        # Nomad interpolates ${...}; the replacement must not use it.
        self.assertFalse(any("${" in tag for tag in tags))

    def test_rendered_service_and_relay_route_carry_redirects(self):
        from luma.nomad_render import render_nomad_job
        from luma.render import render_tailscale_route

        config = LumaConfig(path=None, raw={"providers": {"dns": {"type": "cloudflare", "zone": "example.com"}}, "nodes": {"edge": {"host": "edge", "publicIp": "203.0.113.10", "region": "cn", "roles": ["edge"]}}})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "web.yaml"
            path.write_text(yaml.safe_dump({"name": "web", "image": "nginx:alpine", "region": "cn", "exposure": "cn-edge", "domain": "www.example.com", "redirectFrom": ["example.com"], "port": 80}), encoding="utf-8")
            service = load_service(path)
        job = json.loads(render_nomad_job(config, service))
        tags = [tag for group in job["Job"]["TaskGroups"] for svc in group.get("Services") or [] for tag in svc.get("Tags") or []]
        self.assertIn("traefik.http.routers.web.rule=Host(`www.example.com`)", tags)
        self.assertIn("traefik.http.routers.web-redirect.priority=200", tags)
        relay = ServiceSpec(source=Path("p.yaml"), name="panel", image="x", region="home", public=True, exposure="tailscale-relay", domain="www.example.com", port=80, redirect_from=["example.com"], relay={"url": "http://100.64.0.2:80"})
        route = yaml.safe_load(render_tailscale_route(config, relay))
        self.assertEqual(route["http"]["routers"]["panel-alias"]["service"], "panel")
        self.assertEqual(route["http"]["routers"]["panel-redirect"]["middlewares"], ["panel-to-primary"])
        self.assertEqual(route["http"]["middlewares"]["panel-to-primary"]["redirectRegex"]["replacement"], "https://www.example.com$1")

    def test_deploy_points_redirect_hosts_at_the_edge(self):
        from luma.control.server import _sync_redirect_dns_steps

        service = ServiceSpec(source=Path("a.yaml"), name="web", image="nginx", region="cn", public=True, exposure="cn-edge", domain="www.example.com", port=80, cdn=parse_cdn_block("esa"), redirect_from=["example.com"])
        steps = []
        sync = MagicMock(return_value="DNS updated: example.com -> 203.0.113.10")
        with patch("luma.control.server.sync_dns", sync), patch("luma.control.server.mutate_state") as mutate:
            _sync_redirect_dns_steps(steps, None, object(), {}, service, skip=False)
        owners = {}
        mutate.call_args.args[0](owners)
        self.assertEqual(owners["redirectHosts"], {"example.com": "web"})
        alias = sync.call_args.args[1]
        self.assertEqual((alias.domain, alias.cdn, sync.call_args.kwargs), ("example.com", None, {}))
        self.assertEqual(steps[-1]["name"], "Sync DNS example.com (redirect)")


class StreamHeartbeatTests(unittest.TestCase):
    def test_idle_stream_yields_a_heartbeat_instead_of_going_silent(self):
        import asyncio

        from luma.control import server

        async def run():
            queue = asyncio.Queue()
            with patch.object(server, "STREAM_HEARTBEAT_SECONDS", 0.01):
                first = await server._next_stream_event(queue)
                queue.put_nowait({"status": "ok"})
                second = await server._next_stream_event(queue)
            return first, second

        first, second = asyncio.run(run())
        self.assertIs(first, server._STREAM_HEARTBEAT)
        self.assertEqual(second, {"status": "ok"})


class ReviewFixTests(unittest.TestCase):
    def test_hostname_origin_uses_a_cname_record(self):
        client = FakeEsa()
        original = client.esa
        seen = {}

        def esa(action, **params):
            if action == "CreateRecord":
                seen.update(params)
                params = {**params, "Type": "A/AAAA"}
            return original(action, **params)

        client.esa = esa
        try:
            ensure_record(client, site_id=77, domain="app.example.com", origin="edge.example.net")
        except LumaError:
            pass
        self.assertEqual((seen["Type"], seen["SourceType"], seen["HostPolicy"]), ("CNAME", "Domain", "follow_hostname"))

    def test_redirect_host_owned_by_another_service_is_refused(self):
        from luma.control.server import _require_redirect_hosts_free

        service = ServiceSpec(source=Path("a.yaml"), name="web", image="x", region="cn", public=True, exposure="cn-edge", domain="www.example.com", port=80, redirect_from=["example.com"])
        with self.assertRaisesRegex(LumaError, "already redirected by blog"):
            _require_redirect_hosts_free({"redirectHosts": {"example.com": "blog"}}, service)
        state = {"deployments": {"services": {"old": {"name": "old", "manifest": "domain: example.com\n"}}, "compose": {}}}
        with self.assertRaisesRegex(LumaError, "domain of old"):
            _require_redirect_hosts_free(state, service)
        _require_redirect_hosts_free({"redirectHosts": {"example.com": "web"}}, service)

    def test_dropped_redirect_host_is_released(self):
        from luma.control.server import _sync_redirect_dns_steps

        service = ServiceSpec(source=Path("a.yaml"), name="web", image="x", region="cn", public=True, exposure="cn-edge", domain="www.example.com", port=80, redirect_from=[])
        steps = []
        delete = MagicMock(return_value="DNS deleted: example.com")
        with patch("luma.control.server.delete_dns", delete), patch("luma.control.server.mutate_state"):
            _sync_redirect_dns_steps(steps, None, object(), {"redirectHosts": {"example.com": "web"}}, service, skip=False)
        self.assertEqual(delete.call_args.args[1].domain, "example.com")
        self.assertEqual(steps[-1]["name"], "Delete DNS example.com (redirect removed)")

    def test_redirect_dns_failure_is_a_warning(self):
        from luma.control.server import _sync_redirect_dns_steps

        service = ServiceSpec(source=Path("a.yaml"), name="web", image="x", region="cn", public=True, exposure="cn-edge", domain="www.example.com", port=80, redirect_from=["example.com"])
        steps = []
        with patch("luma.control.server.sync_dns", side_effect=LumaError("Cloudflare API error")), patch("luma.control.server.mutate_state") as mutate:
            _sync_redirect_dns_steps(steps, None, object(), {}, service, skip=False)
        self.assertEqual(steps[-1]["status"], "warning")
        mutate.assert_not_called()

    def test_remove_with_skip_dns_keeps_the_esa_record(self):
        from luma.control.server import _remove_cdn_steps

        service = ServiceSpec(source=Path("a.yaml"), name="web", image="x", region="cn", public=True, exposure="cn-edge", domain="www.example.com", port=80, cdn=parse_cdn_block("esa"))
        steps = []
        state = {"cdnDomains": {"www.example.com": {"service": "web", "siteId": 77}}}
        with patch("luma.control.server.remove_cdn_domain") as remove:
            _remove_cdn_steps(steps, None, state, service, dry_run=False, skip_dns=True)
        remove.assert_not_called()
        self.assertEqual(steps[-1]["status"], "warning")

    def test_cdn_probe_goes_to_the_edge_with_the_domain_as_sni(self):
        from luma.control import server

        service = ServiceSpec(source=Path("a.yaml"), name="web", image="x", region="cn", public=True, exposure="cn-edge", domain="www.example.com", port=80, cdn=parse_cdn_block("esa"), dns={"target": "203.0.113.10"})
        response = MagicMock(status=200, headers={"Server": "traefik"})
        response.read.return_value = b"ok"
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch.object(server, "_OriginHTTPSConnection", return_value=connection) as factory, patch.object(server, "load_config"):
            message = server._probe_public_route(service)
        self.assertEqual(factory.call_args.args[:2], ("203.0.113.10", "www.example.com"))
        self.assertEqual(connection.request.call_args.kwargs["headers"]["Host"], "www.example.com")
        self.assertIn("edge 203.0.113.10", message)
