from __future__ import annotations

import re
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "lae-deploy"


class SkillAssetTests(unittest.TestCase):
    def test_skill_and_controller_share_versioned_knowledge_source(self) -> None:
        pack_path = ROOT / "knowledge" / "v1" / "knowledge-pack.json"
        skill_pack_path = SKILL / "references" / "knowledge-pack.json"
        self.assertEqual(
            skill_pack_path.read_bytes(),
            pack_path.read_bytes(),
            "published Skill knowledge must be identical to the Controller source",
        )
        pack = json.loads(pack_path.read_text(encoding="utf-8"))
        self.assertEqual(pack["schemaVersion"], "lae.knowledge-pack/v1")
        self.assertEqual(pack["knowledgeVersion"], "2026-07-11.1")
        policy = (SKILL / "references" / "policy.md").read_text(encoding="utf-8")
        self.assertIn("knowledge-pack.json", policy)
        for key in (
            "product",
            "manifest",
            "security",
            "resourcesAndPlacement",
            "environment",
            "healthAndFrameworkRecipes",
            "verdicts",
            "blockers",
        ):
            self.assertIn(key, pack)
    def test_lae_deploy_skill_is_closed_and_references_existing_files(self) -> None:
        skill_text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(skill_text.startswith("---\nname: lae-deploy\n"))
        self.assertIn("description:", skill_text.split("---", 2)[1])
        self.assertNotIn("TODO", skill_text)
        self.assertLess(len(skill_text.splitlines()), 500)

        references = re.findall(r"\]\((references/[^)]+)\)", skill_text)
        self.assertEqual(
            set(references),
            {"references/cli-contract.md", "references/policy.md"},
        )
        for reference in references:
            self.assertTrue((SKILL / reference).is_file(), reference)

    def test_skill_metadata_and_secret_guardrails(self) -> None:
        metadata = (SKILL / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn('default_prompt: "Use $lae-deploy ', metadata)
        self.assertNotIn("TODO", metadata)

        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (SKILL / "SKILL.md", *(SKILL / "references").glob("*.md"))
        )
        for expected in (
            "Run `inspect` before every new deployment",
            "Never call Luma management APIs",
            "Do not ask them to paste the value into the conversation",
            "Do not attempt public TCP/UDP",
            "Never complete payment for the user",
        ):
            self.assertIn(expected, combined)


if __name__ == "__main__":
    unittest.main()
