from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

LAE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAE_ROOT / "packages" / "contracts" / "src"))
sys.path.insert(0, str(LAE_ROOT.parent))

from lae_contracts import (  # noqa: E402
    EXPECTED_SCHEMAS,
    is_safe_external_image_reference,
    load_schema,
    specs_root,
    validate_instance,
    validate_repository,
)
from luma.builder_tasks import validate_builder_task_request  # noqa: E402
from luma.errors import LumaError  # noqa: E402


class ContractRepositoryTests(unittest.TestCase):
    def test_repository_and_examples_are_valid(self) -> None:
        result = validate_repository()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["schemas"], 7)
        self.assertGreaterEqual(result["validExamples"], 6)
        self.assertGreaterEqual(result["invalidExamples"], 5)

    def test_every_schema_rejects_additional_root_properties(self) -> None:
        manifest_path = specs_root() / "examples" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for suite in manifest["suites"]:
            example_path = specs_root() / "examples" / suite["valid"][0]
            value = json.loads(example_path.read_text(encoding="utf-8"))
            value["unexpectedRootField"] = True
            with self.subTest(schema=suite["schema"]):
                self.assertTrue(validate_instance(suite["schema"], value))

    def test_task_kind_and_payload_are_bound(self) -> None:
        example_path = (
            specs_root() / "examples" / "valid" / "luma-builder-task.analyze.json"
        )
        value = json.loads(example_path.read_text(encoding="utf-8"))
        value["kind"] = "build-plan"
        issues = validate_instance("luma-builder-task.v1.schema.json", value)
        self.assertTrue(any("oneOf" in issue.message for issue in issues))

    def test_expected_schema_files_are_loadable(self) -> None:
        for name in EXPECTED_SCHEMAS:
            with self.subTest(schema=name):
                self.assertEqual(
                    load_schema(name)["$schema"],
                    "https://json-schema.org/draft/2020-12/schema",
                )

    def test_external_image_references_are_strictly_versioned_and_public(self) -> None:
        digest = "sha256:" + "d" * 64
        for reference in (
            "postgres:17",
            "library/postgres:17-alpine",
            "ghcr.io/acme/api:v1.2.3",
            "registry.example.com/acme/api:2026-07-11",
            f"docker.io/library/postgres@{digest}",
        ):
            with self.subTest(valid=reference):
                self.assertTrue(is_safe_external_image_reference(reference))

        for reference in (
            "postgres",
            "postgres:latest",
            "postgres:LATEST",
            "localhost:5000/acme/api:v1",
            "registry:5000/acme/api:v1",
            "registry.example.com:5000/acme/api:v1",
            "127.0.0.1/acme/api:v1",
            "10.0.0.1/acme/api:v1",
            "[::1]/acme/api:v1",
            "registry.internal/acme/api:v1",
            "registry.local/acme/api:v1",
            "https://ghcr.io/acme/api:v1",
            "user:password@ghcr.io/acme/api:v1",
            "ghcr.io/acme/api:v1?pull=true",
            "ghcr.io/acme/api:v1#fragment",
            "GHCR.IO/acme/api:v1",
            "ghcr.io/Acme/api:v1",
            "ghcr.io/acme/api@sha256:short",
            f"ghcr.io/acme/api:v1@{digest}",
        ):
            with self.subTest(invalid=reference):
                self.assertFalse(is_safe_external_image_reference(reference))

    def test_build_plan_requires_external_images_and_global_image_keys(self) -> None:
        example_path = (
            specs_root() / "examples" / "valid" / "build-plan.multi-service.json"
        )
        valid = json.loads(example_path.read_text(encoding="utf-8"))
        self.assertFalse(validate_instance("build-plan.v1.schema.json", valid))

        missing = copy.deepcopy(valid)
        missing.pop("externalImages")
        self.assertTrue(validate_instance("build-plan.v1.schema.json", missing))

        missing_resolved_digest = copy.deepcopy(valid)
        missing_resolved_digest["externalImages"][0].pop("resolvedDigest")
        self.assertTrue(
            validate_instance("build-plan.v1.schema.json", missing_resolved_digest)
        )

        duplicate = copy.deepcopy(valid)
        duplicate["externalImages"][0]["key"] = duplicate["builds"][0]["key"]
        issues = validate_instance("build-plan.v1.schema.json", duplicate)
        self.assertTrue(any("globally unique" in issue.message for issue in issues))

        private_registry = copy.deepcopy(valid)
        private_registry["externalImages"][0]["ref"] = (
            "registry.internal/acme/postgres:17"
        )
        issues = validate_instance("build-plan.v1.schema.json", private_registry)
        self.assertTrue(any("public registry" in issue.message for issue in issues))

        too_many = copy.deepcopy(valid)
        too_many["builds"] = [too_many["builds"][0]]
        too_many["externalImages"] = [
            {
                "key": f"image-{index}",
                "ref": f"ghcr.io/acme/image-{index}:v1",
                "resolvedDigest": "sha256:" + "f" * 64,
                "platform": "linux/amd64",
            }
            for index in range(64)
        ]
        issues = validate_instance("build-plan.v1.schema.json", too_many)
        self.assertTrue(
            any("at most 64 total items" in issue.message for issue in issues)
        )

    def test_proposal_allows_unresolved_tags_but_binds_embedded_digests(self) -> None:
        example_path = (
            specs_root()
            / "examples"
            / "valid"
            / "build-plan-proposal.external-images.json"
        )
        proposal = json.loads(example_path.read_text(encoding="utf-8"))
        self.assertFalse(
            validate_instance("build-plan-proposal.v1.schema.json", proposal)
        )
        images = {item["key"]: item for item in proposal["externalImages"]}
        self.assertNotIn("resolvedDigest", images["postgres"])
        self.assertEqual(
            images["valkey"]["resolvedDigest"],
            images["valkey"]["ref"].rsplit("@", 1)[1],
        )

        mismatch = copy.deepcopy(proposal)
        mismatch["externalImages"][1]["resolvedDigest"] = "sha256:" + "b" * 64
        issues = validate_instance("build-plan-proposal.v1.schema.json", mismatch)
        self.assertTrue(
            any("digest embedded in ref" in issue.message for issue in issues)
        )

        pre_resolved_tag = copy.deepcopy(proposal)
        pre_resolved_tag["externalImages"][0]["resolvedDigest"] = (
            "sha256:" + "c" * 64
        )
        issues = validate_instance(
            "build-plan-proposal.v1.schema.json", pre_resolved_tag
        )
        self.assertTrue(
            any("omitted for tagged proposal" in issue.message for issue in issues)
        )

        unresolved_digest = copy.deepcopy(proposal)
        unresolved_digest["externalImages"][1].pop("resolvedDigest")
        issues = validate_instance(
            "build-plan-proposal.v1.schema.json", unresolved_digest
        )
        self.assertTrue(
            any("required for digest proposal" in issue.message for issue in issues)
        )

    def test_candidate_and_signed_plan_bind_embedded_image_digest(self) -> None:
        fixtures = (
            (
                "build-plan-candidate.v1.schema.json",
                "build-plan-candidate.multi-service.json",
            ),
            ("build-plan.v1.schema.json", "build-plan.multi-service.json"),
        )
        embedded = "sha256:" + "a" * 64
        for schema_name, filename in fixtures:
            value = json.loads(
                (specs_root() / "examples" / "valid" / filename).read_text(
                    encoding="utf-8"
                )
            )
            value["externalImages"][0]["ref"] = f"postgres@{embedded}"
            value["externalImages"][0]["resolvedDigest"] = "sha256:" + "b" * 64
            with self.subTest(schema=schema_name):
                issues = validate_instance(schema_name, value)
                self.assertTrue(
                    any("digest embedded in ref" in issue.message for issue in issues)
                )

    def test_luma_manual_validator_matches_canonical_build_task_schema(self) -> None:
        example_path = (
            specs_root() / "examples" / "valid" / "luma-builder-task.build.json"
        )
        valid = json.loads(example_path.read_text(encoding="utf-8"))
        self.assertFalse(validate_instance("luma-builder-task.v1.schema.json", valid))
        validate_builder_task_request(copy.deepcopy(valid))

        variants = []
        for required_field in ("context", "dockerfile", "target", "platform"):
            item = copy.deepcopy(valid)
            item["payload"]["signedBuildPlan"]["builds"][0].pop(required_field)
            variants.append((f"missing-{required_field}", item))
        changes = (
            ("commit-41", ("resolvedCommit", "a" * 41)),
            ("bad-platform", ("platform", "linux/arm64")),
            ("bad-build-key", ("key", "web.api")),
            ("lowercase-env", ("buildArgNames", ["node_env"])),
        )
        for label, (field, value) in changes:
            item = copy.deepcopy(valid)
            if field == "resolvedCommit":
                item["payload"]["signedBuildPlan"][field] = value
            else:
                item["payload"]["signedBuildPlan"]["builds"][0][field] = value
            variants.append((label, item))
        short_signature = copy.deepcopy(valid)
        short_signature["payload"]["signedBuildPlan"]["signature"]["value"] = "short"
        variants.append(("short-signature", short_signature))

        digest = "sha256:" + "d" * 64
        for label, reference in (
            ("external-latest", "postgres:latest"),
            ("external-custom-port", "registry.example.com:5000/acme/api:v1"),
            ("external-tag-and-digest", f"ghcr.io/acme/api:v1@{digest}"),
        ):
            item = copy.deepcopy(valid)
            item["payload"]["signedBuildPlan"]["externalImages"][0]["ref"] = reference
            variants.append((label, item))

        duplicate_image_key = copy.deepcopy(valid)
        duplicate_image_key["payload"]["signedBuildPlan"]["externalImages"][0][
            "key"
        ] = "web"
        variants.append(("duplicate-image-key", duplicate_image_key))

        too_many_images = copy.deepcopy(valid)
        too_many_images["payload"]["signedBuildPlan"]["externalImages"] = [
            {
                "key": f"image-{index}",
                "ref": f"ghcr.io/acme/image-{index}:v1",
                "resolvedDigest": "sha256:" + "f" * 64,
                "platform": "linux/amd64",
            }
            for index in range(64)
        ]
        variants.append(("too-many-total-images", too_many_images))

        for label, item in variants:
            with self.subTest(case=label):
                self.assertTrue(
                    validate_instance("luma-builder-task.v1.schema.json", item)
                )
                with self.assertRaises(LumaError):
                    validate_builder_task_request(item)


if __name__ == "__main__":
    unittest.main()
