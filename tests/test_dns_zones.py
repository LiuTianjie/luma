"""DNS zone catalog, provider routing and the Alibaba Cloud / DNSPod clients."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from luma.config import LumaConfig
from luma.control import dns_zones
from luma.control.server import _dns_config, handle_dns_zone_add, handle_dns_zone_list, handle_dns_zone_remove
from luma.control.state import init_state, load_state, mutate_state
from luma.dependencies import service_requirements
from luma.dns import cert_resolver_for, delete_dns, sync_dns, zone_for
from luma.dns_providers import AlidnsProvider, DnsRecord, DnspodProvider, TencentApiError, TencentClient, relative_name, tc3_authorization
from luma.errors import LumaError
from luma.render import render_tailscale_route
from luma.service import ServiceSpec

ZONES = [
    {"name": "acme.cn", "provider": "alidns", "credentials": {"accessKeyId": "LUMA_DNS_ACME_CN_ACCESS_KEY_ID", "accessKeySecret": "LUMA_DNS_ACME_CN_ACCESS_KEY_SECRET"}},
    {"name": "shop.acme.cn", "provider": "dnspod", "credentials": {"secretId": "TC_ID", "secretKey": "TC_KEY"}},
    {"name": "other.dev", "provider": "cloudflare", "zoneId": "z-other", "credentials": {"apiToken": "LUMA_DNS_OTHER_DEV_API_TOKEN"}},
]


def _config(zones=None):
    dns = {"provider": "cloudflare", "zone": "acme.dev", "zoneId": "z-primary", "edgeTarget": "47.100.1.1"}
    if zones is not None:
        dns["zones"] = zones
    return LumaConfig({"providers": {"dns": dns}}, None)


def _service(domain: str, exposure: str = "cn-edge", **extra) -> ServiceSpec:
    return ServiceSpec(source=Path("a.yaml"), name="web", image="nginx", region="cn", public=True, exposure=exposure, domain=domain, port=80, **extra)


class ZoneRoutingTests(unittest.TestCase):
    def test_longest_suffix_wins_and_primary_is_the_fallback(self):
        config = _config(ZONES)
        self.assertEqual(zone_for(config, "api.acme.cn").provider, "alidns")
        self.assertEqual(zone_for(config, "a.shop.acme.cn").provider, "dnspod")
        self.assertEqual(zone_for(config, "www.other.dev").zone_id, "z-other")
        primary = zone_for(config, "app.acme.dev")
        self.assertTrue(primary.primary)
        # Names outside every zone keep the old single-zone behaviour.
        self.assertTrue(zone_for(config, "unrelated.net").primary)

    def test_no_dns_configured_reports_skip(self):
        message = sync_dns(LumaConfig({}, None), _service("api.acme.cn"))
        self.assertEqual(message, "DNS skipped: dns.provider is not cloudflare")

    def test_cloudflare_catalog_zone_uses_its_own_zone_id_and_token(self):
        client = MagicMock()
        client.request.side_effect = lambda method, path, body=None: {"result": []} if method == "GET" else {"result": {}}
        with patch.dict(os.environ, {"CLOUDFLARE_ZONE_ID": "z-primary"}), patch("luma.cloudflare.CloudflareClient", return_value=client) as factory:
            message = sync_dns(_config(ZONES), _service("www.other.dev"), secrets={"LUMA_DNS_OTHER_DEV_API_TOKEN": "other-token"})
        self.assertEqual(message, "DNS created: www.other.dev -> 47.100.1.1")
        factory.assert_called_once_with("other-token")
        post = next(call.args for call in client.request.call_args_list if call.args[0] == "POST")
        self.assertEqual(post[1], "/zones/z-other/dns_records")

    def test_alidns_zone_creates_and_replaces_conflicting_records(self):
        provider = MagicMock()
        provider.records.return_value = [DnsRecord(id="c1", name="api.acme.cn", type="CNAME", content="x.cdn.com", ttl=600)]
        with patch("luma.dns.record_provider", return_value=provider) as factory:
            message = sync_dns(_config(ZONES), _service("api.acme.cn"), secrets={"LUMA_DNS_ACME_CN_ACCESS_KEY_ID": "id", "LUMA_DNS_ACME_CN_ACCESS_KEY_SECRET": "sk"})
        self.assertEqual(message, "DNS updated: api.acme.cn -> 47.100.1.1")
        factory.assert_called_once_with("alidns", "acme.cn", {"accessKeyId": "id", "accessKeySecret": "sk"})
        provider.update.assert_called_once()
        self.assertEqual(provider.update.call_args.args[1:4], ("api.acme.cn", "A", "47.100.1.1"))
        self.assertEqual(provider.update.call_args.args[4], 600)

    def test_proxied_is_rejected_outside_cloudflare(self):
        with self.assertRaisesRegex(LumaError, "Cloudflare feature"):
            sync_dns(_config(ZONES), _service("api.acme.cn", dns={"proxied": True}), secrets={"LUMA_DNS_ACME_CN_ACCESS_KEY_ID": "id", "LUMA_DNS_ACME_CN_ACCESS_KEY_SECRET": "sk"})

    def test_delete_removes_every_matching_record(self):
        provider = MagicMock()
        provider.records.return_value = [DnsRecord(id="1", name="api.acme.cn", type="A", content="47.100.1.1")]
        with patch("luma.dns.record_provider", return_value=provider):
            self.assertEqual(delete_dns(_config(ZONES), _service("api.acme.cn"), secrets={"LUMA_DNS_ACME_CN_ACCESS_KEY_ID": "id", "LUMA_DNS_ACME_CN_ACCESS_KEY_SECRET": "sk"}), "DNS deleted: api.acme.cn")
        provider.delete.assert_called_once()

    def test_missing_credentials_are_named(self):
        with self.assertRaisesRegex(LumaError, "accessKeyId"):
            sync_dns(_config(ZONES), _service("api.acme.cn"), secrets={})

    def test_cert_resolver_follows_the_zone(self):
        config = LumaConfig({"providers": {"dns": {"provider": "cloudflare", "zone": "acme.dev", "zoneId": "z", "zones": ZONES}}}, None)
        self.assertEqual(cert_resolver_for(config, "app.acme.dev"), "letsencrypt")
        self.assertEqual(cert_resolver_for(config, "www.other.dev"), "letsencrypt")
        self.assertEqual(cert_resolver_for(config, "api.acme.cn"), "letsencrypt-http")
        self.assertEqual(cert_resolver_for(config, "app.acme.dev", node_edge=True), "letsencrypt-http")
        # A cluster already on HTTP-01 has a single resolver.
        plain = LumaConfig({"defaults": {"acmeDnsProvider": ""}, "providers": {"dns": {"provider": "cloudflare", "zone": "acme.dev", "zones": ZONES}}}, None)
        self.assertEqual(cert_resolver_for(plain, "api.acme.cn"), "letsencrypt")

    def test_relay_route_uses_http01_for_other_providers(self):
        config = LumaConfig({"providers": {"dns": {"provider": "cloudflare", "zone": "acme.dev", "zoneId": "z", "zones": ZONES}}}, None)
        service = ServiceSpec(source=Path("a.yaml"), name="panel", image="x", region="home", public=True, exposure="tailscale-relay", domain="panel.acme.cn", port=8080, node="mac", relay={"host": "100.64.0.2"})
        self.assertIn("certResolver: letsencrypt-http", render_tailscale_route(config, service))

    def test_requirements_need_zone_credentials_not_cloudflare(self):
        service = _service("api.acme.cn")
        checks = {item["kind"]: item for item in service_requirements(service, {"nodes": {}, "secrets": {}}, _config(ZONES))["checks"]}
        self.assertFalse(checks["cloudflare"]["required"])
        self.assertEqual(checks["dns-zone"]["status"], "missing")
        self.assertIn("LUMA_DNS_ACME_CN_ACCESS_KEY_ID", checks["dns-zone"]["missing"])


class AlidnsProviderTests(unittest.TestCase):
    def test_records_filter_exact_host_and_map_apex(self):
        client = MagicMock()
        client.call.return_value = {"DomainRecords": {"Record": [
            {"RecordId": "1", "RR": "api", "Type": "A", "Value": "47.100.1.1", "TTL": 600},
            {"RecordId": "2", "RR": "x.api", "Type": "A", "Value": "47.100.1.2", "TTL": 600},
        ]}}
        provider = AlidnsProvider("acme.cn", "id", "sk", client=client)
        records = provider.records("api.acme.cn", "A")
        self.assertEqual([record.id for record in records], ["1"])
        self.assertEqual(client.call.call_args.args[:3], ("alidns.aliyuncs.com", "2015-01-09", "DescribeSubDomainRecords"))
        self.assertEqual(relative_name("acme.cn", "acme.cn"), "@")

    def test_update_skips_identical_record(self):
        client = MagicMock()
        provider = AlidnsProvider("acme.cn", "id", "sk", client=client)
        provider.update(DnsRecord(id="1", name="api.acme.cn", type="A", content="47.100.1.1", ttl=600), "api.acme.cn", "A", "47.100.1.1", 600)
        client.call.assert_not_called()
        provider.update(DnsRecord(id="1", name="api.acme.cn", type="A", content="47.100.1.1", ttl=600), "api.acme.cn", "A", "47.100.1.9", 600)
        self.assertEqual(client.call.call_args.args[3], {"RecordId": "1", "RR": "api", "Type": "A", "Value": "47.100.1.9", "TTL": 600})


class DnspodProviderTests(unittest.TestCase):
    def test_empty_result_error_means_no_records(self):
        client = MagicMock()
        client.call.side_effect = TencentApiError("DescribeRecordList", "ResourceNotFound.NoDataOfRecord", "none")
        self.assertEqual(DnspodProvider("acme.cn", "id", "key", client=client).records("api.acme.cn"), [])

    def test_create_uses_default_line_and_minimum_ttl(self):
        client = MagicMock()
        DnspodProvider("acme.cn", "id", "key", client=client).create("api.acme.cn", "A", "47.100.1.1", 1)
        action, body = client.call.call_args.args
        self.assertEqual(action, "CreateRecord")
        self.assertEqual(body, {"Domain": "acme.cn", "SubDomain": "api", "RecordType": "A", "RecordLine": "默认", "Value": "47.100.1.1", "TTL": 600})

    def test_tc3_signature_is_deterministic_and_scoped(self):
        header = tc3_authorization(secret_id="AKID", secret_key="key", service="dnspod", host="dnspod.tencentcloudapi.com", action="DescribeDomain", payload=b"{}", timestamp=1700000000)
        self.assertTrue(header.startswith("TC3-HMAC-SHA256 Credential=AKID/2023-11-14/dnspod/tc3_request, SignedHeaders=content-type;host;x-tc-action, Signature="))
        again = tc3_authorization(secret_id="AKID", secret_key="key", service="dnspod", host="dnspod.tencentcloudapi.com", action="DescribeDomain", payload=b"{}", timestamp=1700000000)
        self.assertEqual(header, again)
        other = tc3_authorization(secret_id="AKID", secret_key="key", service="dnspod", host="dnspod.tencentcloudapi.com", action="DescribeDomain", payload=b'{"a":1}', timestamp=1700000000)
        self.assertNotEqual(header, other)

    def test_client_raises_api_errors_and_retries_throttling(self):
        responses = [
            {"Response": {"Error": {"Code": "RequestLimitExceeded", "Message": "slow down"}, "RequestId": "r1"}},
            {"Response": {"RecordList": [], "RequestId": "r2"}},
        ]

        def urlopen(request, timeout):
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(responses.pop(0)).encode()
            return response

        client = TencentClient("id", "key", sleep=lambda _s: None, clock=lambda: 1700000000)
        with patch("luma.dns_providers.urllib.request.urlopen", side_effect=urlopen):
            self.assertEqual(client.call("DescribeRecordList", {"Domain": "acme.cn"})["RecordList"], [])
        failing = [{"Response": {"Error": {"Code": "AuthFailure.SignatureFailure", "Message": "bad"}}}]

        def fail(request, timeout):
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(failing[0]).encode()
            return response

        with patch("luma.dns_providers.urllib.request.urlopen", side_effect=fail), self.assertRaisesRegex(LumaError, "AuthFailure"):
            client.call("DescribeDomain", {"Domain": "acme.cn"})


class ZoneCatalogHandlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        config = Path(self.tmp.name) / "cluster.yaml"
        config.write_text("providers:\n  dns:\n    provider: cloudflare\n    zone: acme.dev\n    zoneId: z-primary\n", encoding="utf-8")
        self.env = patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": self.tmp.name, "LUMA_CONTROL_CONFIG": str(config)})
        self.env.start()
        self.token = init_state(domain="luma.acme.dev", cluster_id="test", overwrite=True)["deployToken"]
        self.verify = patch("luma.control.dns_zones.verify_zone", return_value={"detail": "reachable", "zoneId": ""})
        self.verified = self.verify.start()

    def tearDown(self):
        self.verify.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_add_stores_credentials_as_secrets_and_never_returns_them(self):
        result = handle_dns_zone_add(self.token, {"name": "Acme.CN.", "provider": "aliyun", "credentials": {"accessKeyId": "id-1", "accessKeySecret": "sk-1"}})
        self.assertEqual(result["zone"]["name"], "acme.cn")
        self.assertNotIn("sk-1", json.dumps(result))
        state = load_state()
        self.assertEqual(state["secrets"]["LUMA_DNS_ACME_CN_ACCESS_KEY_SECRET"], "sk-1")
        self.assertEqual(state["dnsZones"]["acme.cn"]["credentials"]["accessKeyId"], "LUMA_DNS_ACME_CN_ACCESS_KEY_ID")
        self.verified.assert_called_once()
        listing = handle_dns_zone_list(self.token)
        self.assertEqual([zone["name"] for zone in listing["zones"]], ["acme.dev", "acme.cn"])
        self.assertTrue(listing["zones"][0]["primary"])
        self.assertNotIn("sk-1", json.dumps(listing))
        # Control injects the catalog into every DNS call.
        self.assertEqual(zone_for(_dns_config(LumaConfig({"providers": {"dns": {"provider": "cloudflare", "zone": "acme.dev"}}}, None), load_state()), "x.acme.cn").provider, "alidns")

    def test_failed_verification_saves_nothing(self):
        self.verified.side_effect = LumaError("InvalidAccessKeyId")
        with self.assertRaisesRegex(LumaError, "InvalidAccessKeyId"):
            handle_dns_zone_add(self.token, {"name": "acme.cn", "provider": "alidns", "credentials": {"accessKeyId": "id", "accessKeySecret": "sk"}})
        state = load_state()
        self.assertNotIn("acme.cn", state.get("dnsZones") or {})
        self.assertNotIn("LUMA_DNS_ACME_CN_ACCESS_KEY_SECRET", state.get("secrets") or {})

    def test_same_as_shares_credentials_and_removal_keeps_shared_secrets(self):
        handle_dns_zone_add(self.token, {"name": "acme.cn", "provider": "alidns", "credentials": {"accessKeyId": "id", "accessKeySecret": "sk"}})
        handle_dns_zone_add(self.token, {"name": "acme.net.cn", "provider": "alidns", "sameAs": "acme.cn"})
        self.assertEqual(load_state()["dnsZones"]["acme.net.cn"]["credentials"]["accessKeySecret"], "LUMA_DNS_ACME_CN_ACCESS_KEY_SECRET")
        handle_dns_zone_remove(self.token, {"name": "acme.cn"})
        self.assertIn("LUMA_DNS_ACME_CN_ACCESS_KEY_SECRET", load_state()["secrets"])
        handle_dns_zone_remove(self.token, {"name": "acme.net.cn"})
        self.assertNotIn("LUMA_DNS_ACME_CN_ACCESS_KEY_SECRET", load_state()["secrets"])

    def test_same_as_must_match_provider(self):
        handle_dns_zone_add(self.token, {"name": "acme.cn", "provider": "alidns", "credentials": {"accessKeyId": "id", "accessKeySecret": "sk"}})
        with self.assertRaisesRegex(LumaError, "not Tencent"):
            handle_dns_zone_add(self.token, {"name": "acme.net", "provider": "dnspod", "sameAs": "acme.cn"})

    def test_update_without_new_keys_keeps_the_stored_ones(self):
        handle_dns_zone_add(self.token, {"name": "acme.cn", "provider": "alidns", "credentials": {"accessKeyId": "id", "accessKeySecret": "sk"}})
        with self.assertRaisesRegex(LumaError, "already exists"):
            handle_dns_zone_add(self.token, {"name": "acme.cn", "provider": "alidns", "credentials": {"accessKeyId": "id", "accessKeySecret": "sk"}})
        handle_dns_zone_add(self.token, {"name": "acme.cn", "provider": "alidns", "update": True, "credentials": {"accessKeySecret": "sk-2"}})
        self.assertEqual(load_state()["secrets"]["LUMA_DNS_ACME_CN_ACCESS_KEY_SECRET"], "sk-2")
        self.assertEqual(load_state()["secrets"]["LUMA_DNS_ACME_CN_ACCESS_KEY_ID"], "id")

    def test_primary_zone_and_bad_names_are_rejected(self):
        with self.assertRaisesRegex(LumaError, "primary zone"):
            handle_dns_zone_add(self.token, {"name": "acme.dev", "provider": "cloudflare", "credentials": {"apiToken": "t"}})
        for name in ("https://acme.cn", "acme", "-a.cn"):
            with self.subTest(name=name), self.assertRaisesRegex(LumaError, "registered domain"):
                handle_dns_zone_add(self.token, {"name": name, "provider": "alidns", "credentials": {"accessKeyId": "i", "accessKeySecret": "s"}})

    def test_remove_refuses_zones_in_use(self):
        handle_dns_zone_add(self.token, {"name": "acme.cn", "provider": "alidns", "credentials": {"accessKeyId": "id", "accessKeySecret": "sk"}})

        def deploy(current):
            current.setdefault("deployments", {}).setdefault("services", {})["api"] = {"name": "api", "manifest": "name: api\ndomain: api.acme.cn\n"}

        mutate_state(deploy)
        with self.assertRaisesRegex(LumaError, "used by api"):
            handle_dns_zone_remove(self.token, {"name": "acme.cn"})
        listing = handle_dns_zone_list(self.token)
        self.assertEqual(next(zone for zone in listing["zones"] if zone["name"] == "acme.cn")["services"], ["api"])

    def test_wrong_token_is_rejected(self):
        with self.assertRaises(LumaError):
            handle_dns_zone_list("nope")

    def test_secret_name_shape(self):
        self.assertEqual(dns_zones.credential_secret_name("shop-1.acme.cn", "secretKey"), "LUMA_DNS_SHOP_1_ACME_CN_SECRET_KEY")


if __name__ == "__main__":
    unittest.main()
