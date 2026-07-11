import copy
import re
import unittest
from pathlib import Path

from luma.compose import compose_public_services, load_compose_deployment
from luma.config import LumaConfig
from luma.io import load_yaml
from luma.nomad_render import render_compose_job


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "lae" / "deploy" / "luma"
SHA256_IMAGE = re.compile(r"^[^\s]+@sha256:[0-9a-f]{64}$")
PLACEHOLDER_IMAGES = {
    "web": "registry.internal/lae/web:git-sha",
    "api": "registry.internal/lae/api:git-sha",
    "worker": "registry.internal/lae/worker:git-sha",
    "agent-controller": "registry.internal/lae/agent-controller:git-sha",
    "artifact-store": "registry.internal/lae/artifact-store:git-sha",
    "artifact-init": "registry.internal/lae/artifact-init:git-sha",
}
STORAGE_CLASSES = {
    "lae-cn-artifacts": {
        "provider": "nfs",
        "mode": "external",
        "endpoint": "nfs.example.test:/srv/lae-artifacts",
        "regions": ["cn"],
    },
    "lae-cn-postgres": {
        "provider": "nfs",
        "mode": "external",
        "endpoint": "nfs.example.test:/srv/lae-postgres",
        "regions": ["cn"],
    },
}
LIVE_STAGING_STORAGE_CLASSES = {
    "builder-registry-nfs": {
        "provider": "nfs",
        "mode": "managed",
        "node": "builder",
        "path": "/srv/luma",
        "regions": ["home"],
    }
}


def config() -> LumaConfig:
    return LumaConfig(
        {
            "defaults": {
                "engine": "nomad",
                "entrypoint": "websecure",
                "certResolver": "letsencrypt",
            }
        },
        None,
    )


