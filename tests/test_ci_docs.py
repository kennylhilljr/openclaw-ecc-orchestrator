import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI_DOC = ROOT / "docs" / "ci.md"
WORKFLOW = ROOT / "fixtures" / "ci" / "workflow.yml"


def read(path):
    return path.read_text(encoding="utf-8")


class CiDocsTest(unittest.TestCase):
    def test_yaml_block_matches_workflow_fixture(self):
        blocks = re.findall(r"^```yaml\n(.*?\n)```$", read(CI_DOC), re.DOTALL | re.MULTILINE)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0], read(WORKFLOW))

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
