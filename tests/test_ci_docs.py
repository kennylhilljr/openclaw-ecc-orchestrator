import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI_DOC = ROOT / "docs" / "ci.md"
WORKFLOW = ROOT / "fixtures" / "ci" / "workflow.yml"
ACTIVE_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def read(path):
    return path.read_text(encoding="utf-8")


class CiDocsTest(unittest.TestCase):
    def test_yaml_block_matches_workflow_fixture(self):
        blocks = re.findall(r"^```yaml\n(.*?\n)```$", read(CI_DOC), re.DOTALL | re.MULTILINE)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0], read(WORKFLOW))

    def test_active_workflow_matches_fixture(self):
        self.assertEqual(read(ACTIVE_WORKFLOW), read(WORKFLOW))

    def test_workflow_requires_gitleaks_and_pins_actions(self):
        text = read(WORKFLOW)
        self.assertIn('ECC_CHECK_REQUIRE_GITLEAKS: "1"', text)
        self.assertIn("sha256sum -c", text)
        for uses in re.findall(r"uses: (\S+)", text):
            with self.subTest(uses=uses):
                self.assertRegex(uses, r"@[0-9a-f]{40}$")

    def test_no_double_hyphen_or_dashes(self):
        text = read(CI_DOC)
        self.assertNotIn("--", text)
        self.assertNotIn("—", text)
        self.assertNotIn("–", text)

    def test_agents_and_readme_name_check_script(self):
        for name in ("AGENTS.md", "README.md"):
            with self.subTest(name=name):
                self.assertIn("scripts/check.sh", read(ROOT / name))


if __name__ == "__main__":
    unittest.main()
