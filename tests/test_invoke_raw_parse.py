"""Live finding (rm-m0b, rm-m0d, rm-m0f; 2026-10-21 to 2026-10-23): 38 of 63 Claude reviewer
replies were read from the redacted log, where masking had broken the JSON result line. Each
became "no ECC_REVIEW line" and counted as changes requested. A CLI turn now parses the raw
stdout inside the process handle and keeps only redacted text."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from openclaw_ecc_orchestrator.process import supervisor as S
from openclaw_ecc_orchestrator.process.supervisor import Supervisor
from openclaw_ecc_orchestrator.runners.cli_adapters import ClaudeAdapter
from openclaw_ecc_orchestrator.team import invoke as I

SECRET = "sk-ant-api03-" + "Ab3dE5gH7jK9mN1pQ3sT5vX7zB9dF1hJ" * 2
REPLY = ("Reviewed.\nECC_REVIEW {\"verdict\": \"approved\", \"head\": \"2f6abdbb5c78\", "
         "\"findings\": [{\"severity\": \"minor\", \"what\": \"key " + SECRET + " in a fixture\"}]}")
RESULT = {"type": "result", "subtype": "success", "is_error": False, "session_id": "s-1",
          "total_cost_usd": 0.25, "usage": {"input_tokens": 8, "output_tokens": 2411,
                                            "output_tokens_details": {"thinking_tokens": 567}},
          "modelUsage": {"claude-opus": {}}, "result": REPLY}


class _Adapter(ClaudeAdapter):
    def __init__(self, stdout):
        super().__init__()
        self._stdout = stdout

    def build_invocation(self, prompt, model=None, cwd=None, writable=False):
        return [sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[1])", self._stdout]


def _turn(stdout):
    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "turn.log")
        turn = I.run_cli(_Adapter(stdout), "opus", "review", cwd=tmp, writable=False,
                         supervisor=Supervisor(), log_path=log, timeout=60)
        with open(log, encoding="utf-8") as fh:
            return turn, fh.read()


class RawParseTests(unittest.TestCase):
    def test_verdict_survives_a_log_redactor_that_breaks_json(self):
        broken = mock.patch.object(S.StreamRedactor, "feed", lambda self, line: line.replace('"', "'"))
        with broken:
            turn, log = _turn(json.dumps(RESULT, separators=(",", ":")) + "\n")
        self.assertIsNone(turn["error"])
        self.assertIn("ECC_REVIEW", turn["text"])
        self.assertEqual(turn["cost_usd"], 0.25)
        self.assertNotIn('"type":"result"', log)  # the log really was mangled

    def test_secrets_stay_out_of_the_text_and_the_log(self):
        turn, log = _turn(json.dumps(RESULT, separators=(",", ":")) + "\n")
        self.assertIn("ECC_REVIEW", turn["text"])
        self.assertNotIn(SECRET, turn["text"])
        self.assertNotIn(SECRET, log)

    def test_unparsable_output_is_a_runner_error_not_a_verdict(self):
        turn, _ = _turn("not json at all\n")
        self.assertIn("unparsable", turn["error"] or "")


if __name__ == "__main__":
    unittest.main()
