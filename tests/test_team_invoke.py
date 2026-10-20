"""API persona turns: reasoning models that spend the output budget thinking
(live finding, rmcp-m0f: kimi-k3 returned an empty reply on a large review pack)."""

import json
import os
import tempfile
import unittest

from openclaw_ecc_orchestrator.runners.api_adapters import KimiAdapter
from openclaw_ecc_orchestrator.runners.discovery import HttpResponse
from openclaw_ecc_orchestrator.team import invoke as I


def reply(content, finish, out_tokens):
    return HttpResponse(200, json.dumps({"model": "kimi-k3", "choices": [{"finish_reason": finish, "message": {
        "content": content, "reasoning_content": "thinking " * 10}}], "usage": {"completion_tokens": out_tokens}}))


class ApiTurnTests(unittest.TestCase):
    ENV = {"MOONSHOT_API_KEY": "k"}

    def test_empty_reply_cut_by_the_budget_is_retried_with_twice_the_budget(self):
        budgets = []

        def post(url, headers, body, timeout):
            budgets.append(body["max_tokens"])
            return reply("", "length", body["max_tokens"]) if len(budgets) == 1 else reply("ok", "stop", 50)

        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "turn.log")
            res = I.run_api(KimiAdapter(), "kimi-k3", "review this", env=self.ENV, http_post=post, log_path=log)
            self.assertEqual(res["text"], "ok")
            self.assertIsNone(res["error"])
            self.assertEqual(budgets, [I.API_MAX_OUTPUT_TOKENS, 2 * I.API_MAX_OUTPUT_TOKENS])
            with open(log) as fh:
                meta = json.loads(fh.readline())
            self.assertEqual((meta["provider"], meta["finish_reason"]), ("kimi", "stop"))
            self.assertGreater(meta["reasoning_chars"], 0)

    def test_empty_reply_reports_why(self):
        res = I.run_api(KimiAdapter(), "kimi-k3", "x", env=self.ENV,
                        http_post=lambda u, h, b, t: reply("", "stop", 0))
        self.assertIn("finish_reason stop", res["error"])

    def test_budget_is_large_enough_for_reasoning_models(self):
        self.assertGreaterEqual(I.API_MAX_OUTPUT_TOKENS, 32768)


if __name__ == "__main__":
    unittest.main()
