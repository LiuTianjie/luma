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
    "backup": "registry.internal/lae/backup:git-sha",
    "valkey": "registry.internal/lae/valkey:git-sha",
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
    "lae-cn-backups": {
        "provider": "nfs",
        "mode": "external",
        "endpoint": "backup-nfs.example.test:/srv/lae-backups",
        "regions": ["cn"],
    },
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

    def test_backup_image_uses_existing_builder_postgres_manifest(self):
        dockerfile = (DEPLOY / "docker" / "backup.Dockerfile").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "100.66.177.70:5000/lae/postgres-amd64:17@sha256:"
            "5aee909f99ab78c62f03636b6ca25a17195657605ce6782d9919ce4288595eda",
            dockerfile,
        )
        self.assertNotIn("/luma-system/postgres-amd64", dockerfile)

        script = (DEPLOY / "docker" / "backup.sh").read_text(encoding="utf-8")
        self.assertNotIn('chmod 0700 "$backup_root"', script)
        self.assertIn('chmod 0700 "$snapshot_root"', script)

    def test_runtime_has_no_retired_or_public_registry_pull(self):
        compose = load_yaml(DEPLOY / "docker-compose.yml")
        postgres = compose["services"]["postgres"]
        self.assertTrue(postgres["image"].startswith("100.66.177.70:5000/lae/"))
        self.assertNotIn("registry.itool.tech", postgres["image"])

        valkey = compose["services"]["valkey"]
        self.assertNotIn("image", valkey)
        self.assertEqual(
            valkey["build"]["dockerfile"], "deploy/luma/docker/valkey.Dockerfile"
        )
        dockerfile = (DEPLOY / "docker" / "valkey.Dockerfile").read_text(
            encoding="utf-8"
        )
        self.assertIn("FROM valkey/valkey:9.1.0-alpine@sha256:", dockerfile)

    def test_control_bundle_install_is_versioned_and_atomic(self):
        script = (ROOT / "scripts/install-lae-control-bundle.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("bundleFingerprint", script)
        self.assertIn("os.replace(temporary, target)", script)
        self.assertIn("LUMA_LAE_SERVICE_PRINCIPALS_FILE", script)
        self.assertIn("LUMA_LAE_RUNTIME_SERVICE_PRINCIPALS_FILE", script)
        self.assertIn('atomic("control.env"', script)
        self.assertNotIn("source $", script)

        runner = (ROOT / "scripts/update-lae-builder-runner.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("@sha256:", runner)
        self.assertIn("docker --host", runner)
        self.assertIn("RepoDigests", runner)
        self.assertIn("matches != 1", runner)
        self.assertNotIn("docker daemon", runner.lower())

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

    def test_production_is_valid_import_shape_and_renders_http_routes(self):
        deployment = self.load("luma.compose.yml")
        self.assertEqual(deployment.name, "lae-platform")
        self.assertEqual(
            {service.name for service in compose_public_services(deployment)},
            {"web", "api", "agent-controller", "artifact-store"},
        )
        self.assertEqual(
            {service.exposure for service in compose_public_services(deployment)},
            {"cn-edge"},
        )
        self.assertEqual(
            len(deployment.warnings),
            sum(
                service.get("build") is not None
                for service in deployment.compose["services"].values()
            ),
        )

        rendered = render_compose_job(
            config(),
            self.with_import_images(deployment),
            as_json=False,
            resolve_secrets=False,
            node_records={},
        )["Job"]
        self.assertEqual(rendered["ID"], "lae-platform")
        self.assertEqual(len(rendered["TaskGroups"]), 1)
        group = rendered["TaskGroups"][0]
        self.assertEqual(len(group["Tasks"]), 9)
        self.assertEqual(
            {service["Name"] for service in group["Services"]},
            {
                "lae-platform-web",
                "lae-platform-api",
                "lae-platform-agent-controller",
                "lae-platform-artifact-store",
            },
        )
        self.assertEqual(
            {port["To"] for port in group["Networks"][0]["DynamicPorts"]},
            {3000, 8080, 8081, 9000},
        )

    def test_only_signed_s3_data_plane_is_public_beside_web_and_api(self):
        deployment = self.load("luma.compose.yml")
        for name in (
            "worker",
            "postgres",
            "artifact-init",
            "backup",
            "valkey",
        ):
            self.assertEqual(deployment.services[name].exposure, "none")
            self.assertIsNone(deployment.services[name].domain)
        artifact = deployment.services["artifact-store"]
        self.assertEqual(artifact.exposure, "cn-edge")
        self.assertEqual(artifact.port, 9000)
        self.assertIn("artifacts", artifact.domain or "")
        controller = deployment.services["agent-controller"]
        self.assertEqual(controller.exposure, "cn-edge")
        self.assertEqual(controller.port, 8081)
        self.assertEqual(controller.domain, "lae-agent.itool.tech")

    def test_compose_has_no_host_or_public_port_escape_hatches(self):
        compose = load_yaml(DEPLOY / "docker-compose.yml")
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

        rendered = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
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
            if name not in {
                "web",
                "artifact-store",
                "artifact-init",
                "backup",
                "valkey",
            }:
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
        self.assertIn("verify-agent-runner-contract.py", runner_dockerfile)
        self.assertNotIn(":latest", runner_dockerfile)

        init_script = (DEPLOY / "docker" / "artifact-init.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("admin policy create", init_script)
        self.assertIn("anonymous set none", init_script)
        self.assertNotIn("cors set", init_script)
        self.assertNotIn("s3:*", init_script)

        backup_script = (DEPLOY / "docker" / "backup.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("pg_dump --format=custom", backup_script)
        self.assertIn(
            "pg_restore --single-transaction --exit-on-error", backup_script
        )
        self.assertIn("pg_isready --quiet", backup_script)
        self.assertIn("LAE_BACKUP_DEPENDENCY_TIMEOUT_SECONDS", backup_script)
        self.assertIn("sha256sum -c SHA256SUMS", backup_script)
        self.assertIn("lae-restore-drill-", backup_script)
        self.assertNotIn("set -x", backup_script)

        production_store = self.load("luma.compose.yml").compose["services"][
            "artifact-store"
        ]["environment"]
        self.assertEqual(
            production_store["MINIO_API_CORS_ALLOW_ORIGIN"],
            "https://lae.itool.tech",
        )
        self.assertNotIn("*", production_store["MINIO_API_CORS_ALLOW_ORIGIN"])

    def test_platform_local_storage_and_external_email_environment_boundary(self):
        production = self.load("luma.compose.yml")
        self.assertNotIn("mailpit", production.compose["services"])
        for volume in production.volumes.values():
            self.assertEqual(volume.kind, "local")
            self.assertEqual(volume.local_node, "manager")
            self.assertIsNone(volume.storage_class)
            self.assertEqual(volume.initialize, "empty")
            self.assertEqual(volume.access_mode, "ReadWriteOnce")

        production_api = production.compose["services"]["api"]["environment"]
        self.assertEqual(production_api["LAE_ENVIRONMENT"], "production")
        self.assertEqual(production_api["LAE_BILLING_DRIVER"], "disabled")
        self.assertEqual(production_api["LAE_EMAIL_DRIVER"], "smtp")
        self.assertEqual(str(production_api["LAE_AUTH_EXTERNAL_MAILBOX"]), "1")
        self.assertEqual(production_api["LAE_SMTP_SECURITY"], "starttls")
        self.assertEqual(production_api["LAE_SMTP_PORT"], "${ITOOL_TECH_SMTP_PORT}")
        self.assertEqual(production_api["LAE_AUTH_PREVIEW_MODE"], "disabled")
        self.assertNotIn("LAE_AUTH_PREVIEW_EMAIL", production_api)

    def test_env_example_covers_references_without_secret_values(self):
        referenced: set[str] = set()
        text = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
        referenced.update(re.findall(r"(?<!\$)\$\{([A-Z][A-Z0-9_]*)\}", text))

        values: dict[str, str] = {}
        for example_name in (".env.example", ".global-secrets.example"):
            for line in (DEPLOY / example_name).read_text(encoding="utf-8").splitlines():
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
            "ITOOL_TECH_SMTP_HOST",
            "ITOOL_TECH_SMTP_PASS",
            "ITOOL_TECH_SMTP_PORT",
            "ITOOL_TECH_SMTP_USER",
        }
        self.assertEqual({name: values[name] for name in sensitive}, dict.fromkeys(sensitive, ""))

    def test_upload_analysis_has_exact_internal_redemption_capability(self):
        compose = load_yaml(DEPLOY / "docker-compose.yml")
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
        compose = load_yaml(DEPLOY / "docker-compose.yml")
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
