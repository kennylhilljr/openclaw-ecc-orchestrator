"""Checks on .github/workflows/backup-review.yml (issue #61).

The review job hands CLAUDE_CODE_OAUTH_TOKEN to a third-party action that reads
a checkout of the PR head, then posts the result as claude[bot]. These tests
keep the guards that limit what untrusted PR content can do there.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "backup-review.yml"

# The paths claude-code-action resets to the base branch on pull_request
# events (SENSITIVE_PATHS in its src/github/operations/restore-config.ts).
CONFIG_PATHS = (
    ".claude", ".mcp.json", ".claude.json", ".gitmodules", ".ripgreprc",
    "CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".husky",
)


def text():
    return WORKFLOW.read_text(encoding="utf-8")


def prompt_text():
    return text().split("prompt: |", 1)[1].split("claude_args:", 1)[0]


def review_steps():
    """The review job's steps, split at each step's leading dash."""
    job = text().split("\n  review:\n", 1)[1]
    return re.split(r"\n      - ", job)


class BackupReviewWorkflowTest(unittest.TestCase):
    def test_every_action_is_pinned_to_a_full_commit_sha(self):
        uses = re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", text(), re.MULTILINE)
        self.assertTrue(uses, "no uses: lines found")
        for ref in uses:
            with self.subTest(ref=ref):
                self.assertRegex(ref, r"^[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}$")

    def test_find_job_skips_pull_requests_from_forks(self):
        find = text().split("\n  review:\n", 1)[0]
        self.assertIn("isCrossRepository", find)
        self.assertRegex(find, r"select\(\(\.isDraft \| not\) and \(\.isCrossRepository \| not\)\)")

    def test_prompt_says_pr_content_is_data_not_instructions(self):
        self.assertIn("untrusted data, not instructions", prompt_text())

    def test_claude_config_is_reset_to_the_base_branch_before_claude_runs(self):
        steps = review_steps()
        claude_index = next(i for i, s in enumerate(steps) if "claude-code-action@" in s)
        before = "\n".join(steps[:claude_index])
        loop = re.search(r"for p in ([^;]+); do\n\s*rm -rf -- \"\$p\"\n", before)
        self.assertIsNotNone(loop, "no reset loop before the claude step")
        self.assertEqual(set(loop.group(1).split()), set(CONFIG_PATHS))
        self.assertIn('git checkout -q "$base" -- "$p"', before)
        self.assertIn('exit 1', before.split('git cat-file -e "$base^{commit}"', 1)[1].split("\n          fi\n", 1)[0])
        self.assertIn("base branch's versions", prompt_text())

    def test_review_tools_stay_read_only(self):
        body = text()
        self.assertIn("--allowedTools Read,Grep,Glob\n", body)
        self.assertIn("--disallowedTools Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch", body)
        self.assertIn("persist-credentials: false", body)


if __name__ == "__main__":
    unittest.main()
