import base64
import datetime
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
    EdgeCertificate,
    acs3_signature,
    load_edge_certificate,
    parse_cdn_block,
    prepare_aliyun_domain,
    purge_targets,
)
from luma.cloudflare import sync_dns
from luma.config import LumaConfig
from luma.control.server import _purge_cdn_step, handle_deployment, handle_service_remove
from luma.control.state import init_state, load_state, mutate_state
from luma.errors import LumaError
from luma.service import ServiceSpec, load_service
from tests.support import _restore_env, _set_env


CERT = EdgeCertificate(
    cert_pem="-----BEGIN CERTIFICATE-----\nnew\n-----END CERTIFICATE-----\n",
    key_pem="-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----\n",
    sha256="a" * 64,
    not_after=4102444800,
    names=("*.example.com", "example.com"),
)


class FakeAliyun:
    """In-memory Alibaba Cloud CDN/CAS account."""

    def __init__(self, *, verified=True, domains=None, uploads=None):
        self.verified = verified
        self.domains = dict(domains or {})
        self.uploads = list(uploads or [])
        self.configs = {}
        self.calls = []

    def cdn(self, action, **params):
        self.calls.append((action, params))
        domain = params.get("DomainName")
        if action == "DescribeUserDomains":
            if domain is None:
                return {"TotalCount": len(self.domains)}
            item = self.domains.get(domain)
            return {"Domains": {"PageData": [item] if item else []}}
        if action == "AddCdnDomain":
            if not self.verified:
                raise AliyunApiError(action, 400, "DomainOwnerVerifyFail", "Owner verification of the root domain failed.")
            self.domains[domain] = {"DomainName": domain, "DomainStatus": "online", "Cname": f"{domain}.w.kunlunsl.com", "Coverage": params["Scope"]}
            return {}
        if action == "DescribeVerifyContent":
            return {"Content": "verify_123"}
        if action == "VerifyDomainOwner":
            self.verified = True
            return {}
        if action == "DescribeCdnDomainConfigs":
            return {"DomainConfigs": {"DomainConfig": [{"FunctionName": name} for name in self.configs.get(domain, [])]}}
        if action == "BatchSetCdnDomainConfig":
            for function in json.loads(params["Functions"]):
                self.configs.setdefault(params["DomainNames"], []).append(function["functionName"])
            return {}
        if action in {"SetCdnDomainSSLCertificate", "DeleteCdnDomain"}:
            if action == "DeleteCdnDomain":
                self.domains.pop(domain, None)
            return {}
        if action == "RefreshObjectCaches":
            return {"RefreshTaskId": "7001"}
        raise AssertionError(f"unexpected CDN action {action}")

    def cas(self, action, **params):
        self.calls.append((action, params))
        if action == "ListUserCertificateOrder":
            return {"CertificateOrderList": list(self.uploads)}
        if action == "UploadUserCertificate":
            cert_id = 900 + len(self.uploads)
            self.uploads.append({"CertificateId": cert_id, "Name": params["Name"]})
            return {"CertId": cert_id}
        raise AssertionError(f"unexpected CAS action {action}")

    def actions(self):
        return [action for action, _params in self.calls]