class LaeLumaDeployAssetTests(unittest.TestCase):
    def load(self, sidecar_name: str):
        return load_compose_deployment(
            DEPLOY / sidecar_name,
            storage_classes=STORAGE_CLASSES,
            allow_sidecar_storage_classes=False,
            allow_build_services=True,
        )

    def with_import_images(self, deployment):
        result = copy.deepcopy(deployment)
        for name, service in result.compose["services"].items():
            if service.get("build") is not None:
                service["image"] = PLACEHOLDER_IMAGES[name]
                service.pop("build", None)
        return result

    def test_production_is_valid_import_shape_and_renders_three_http_routes(self):
        deployment = self.load("luma.compose.yml")
        self.assertEqual(deployment.name, "lae-platform")
        self.assertEqual(
            {service.name for service in compose_public_services(deployment)},
            {"web", "api", "artifact-store"},
        )
        self.assertEqual(
            {service.exposure for service in compose_public_services(deployment)},
            {"cn-edge"},
        )
        self.assertEqual(len(deployment.warnings), len(PLACEHOLDER_IMAGES))

        rendered = render_compose_job(
            config(),
            self.with_import_images(deployment),
            as_json=False,
            resolve_secrets=False,
        )["Job"]
        self.assertEqual(rendered["ID"], "lae-platform")
        self.assertEqual(len(rendered["TaskGroups"]), 1)
        group = rendered["TaskGroups"][0]
        self.assertEqual(len(group["Tasks"]), 8)
        self.assertEqual(
            {service["Name"] for service in group["Services"]},
            {"web", "api", "artifact-store"},
        )
        self.assertEqual(
            {port["To"] for port in group["Networks"][0]["DynamicPorts"]},
            {3000, 8080, 9000},
        )

    def test_live_itool_staging_overlay_uses_only_existing_internal_topology(self):
        deployment = load_compose_deployment(
            DEPLOY / "luma.compose.staging.itool.yml",
            storage_classes=LIVE_STAGING_STORAGE_CLASSES,
            allow_sidecar_storage_classes=False,
            allow_build_services=True,
        )
        self.assertEqual(deployment.name, "lae-platform-staging")
        self.assertEqual(deployment.region, "home")
        self.assertEqual(
            {volume.storage_class for volume in deployment.volumes.values()},
            {"builder-registry-nfs"},
        )
        self.assertEqual(
            {service.node for service in deployment.services.values()}, {"lab"}
        )
        self.assertEqual(
            {service.name for service in compose_public_services(deployment)},
            {"web", "api", "agent-controller", "artifact-store"},
        )
        rendered = render_compose_job(
            config(),
            self.with_import_images(deployment),
            as_json=False,
            resolve_secrets=False,
        )["Job"]
        self.assertIn("lab", str(rendered["Constraints"]))
        self.assertIn("home", str(rendered["Constraints"]))

    def test_only_signed_s3_data_plane_is_public_beside_web_and_api(self):
        for sidecar_name in ("luma.compose.yml", "luma.compose.staging.yml"):
            deployment = self.load(sidecar_name)
            for name in (
                "worker",
                "agent-controller",
                "postgres",
                "artifact-init",
                "valkey",
            ):
                self.assertEqual(deployment.services[name].exposure, "none")
                self.assertIsNone(deployment.services[name].domain)
            artifact = deployment.services["artifact-store"]
            self.assertEqual(artifact.exposure, "cn-edge")
            self.assertEqual(artifact.port, 9000)
            self.assertIn("artifacts", artifact.domain or "")
            if "mailpit" in deployment.services:
                self.assertEqual(deployment.services["mailpit"].exposure, "none")
                self.assertIsNone(deployment.services["mailpit"].domain)

    def test_compose_has_no_host_or_public_port_escape_hatches(self):
        for compose_name in ("docker-compose.yml", "docker-compose.staging.yml"):
            compose = load_yaml(DEPLOY / compose_name)
            for name, service in compose["services"].items():
                self.assertNotIn("ports", service, name)
                self.assertNotIn("network_mode", service, name)
                self.assertNotIn("privileged", service, name)
                self.assertNotIn("devices", service, name)
                self.assertIn("healthcheck", service, name)
                for volume in service.get("volumes", []):
                    source = str(volume).split(":", 1)[0]
                    self.assertFalse(source.startswith(("/", ".", "~")), name)
                    self.assertNotIn("docker.sock", str(volume), name)

            rendered = (DEPLOY / compose_name).read_text(encoding="utf-8")
            self.assertNotIn('"$VALKEY_PASSWORD"', rendered)
            self.assertIn('"$${VALKEY_PASSWORD}"', rendered)

    def test_images_and_builds_are_reproducible_inputs(self):
        compose = load_yaml(DEPLOY / "docker-compose.yml")
        for name, service in compose["services"].items():
            if "image" in service:
                self.assertRegex(service["image"], SHA256_IMAGE, name)
                self.assertNotIn(":latest", service["image"], name)
                continue
            build = service["build"]
            self.assertEqual(service["platform"], "linux/amd64")
            context = (DEPLOY / build["context"]).resolve()
            dockerfile = context / build["dockerfile"]
            self.assertEqual(context, ROOT / "lae")
            self.assertTrue(dockerfile.is_file(), dockerfile)
            text = dockerfile.read_text(encoding="utf-8")
            self.assertNotIn(":latest", text)
            self.assertIn("@sha256:", text)
            self.assertIn("USER ", text)
            if name not in {"web", "artifact-store", "artifact-init"}:
                self.assertIn("UV_PROJECT_ENVIRONMENT=/opt/lae/.venv", text)
                self.assertIn(
                    "COPY --from=build /opt/lae/.venv /opt/lae/.venv", text
                )

        web_dockerfile = (
            DEPLOY / "docker" / "web.Dockerfile"
        ).read_text(encoding="utf-8")
        self.assertIn("LAE_API_INTERNAL_URL=http://api:8080", web_dockerfile)
        self.assertIn("NEXT_PUBLIC_LAE_UPLOAD_ORIGINS", web_dockerfile)
        self.assertIn("production API rewrite is not container-internal", web_dockerfile)

        minio_dockerfile = (DEPLOY / "docker" / "minio.Dockerfile").read_text(
            encoding="utf-8"
        )
        self.assertIn("RELEASE.2025-10-15T17-29-55Z", minio_dockerfile)
        self.assertIn(
            "9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a", minio_dockerfile
        )
        self.assertNotIn("RELEASE.2025-09-07T16-13-09Z", minio_dockerfile)

        runner_dockerfile = (
            DEPLOY / "docker" / "agent-runner.Dockerfile"
        ).read_text(encoding="utf-8")
        self.assertIn("--package lae-agent-runner", runner_dockerfile)
        self.assertIn('ENTRYPOINT ["lae-agent-runner"]', runner_dockerfile)
        self.assertIn("USER 10001:10001", runner_dockerfile)
        self.assertIn("@sha256:", runner_dockerfile)
        self.assertNotIn(":latest", runner_dockerfile)

        init_script = (DEPLOY / "docker" / "artifact-init.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("admin policy create", init_script)
        self.assertIn("anonymous set none", init_script)
        self.assertNotIn("cors set", init_script)
        self.assertNotIn("s3:*", init_script)

        production_store = self.load("luma.compose.yml").compose["services"][
            "artifact-store"
        ]["environment"]
        staging_store = self.load("luma.compose.staging.yml").compose["services"][
            "artifact-store"
        ]["environment"]
        self.assertEqual(
            production_store["MINIO_API_CORS_ALLOW_ORIGIN"],
            "https://lae.itool.tech",
        )
        self.assertEqual(
            staging_store["MINIO_API_CORS_ALLOW_ORIGIN"],
            "https://lae-staging.itool.tech",
        )
        self.assertNotIn("*", production_store["MINIO_API_CORS_ALLOW_ORIGIN"])
        self.assertNotIn("*", staging_store["MINIO_API_CORS_ALLOW_ORIGIN"])

    def test_managed_storage_and_mailpit_environment_boundary(self):
        production = self.load("luma.compose.yml")
        staging = self.load("luma.compose.staging.yml")
        self.assertNotIn("mailpit", production.compose["services"])
        self.assertIn("mailpit", staging.compose["services"])
        for deployment in (production, staging):
            for volume in deployment.volumes.values():
                self.assertIsNotNone(volume.storage_class)
                self.assertEqual(volume.initialize, "empty")
                self.assertEqual(volume.access_mode, "ReadWriteOnce")

        production_api = production.compose["services"]["api"]["environment"]
        self.assertEqual(production_api["LAE_ENVIRONMENT"], "production")
        self.assertEqual(production_api["LAE_BILLING_DRIVER"], "disabled")
        self.assertEqual(production_api["LAE_EMAIL_DRIVER"], "smtp")
        self.assertEqual(production_api["LAE_SMTP_SECURITY"], "tls")
        self.assertEqual(str(production_api["LAE_SMTP_PORT"]), "465")

        staging_api = staging.compose["services"]["api"]["environment"]
        self.assertEqual(staging_api["LAE_ENVIRONMENT"], "staging")
        self.assertEqual(staging_api["LAE_BILLING_DRIVER"], "mock")
        self.assertEqual(staging_api["LAE_SMTP_HOST"], "mailpit")
        self.assertEqual(staging_api["LAE_SMTP_SECURITY"], "plain")

    def test_env_example_covers_references_without_secret_values(self):
        referenced: set[str] = set()
        for compose_name in ("docker-compose.yml", "docker-compose.staging.yml"):
            text = (DEPLOY / compose_name).read_text(encoding="utf-8")
            referenced.update(
                re.findall(r"(?<!\$)\$\{([A-Z][A-Z0-9_]*)\}", text)
            )

        values: dict[str, str] = {}
        for line in (DEPLOY / ".env.example").read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            name, separator, value = line.partition("=")
            self.assertEqual(separator, "=", line)
            values[name] = value

        self.assertTrue(referenced.issubset(values.keys()))
        sensitive = {
            "LAE_ANALYZER_IMAGE_DIGEST",
            "LAE_ADMIN_API_TOKEN",
            "LAE_APPLICATION_IDEMPOTENCY_HMAC_KEY",
            "LAE_AUTH_HMAC_KEY",
            "LAE_BILLING_HMAC_KEY",
            "LAE_BUILD_CREDENTIAL_LEASE_HMAC_KEY",
            "LAE_BUILD_PLAN_SIGNING_HMAC_KEY",
            "LAE_CREDENTIAL_BROKER_TOKEN",
            "LAE_DATABASE_URL",
            "LAE_DEPLOYMENT_IDEMPOTENCY_HMAC_KEY",
            "LAE_ENVIRONMENT_AEAD_KEYS",
            "LAE_ENVIRONMENT_CHECKSUM_HMAC_KEY",
            "LAE_LUMA_SERVICE_TOKEN",
            "LAE_LUMA_RUNTIME_SERVICE_TOKEN",
            "LAE_MINIO_ROOT_PASSWORD",
            "LAE_MOCK_CHECKOUT_BASE_URL",
            "LAE_MOCK_PAYMENT_MERCHANT_ID",
            "LAE_MOCK_PAYMENT_SIGNING_KEY",
            "LAE_POSTGRES_PASSWORD",
            "LAE_S3_API_ACCESS_KEY",
            "LAE_S3_API_SECRET_KEY",
            "LAE_S3_WORKER_ACCESS_KEY",
            "LAE_S3_WORKER_SECRET_KEY",
            "LAE_SMTP_PASSWORD",
            "LAE_SMTP_USERNAME",
            "LAE_SOURCE_CONNECTION_AEAD_KEYS",
            "LAE_SOURCE_CONNECTION_HMAC_KEYS",
            "LAE_SOURCE_CONNECTION_IDEMPOTENCY_HMAC_KEY",
            "LAE_UPLOAD_HMAC_KEY",
            "LAE_VALKEY_PASSWORD",
            "LAE_WORKER_STATE_HMAC_KEY",
        }
        self.assertEqual({name: values[name] for name in sensitive}, dict.fromkeys(sensitive, ""))

    def test_upload_analysis_has_exact_internal_redemption_capability(self):
        for compose_name in ("docker-compose.yml", "docker-compose.staging.yml"):
            compose = load_yaml(DEPLOY / compose_name)
            api = compose["services"]["api"]["environment"]
            self.assertEqual(str(api["LAE_UPLOAD_ANALYSIS_BROKER_ENABLED"]), "1")
            self.assertEqual(
                api["LAE_CREDENTIAL_BROKER_TOKEN"],
                "${LAE_CREDENTIAL_BROKER_TOKEN}",
            )
            self.assertEqual(api["LAE_UPLOAD_DRIVER"], "s3")
            self.assertIn("LAE_UPLOAD_S3_ACCESS_KEY", api)
            self.assertIn("LAE_UPLOAD_S3_SECRET_KEY", api)

    def test_api_entrypoint_materializes_admin_token_and_waits_for_database(self):
        entrypoint = (DEPLOY / "docker" / "api-entrypoint.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("umask 077", entrypoint)
        self.assertIn("LAE_ADMIN_API_TOKEN_FILE=/tmp/lae-admin-api.token", entrypoint)
        self.assertIn("unset LAE_ADMIN_API_TOKEN", entrypoint)
        self.assertIn("until alembic", entrypoint)
        self.assertIn("LAE_MIGRATION_MAX_ATTEMPTS", entrypoint)
        self.assertIn("LAE_MIGRATION_RETRY_SECONDS", entrypoint)

    def test_worker_and_api_use_separate_runtime_principal(self):
        for compose_name in ("docker-compose.yml", "docker-compose.staging.yml"):
            compose = load_yaml(DEPLOY / compose_name)
            api = compose["services"]["api"]["environment"]
            worker = compose["services"]["worker"]["environment"]
            self.assertEqual(str(worker["LAE_DEPLOYMENT_WORKER_ENABLED"]), "1")
            self.assertEqual(worker["LAE_LUMA_RUNTIME_PRINCIPAL_ID"], "lae-runtime")
            self.assertEqual(worker["LAE_LUMA_SERVICE_PRINCIPAL_ID"], "lae-builder")
            self.assertNotEqual(
                worker["LAE_LUMA_RUNTIME_SERVICE_TOKEN"],
                worker["LAE_LUMA_SERVICE_TOKEN"],
            )
            self.assertEqual(
                api["LAE_LUMA_RUNTIME_SERVICE_TOKEN"],
                worker["LAE_LUMA_RUNTIME_SERVICE_TOKEN"],
            )
            self.assertIn("LAE_BUILD_PLAN_SIGNING_HMAC_KEY", worker)
            self.assertIn("LAE_BUILD_CREDENTIAL_LEASE_HMAC_KEY", worker)
            self.assertIn("LAE_ADMIN_API_TOKEN", api)


if __name__ == "__main__":
    unittest.main()
