"""Harbor management and OCI content verification; no stop-the-world scripts."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from ..errors import LumaError
from .model import Conflict, DIGEST


class Harbor:
    def __init__(self, settings):
        self.settings = settings
        self.base = settings.registry_url
        self.basic = "Basic " + base64.b64encode(f"{settings.registry_username}:{settings.registry_password}".encode()).decode()

    def request(self, method: str, path: str, body=None, *, media="application/json", expected=(200,), bearer=""):
        raw = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        headers = {"Authorization": "Bearer " + bearer if bearer else self.basic,
                   "Accept": "application/vnd.oci.image.index.v1+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json,application/vnd.docker.distribution.manifest.list.v2+json,application/json",
                   "Content-Type": media}
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, data=raw, method=method, headers=headers), timeout=30) as response:
                status, data, result_headers = response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            status, data, result_headers = exc.code, exc.read(), dict(exc.headers)
            challenge = exc.headers.get("WWW-Authenticate", "")
            if status == 401 and not bearer and challenge.lower().startswith("bearer "):
                values = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
                realm = values.get("realm", "")
                if urllib.parse.urlsplit(realm).netloc != urllib.parse.urlsplit(self.base).netloc:
                    raise LumaError("Harbor token realm does not match the configured registry") from exc
                separator = "&" if "?" in realm else "?"
                url = realm + separator + urllib.parse.urlencode({k: values[k] for k in ("service", "scope") if k in values})
                with urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": self.basic}), timeout=20) as response:
                    token = json.load(response).get("token")
                if not token:
                    raise LumaError("Harbor did not issue a registry token")
                return self.request(method, path, body, media=media, expected=expected, bearer=token)
        if status not in expected:
            raise (Conflict if 400 <= status < 500 else LumaError)(f"Harbor {method} {path} returned HTTP {status}")
        return data, result_headers

    def manifest(self, image: str) -> dict:
        prefix = self.settings.registry_host + "/"
        if not image.startswith(prefix):
            raise Conflict("artifact is outside the configured Harbor registry")
        ref = image[len(prefix):]
        repository, reference = ref.rsplit("@", 1) if "@" in ref else ref.rsplit(":", 1)
        path = "/v2/" + urllib.parse.quote(repository, safe="/") + "/manifests/" + urllib.parse.quote(reference, safe=":")
        raw, headers = self.request("GET", path)
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        claimed = next((v for k, v in headers.items() if k.lower() == "docker-content-digest"), digest)
        if digest != claimed or (reference.startswith("sha256:") and reference != digest):
            raise Conflict("registry manifest digest verification failed")
        value = json.loads(raw)
        return {"digest": digest, "size": len(raw), "mediaType": value["mediaType"], "manifest": value,
                "image": prefix + repository + "@" + digest}

    def platform_manifest(self, image: str, platform: str) -> dict:
        part = self.manifest(image)
        value = part['manifest']
        if 'manifests' in value:
            os_name, architecture = platform.split('/')
            matches = [item for item in value['manifests'] if item.get('platform', {}).get('os') == os_name and item.get('platform', {}).get('architecture') == architecture]
            if len(matches) != 1:
                raise Conflict('registry index does not uniquely identify the native platform')
            image = image.split('@')[0] + '@' + matches[0]['digest']
            part = self.manifest(image)
            value = part['manifest']
        config_digest = value.get('config', {}).get('digest', '')
        if not DIGEST.fullmatch(config_digest):
            raise Conflict('image has no OCI config digest')
        repository = image[len(self.settings.registry_host) + 1:].split('@')[0]
        raw, _ = self.request('GET', '/v2/' + repository + '/blobs/' + config_digest)
        if 'sha256:' + hashlib.sha256(raw).hexdigest() != config_digest:
            raise Conflict('image config digest verification failed')
        config = json.loads(raw)
        if config.get('os', '') + '/' + config.get('architecture', '') != platform:
            raise Conflict('image config does not match requested native platform')
        return part

    def index(self, repository: str, tag: str, parts: list[dict]) -> str:
        if len(parts) == 1:
            return parts[0]["image"]
        descriptors = [{k: part[k] for k in ("digest", "size", "mediaType", "platform")} for part in parts]
        value = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": descriptors}
        self.request("PUT", f"/v2/{repository}/manifests/{tag}", value, media=value["mediaType"], expected=(201,))
        return self.manifest(self.settings.registry_host + "/" + repository + ":" + tag)["image"]

    def pin(self, image: str, tag: str):
        project, repository, digest = self._artifact_path(image)
        self.request("POST", f"/api/v2.0/projects/{project}/repositories/{repository}/artifacts/{digest}/tags", {"name": tag}, expected=(201, 409))

    def _artifact_path(self, image: str):
        ref = image.removeprefix(self.settings.registry_host + "/")
        repository, digest = ref.rsplit("@", 1)
        project, repository = repository.split("/", 1)
        if not DIGEST.fullmatch(digest):
            raise Conflict("immutable digest required")
        return urllib.parse.quote(project, safe=""), urllib.parse.quote(urllib.parse.quote(repository, safe=""), safe=""), digest

    def inventory(self, *, page=1, limit=50) -> dict:
        project = urllib.parse.quote(self.settings.registry_project, safe="")
        data, headers = self.request("GET", f"/api/v2.0/projects/{project}/repositories?page={page}&page_size={limit}")
        repos = json.loads(data)
        return {"backend": "harbor", "repositories": repos, "page": {"number": page, "limit": limit,
                "total": int(next((v for k, v in headers.items() if k.lower() == 'x-total-count'), len(repos)))}}

    def gc(self, *, dry_run: bool) -> dict:
        data, _ = self.request("POST", "/api/v2.0/system/gc/schedule", {"schedule": {"type": "Manual"}, "parameters": {"dry_run": dry_run, "delete_untagged": False}}, expected=(200, 201, 202))
        return {"backend": "harbor", "scheduled": True, "dryRun": dry_run, "result": json.loads(data) if data else {}}