class CdnManifestTests(unittest.TestCase):
    def test_short_and_full_forms(self):
        self.assertEqual(parse_cdn_block("aliyun").purge, ("/",))
        spec = parse_cdn_block({"provider": "aliyun", "scope": "overseas", "purge": ["/", "/blog/*", "/"]})
        self.assertEqual((spec.scope, spec.purge), ("overseas", ("/", "/blog/*")))
        self.assertIsNone(parse_cdn_block(None))
        self.assertIsNone(parse_cdn_block(False))

    def test_invalid_blocks_are_rejected(self):
        for raw, message in [
            ("cloudflare", "provider must be one of"),
            ({"provider": "aliyun", "scope": "mars"}, "scope must be one of"),
            ({"provider": "aliyun", "purge": "/"}, "purge must be a list"),
            ({"provider": "aliyun", "purge": ["index.html"]}, "must be URL paths"),
            ({"provider": "aliyun", "purge": ["/a*b"]}, "trailing /\\* directory"),
            ({"provider": "aliyun", "ttl": 1}, "unsupported cdn field"),
        ]:
            with self.subTest(raw=raw), self.assertRaisesRegex(LumaError, message):
                parse_cdn_block(raw)

    def test_purge_targets_split_files_and_directories(self):
        self.assertEqual(
            purge_targets("a.example.com", ["/", "/sitemap.xml", "/*", "/blog/*"]),
            {
                "File": ["https://a.example.com/", "https://a.example.com/sitemap.xml"],
                "Directory": ["https://a.example.com/", "https://a.example.com/blog/"],
            },
        )

    def _load(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.yaml"
            path.write_text(yaml.safe_dump(data), encoding="utf-8")
            return load_service(path)

    def test_service_manifest_accepts_cdn_on_traefik_exposures_only(self):
        base = {"name": "web", "image": "nginx", "region": "cn", "domain": "web.example.com", "port": 80, "cdn": "aliyun"}
        self.assertEqual(self._load({**base, "exposure": "cn-edge"}).cdn.provider, "aliyun")
        with self.assertRaisesRegex(LumaError, "cdn requires exposure"):
            self._load({**base, "exposure": "tcp-relay"})

    def test_compose_sidecar_cdn(self):
        from luma.compose import _load_compose_services

        services = _load_compose_services({"web": {"exposure": "cn-edge", "domain": "w.example.com", "port": 80, "cdn": {"provider": "aliyun", "purge": ["/"]}}})
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


class PrepareDomainTests(unittest.TestCase):
    def prepare(self, client, known=None, published=None):
        return prepare_aliyun_domain(
            client,
            domain="app.example.com",
            scope="global",
            origin="203.0.113.10",
            certificate=CERT,
            known=known or {},
            publish_txt=lambda host, content: (published.append((host, content)) if published is not None else None) or f"{host}.example.com",
            sleep=lambda _seconds: None,
        )

    def test_first_time_verifies_ownership_configures_and_installs_certificate(self):
        client = FakeAliyun(verified=False)
        published = []
        record = self.prepare(client, published=published)
        self.assertEqual(published, [("verification", "verify_123")])
        self.assertTrue(record["created"])
        self.assertEqual(record["cname"], "app.example.com.w.kunlunsl.com")
        self.assertEqual(client.actions().count("AddCdnDomain"), 2)
        add = next(params for action, params in client.calls if action == "AddCdnDomain")
        self.assertEqual(json.loads(add["Sources"])[0], {"content": "203.0.113.10", "type": "ipaddr", "port": 443, "priority": "20", "weight": "10"})
        self.assertEqual(
            client.configs["app.example.com"],
            ["set_req_host_header", "forward_scheme", "https_origin_sni", "https_force"],
        )
        actions = client.actions()
        self.assertLess(actions.index("SetCdnDomainSSLCertificate"), actions.index("BatchSetCdnDomainConfig"))
        self.assertEqual(actions.count("BatchSetCdnDomainConfig"), 1)
        self.assertIn("UploadUserCertificate", actions)
        cert_call = next(params for action, params in client.calls if action == "SetCdnDomainSSLCertificate")
        self.assertEqual((cert_call["CertType"], cert_call["CertId"]), ("cas", 900))

    def test_existing_domain_with_same_certificate_makes_no_changes(self):
        client = FakeAliyun(domains={"app.example.com": {"DomainName": "app.example.com", "DomainStatus": "online", "Cname": "x.w.kunlunsl.com", "Coverage": "global"}})
        record = self.prepare(client, known={"configured": True, "certSha256": CERT.sha256, "certId": 5})
        self.assertFalse(record["created"])
        self.assertEqual(record["notes"], [])
        self.assertEqual(client.actions(), ["DescribeUserDomains", "DescribeUserDomains"])

    def test_renewed_certificate_reuses_an_already_uploaded_copy(self):
        name = f"luma-{CERT.sha256[:20]}"
        client = FakeAliyun(
            domains={"app.example.com": {"DomainName": "app.example.com", "DomainStatus": "online", "Cname": "x", "Coverage": "global"}},
            uploads=[{"CertificateId": 42, "Name": name}],
        )
        record = self.prepare(client, known={"configured": True, "certSha256": "old", "certId": 5})
        self.assertEqual(record["certId"], 42)
        self.assertNotIn("UploadUserCertificate", client.actions())

    def test_failed_domain_status_is_reported(self):
        client = FakeAliyun(domains={"app.example.com": {"DomainName": "app.example.com", "DomainStatus": "check_failed", "Cname": "x"}})
        with self.assertRaisesRegex(LumaError, "is check_failed"):
            self.prepare(client)


class EdgeCertificateTests(unittest.TestCase):
    def _acme(self, root: Path, names):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=60)).sign(key, hashes.SHA256())
        )
        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        data = {"letsencrypt": {"Certificates": [{
            "domain": {"main": names[0], "sans": list(names[1:])},
            "certificate": base64.b64encode(cert_pem).decode(),
            "key": base64.b64encode(key_pem).decode(),
        }]}}
        path = root / "acme.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path, cert

    def test_wildcard_covers_one_label_and_apex(self):
        from cryptography.hazmat.primitives import hashes

        with tempfile.TemporaryDirectory() as tmp:
            path, cert = self._acme(Path(tmp), ["*.example.com", "example.com"])
            found = load_edge_certificate("app.example.com", resolver="letsencrypt", path=path)
            self.assertEqual(found.sha256, cert.fingerprint(hashes.SHA256()).hex())
            self.assertIn("BEGIN PRIVATE KEY", found.key_pem)
            load_edge_certificate("example.com", resolver="letsencrypt", path=path)
            with self.assertRaisesRegex(LumaError, "no Traefik certificate covers"):
                load_edge_certificate("a.b.example.com", resolver="letsencrypt", path=path)

    def test_missing_store_explains_the_mount(self):
        with self.assertRaisesRegex(LumaError, "luma update"):
            load_edge_certificate("app.example.com", resolver="letsencrypt", path=Path("/nonexistent/acme.json"))


