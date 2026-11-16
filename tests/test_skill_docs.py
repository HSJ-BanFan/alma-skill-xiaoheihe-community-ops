from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(os.environ.get("XHH_TEST_SKILL_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "tests"))

import verify_skill_docs


class SkillDocsTest(unittest.TestCase):
    def test_packaged_tree_has_resolvable_links_and_frontmatter(self):
        report = verify_skill_docs.check(ROOT)
        self.assertEqual(report["broken_links"], [])
        self.assertEqual(report["missing_frontmatter"], [])
        self.assertEqual(report["unlinked_references"], [])
        self.assertGreater(report["documents"], 5)

    def test_deleting_a_referenced_file_breaks_the_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "references").mkdir()
            (root / "SKILL.md").write_text(
                "---\nname: fixture\ndescription: fixture skill\n---\n"
                "[rules](references/rules.md)\n", encoding="utf-8")
            target = root / "references/rules.md"
            target.write_text("# rules\n", encoding="utf-8")
            self.assertTrue(verify_skill_docs.check(root)["ok"])
            target.unlink()
            report = verify_skill_docs.check(root)
            self.assertFalse(report["ok"])
            self.assertIn({"file": "SKILL.md", "target": "references/rules.md"},
                          report["broken_links"])

    def test_pseudo_links_in_code_are_ignored(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "SKILL.md").write_text(
                "---\nname: fixture\ndescription: fixture skill\n---\n"
                "```markdown\n[missing](missing.md)\n```\n\nInline `[gone](gone.md)` too.\n",
                encoding="utf-8")
            report = verify_skill_docs.check(root)
            self.assertTrue(report["ok"], report)

    def test_parent_audit_directory_does_not_hide_packaged_documents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / ".audit" / "copied-skill"
            root.mkdir(parents=True)
            (root / "SKILL.md").write_text(
                "---\nname: fixture\ndescription: fixture skill\n---\n"
                "[guide](guide.md)\n", encoding="utf-8")
            (root / "guide.md").write_text("# guide\n", encoding="utf-8")
            (root / ".audit").mkdir()
            (root / ".audit/private-notes.md").write_text("[missing](missing.md)\n", encoding="utf-8")
            report = verify_skill_docs.check(root)
            self.assertTrue(report["ok"], report)
            self.assertEqual(report["documents"], 2)


if __name__ == "__main__":
    unittest.main()
