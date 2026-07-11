from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "control-image.yml"


class ControlImageWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_manual_dispatch_publishes_commit_scoped_short_sha_tag(self) -> None:
        self.assertIn("  workflow_dispatch:\n", self.workflow)
        self.assertIn(
            "type=sha,prefix=sha-,format=short,"
            "enable=${{ github.event_name == 'workflow_dispatch' }}",
            self.workflow,
        )

    def test_main_and_release_tag_semantics_are_preserved(self) -> None:
        self.assertIn("branches:\n      - main", self.workflow)
        self.assertIn('tags:\n      - "v*"', self.workflow)
        self.assertIn(
            "type=raw,value=latest,enable={{is_default_branch}}", self.workflow
        )
        self.assertIn(
            "type=ref,event=tag,enable=${{ github.event_name == 'push' }}",
            self.workflow,
        )
        self.assertIn(
            "type=sha,prefix=main-,enable={{is_default_branch}}", self.workflow
        )

    def test_build_push_consumes_only_metadata_action_tags(self) -> None:
        self.assertIn("permissions:\n  contents: read\n  packages: write", self.workflow)
        self.assertIn("push: true", self.workflow)
        self.assertIn("tags: ${{ steps.meta.outputs.tags }}", self.workflow)

    def test_installer_accepts_full_commit_as_immutable_install_ref(self) -> None:
        installer = (ROOT / "scripts" / "install-luma.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("is_commit_ref()", installer)
        self.assertIn('[ "${#1}" -eq 40 ]', installer)
        self.assertIn(
            'default_archive_url="$REPO_URL/archive/$INSTALL_REF.tar.gz"',
            installer,
        )


if __name__ == "__main__":
    unittest.main()