class CdnDnsTests(unittest.TestCase):
    def _service(self):
        return ServiceSpec(source=Path("a.yaml"), name="web", image="nginx", region="cn", public=True, exposure="cn-edge", domain="web.example.com", port=80)

    def _config(self):
        return LumaConfig(path=None, raw={"providers": {"dns": {"type": "cloudflare", "zone": "example.com", "zoneId": "z1", "edgeTarget": "203.0.113.10"}}})

    def test_switching_to_cdn_replaces_the_a_record_in_place(self):
        client = MagicMock()
        client.request.side_effect = lambda method, path, body=None: (
            {"result": [{"id": "r1", "type": "A", "content": "203.0.113.10"}, {"id": "t1", "type": "TXT", "content": "x"}]} if method == "GET" else {"result": {}}
        )
        with patch("luma.cloudflare.CloudflareClient", return_value=client):
            message = sync_dns(self._config(), self._service(), secrets={"CLOUDFLARE_API_TOKEN": "t"}, override={"target": "web.example.com.w.kunlunsl.com", "type": "CNAME", "proxied": False})
        self.assertEqual(message, "DNS updated: web.example.com -> web.example.com.w.kunlunsl.com")
        writes = [call.args for call in client.request.call_args_list if call.args[0] != "GET"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][:2], ("PUT", "/zones/z1/dns_records/r1"))
        self.assertEqual((writes[0][2]["type"], writes[0][2]["proxied"]), ("CNAME", False))

    def test_disabling_cdn_turns_the_cname_back_into_an_a_record(self):
        client = MagicMock()
        client.request.side_effect = lambda method, path, body=None: (
            {"result": [{"id": "c1", "type": "CNAME", "content": "x.w.kunlunsl.com"}]} if method == "GET" else {"result": {}}
        )
        with patch("luma.cloudflare.CloudflareClient", return_value=client):
            sync_dns(self._config(), self._service(), secrets={"CLOUDFLARE_API_TOKEN": "t"})
        put = next(call.args for call in client.request.call_args_list if call.args[0] == "PUT")
        self.assertEqual((put[1], put[2]["type"], put[2]["content"]), ("/zones/z1/dns_records/c1", "A", "203.0.113.10"))


class PurgeStepTests(unittest.TestCase):
    def _service(self):
        return ServiceSpec(source=Path("a.yaml"), name="web", image="nginx", region="cn", public=True, exposure="cn-edge", domain="web.example.com", port=80, cdn=parse_cdn_block("aliyun"))

    def test_provider_failure_is_a_warning(self):
        steps = []
        with patch("luma.control.server.purge_service_cdn", side_effect=LumaError("QuotaExceeded.Refresh")):
            message = _purge_cdn_step(steps, None, {}, self._service(), prepared={}, skip_orchestrator=False)
        self.assertEqual(steps[-1]["status"], "warning")
        self.assertIn("deployment kept", message)

    def test_unexpected_errors_propagate_unwrapped(self):
        steps = []
        with patch("luma.control.server.purge_service_cdn", side_effect=KeyError("boom")):
            with self.assertRaises(KeyError):
                _purge_cdn_step(steps, None, {}, self._service(), prepared={}, skip_orchestrator=False)
        self.assertEqual(steps[-1]["status"], "fail")

    def test_new_domain_is_not_purged(self):
        steps = []
        with patch("luma.control.server.purge_service_cdn") as purge:
            _purge_cdn_step(steps, None, {}, self._service(), prepared={"created": True}, skip_orchestrator=False)
        purge.assert_not_called()
        self.assertIn("no cache yet", steps[-1]["message"])


class CdnDeploymentFlowTests(unittest.TestCase):
    def test_deploy_prepares_points_dns_purges_and_remove_deletes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            # handle_deployment copies global secrets into os.environ; restore them.
            secret_names = ("CLOUDFLARE_API_TOKEN", "ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET")
            old_secrets = {name: os.environ.get(name) for name in secret_names}
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)

                def add_secrets(current):
                    current.setdefault("secrets", {}).update({
                        "CLOUDFLARE_API_TOKEN": "cf",
                        "ALIBABA_CLOUD_ACCESS_KEY_ID": "LTAIexample",
                        "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "secret",
                    })

                mutate_state(add_secrets)
                (root / "luma.yaml").write_text(yaml.safe_dump({
                    "providers": {"dns": {"type": "cloudflare", "zone": "example.com", "zoneId": "z1"}},
                    "defaults": {"stackRoot": str(root / "stacks")},
                    "nodes": {"edge": {"host": "edge", "publicIp": "203.0.113.10", "region": "cn", "roles": ["edge"]}},
                }), encoding="utf-8")
                manifest = yaml.safe_dump({
                    "name": "web", "image": "nginx:alpine", "region": "cn", "exposure": "cn-edge",
                    "domain": "web.example.com", "port": 80, "cdn": {"provider": "aliyun", "purge": ["/", "/sitemap.xml"]},
                })
                fake = FakeAliyun()
                sync = MagicMock(return_value="DNS updated")
                probe_error = urllib.error.HTTPError("https://web.example.com/", 404, "not found", {}, None)
                with patch("luma.control.cdn.AliyunClient", return_value=fake), patch(
                    "luma.control.cdn.load_edge_certificate", return_value=CERT
                ), patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="ok"), patch(
                    "luma.control.server.sync_dns", sync
                ), patch("luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"), patch(
                    "luma.control.server.urllib.request.urlopen", side_effect=probe_error
                ):
                    first = handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "web.yaml"})
                    second = handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "web.yaml"})

                names = [step["name"] for step in first["steps"]]
                self.assertLess(names.index("Prepare CDN"), names.index("Sync DNS"))
                self.assertLess(names.index("Probe public route"), names.index("Purge CDN"))
                self.assertEqual(sync.call_args.kwargs["override"], {"target": "web.example.com.w.kunlunsl.com", "type": "CNAME", "proxied": False})
                self.assertIn("no cache yet", first["cdn"]["purge"])
                self.assertIn("/, /sitemap.xml", second["cdn"]["purge"])
                refresh = next(params for action, params in fake.calls if action == "RefreshObjectCaches")
                self.assertEqual(refresh["ObjectPath"], "https://web.example.com/\nhttps://web.example.com/sitemap.xml")
                record = load_state()["cdnDomains"]["web.example.com"]
                self.assertEqual((record["service"], record["certSha256"]), ("web", CERT.sha256))
                self.assertEqual(record["lastPurge"]["taskIds"], ["7001"])

                delete = MagicMock(return_value="DNS deleted")
                with patch("luma.control.cdn.AliyunClient", return_value=fake), patch(
                    "luma.control.server.delete_dns", delete
                ), patch("luma.control.server.remove_from_nomad", return_value="removed"):
                    removed = handle_service_remove(state["deployToken"], {"name": "web"})
                self.assertEqual(delete.call_args.kwargs["override"]["type"], "CNAME")
                steps = {step["name"]: step for step in removed["steps"]}
                self.assertEqual(steps["Delete CDN domain"]["message"], "CDN domain web.example.com deleted")
                self.assertNotIn("web.example.com", load_state().get("cdnDomains") or {})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                for name, value in old_secrets.items():
                    _restore_env(name, value)

    def test_cli_deploy_is_blocked_without_alibaba_cloud_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(yaml.safe_dump({"providers": {"dns": {"type": "cloudflare", "zone": "example.com"}}, "defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                manifest = yaml.safe_dump({"name": "web", "image": "nginx", "region": "cn", "exposure": "cn-edge", "domain": "web.example.com", "port": 80, "cdn": "aliyun"})
                with self.assertRaisesRegex(LumaError, "cdn: .*ALIBABA_CLOUD_ACCESS_KEY_ID|cdn: Alibaba Cloud CDN credentials"):
                    handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "web.yaml"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)


if __name__ == "__main__":
    unittest.main()


class CdnRenderTests(unittest.TestCase):
    def test_control_job_mounts_traefik_certificates_read_only(self):
        from luma.nomad_render import TRAEFIK_LETSENCRYPT_VOLUME, render_control_job

        job = render_control_job(image="ghcr.io/x/luma-control:v1", node_name="manager", as_json=False)
        mounts = (job.get("Job") or job)["TaskGroups"][0]["Tasks"][0]["Config"]["mount"]
        self.assertIn(
            {"type": "volume", "target": "/run/luma/traefik-acme", "source": TRAEFIK_LETSENCRYPT_VOLUME, "readonly": True},
            mounts,
        )

    def test_traefik_access_log_keeps_the_cdn_client_address(self):
        from luma.nomad_render import render_traefik_job

        job = render_traefik_job(image="traefik:v3", acme_email="ops@example.com", as_json=False)
        task = (job.get("Job") or job)["TaskGroups"][0]["Tasks"][0]
        self.assertIn("--accesslog.fields.headers.names.Ali-Cdn-Real-Ip=keep", task["Config"]["args"])
