"""Regression tests for the single redaction engine (review finding 3)."""

import json
import os
import sys
import tempfile
import unittest

from openclaw_ecc_orchestrator.handoffs import redaction
from openclaw_ecc_orchestrator.handoffs.redaction import (REDACTED, Redactor, StreamRedactor,
                                                          contains_secret, redact_argv, redact_obj)
from openclaw_ecc_orchestrator.process import redact as process_redact
from openclaw_ecc_orchestrator.process.supervisor import Supervisor

PY = sys.executable

SAMPLES = {
    "groq": "gsk_" + "abcdefghijklmnopqrstuvwxyz0123456789ABCD",
    "google": "AIza" + "SyA1234567890abcdefghijklmnopqrstu",
    "openai": "sk-" + "orchestrator-parent-secret-123456",
    "anthropic": "sk-ant-" + "api03-" + "b" * 30,
    "openrouter": "sk-or-" + "v1-" + "0123456789abcdef" * 2,
    "github_classic": "ghp_" + "a" * 36,
    "github_oauth": "gho_" + "B" * 36,
    "github_pat": "github_pat_" + "11ABCDEFG0123456789_abcdefghijklmnop",
    "slack_bot": "xoxb-" + "123456789012-abcdefghijklmnop",
    "slack_export": "xoxe-" + "1-abcdefghijklmnop",
    "aws": "AKIA" + "ABCDEFGHIJKLMNOP",
    "jwt": "eyJhbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiIxMjM0NTY3ODkwIn0" + ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "high_entropy": "Xq7Lp2Zr9Vt4Mb8Nc1Kd6Hf3Jg5Ws0Ye",
}


class EnginePatternTests(unittest.TestCase):
    def test_every_pattern_detected_and_redacted(self):
        r = Redactor()
        for label, secret in SAMPLES.items():
            with self.subTest(label=label):
                text = f"export X={secret} trailing"
                self.assertTrue(contains_secret(f"value {secret} end"), label)
                for out in (r.redact(text), redaction.redact_text(text), redact_obj({"m": text})["m"]):
                    self.assertNotIn(secret, out)
                    self.assertIn(REDACTED, out)

    def test_headers(self):
        r = Redactor()
        for text, secret in [("Authorization: Bearer abc.def.ghijklmnop", "abc.def.ghijklmnop"),
                             ("authorization: token s3cr3tvalue", "s3cr3tvalue"),
                             ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
                             ("curl -H 'Authorization: Bearer zzzzzzzzzzzzzzzzzzzz'", "zzzzzzzzzzzzzzzzzzzz")]:
            with self.subTest(text=text):
                self.assertNotIn(secret, r.redact(text))

    def test_assignments_and_url_credentials(self):
        r = Redactor()
        for text, secret in [("MOONSHOT_KEY=mk-zzzzzzzz", "mk-zzzzzzzz"),
                             ("DB_PASS: hunter22", "hunter22"),
                             ('{"api_key": "abc123xyz"}', "abc123xyz"),
                             ("https://user:hunter2pass@example.invalid/x.git", "hunter2pass")]:
            with self.subTest(text=text):
                self.assertNotIn(secret, r.redact(text))

    def test_single_text_pem_block(self):
        pem = ("-----BEGIN OPENSSH PRIVATE KEY-----\n"  # gitleaks:allow
               "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
               "-----END OPENSSH PRIVATE KEY-----\nafter")
        out = Redactor().redact(pem)
        self.assertNotIn("b3BlbnNzaC1rZXkt", out)
        self.assertTrue(out.endswith("after"))

    def test_split_line_pem_stream(self):
        stream = StreamRedactor(Redactor())
        lines = ["before", "-----BEGIN RSA PRIVATE KEY-----", "MIIEowIBAAKCAQEAabc", "shortline", "zzz==",  # gitleaks:allow
                 "-----END RSA PRIVATE KEY-----", "after"]
        out = [stream.feed(line) for line in lines]
        self.assertEqual(out[0], "before")
        self.assertEqual(out[-1], "after")
        for line in out[1:-1]:
            self.assertEqual(line, REDACTED)
        self.assertFalse(stream.in_key_block)

    def test_benign_text_untouched(self):
        r = Redactor()
        for text in ("ran 12 tests in 0.3s OK", "PATH=/usr/bin:/bin", "git rev-parse HEAD",
                     "0123456789abcdef0123456789abcdef01234567"):
            self.assertEqual(r.redact(text), text)

    def test_absolute_paths_untouched(self):
        # A venv interpreter or macOS temp dir path used to be masked whole,
        # because "/" is a token character and the path mixes case and digits.
        r = Redactor()
        for text in ("/var/folders/6j/8_5x46rs3db2852wd7wb16v40000gn/T/venv-oc/bin/python",
                     "/Users/Dev1/Documents/GitHub/openclaw-ecc-orchestrator/.venv/bin/python3.11",
                     "/Users/Dev1/Projects/Orchestrator2/build/tmpAb3xY9z/worktrees/unit-a"):
            with self.subTest(text=text):
                self.assertFalse(contains_secret(text))
                self.assertEqual(r.redact(text), text)
                self.assertEqual(r.redact(f"cwd={text} ok"), f"cwd={text} ok")

    def test_secret_inside_path_or_with_slashes_still_redacted(self):
        r = Redactor()
        secret = SAMPLES["high_entropy"]
        out = r.redact(f"/tmp/cache/{secret}/file")
        self.assertNotIn(secret, out)
        self.assertTrue(out.startswith("/tmp/cache/"))
        aws_style = "Xq7Lp2Zr9Vt4/Mb8Nc1Kd6Hf3Jg5/Ws0YeR2uT8iO"
        self.assertTrue(contains_secret(f"key {aws_style} end"))
        self.assertNotIn(aws_style, r.redact(f"key {aws_style} end"))

    def test_literal_secret_registration(self):
        r = Redactor()
        r.add_secret("plain-looking-value")
        self.assertEqual(r.redact("x plain-looking-value y"), f"x {REDACTED} y")
        r.add_secret("ab")
        self.assertEqual(r.redact("ab"), "ab")

    def test_process_redact_is_reexport(self):
        self.assertIs(process_redact.Redactor, Redactor)
        self.assertIs(process_redact.redact_obj, redact_obj)
        self.assertIs(process_redact.looks_secret_name, redaction.looks_secret_name)


class JsonLineTests(unittest.TestCase):
    """Live finding (rm-m0b M0-02, 2026-10-22): masking "session_id": "<uuid>" together with its quotes, and
    "thinking_tokens": null as a value, left Claude's result line invalid JSON in the log. The verdict
    inside it was then reported as "no ECC_VERIFY line" and its cost as $0.00."""

    RESULT = {"type": "result", "subtype": "success", "is_error": False, "stop_reason": "end_turn",
              "session_id": "0f9c2d1e-7a3b-4c5d-9e8f-123456789abc", "total_cost_usd": 0.4473,
              "usage": {"input_tokens": 24, "output_tokens": 9550,
                        "output_tokens_details": {"thinking_tokens": None}},
              "modelUsage": {"claude-opus": {}},
              "result": 'Checked.\nECC_VERIFY {"verdict": "pass", "head": "2f6abdbb5c78", "checks": []}'}

    def test_redacted_result_line_stays_json_and_parses(self):
        from openclaw_ecc_orchestrator.runners.cli_adapters import ClaudeAdapter
        line = json.dumps(self.RESULT, separators=(",", ":"))
        for out in (Redactor().redact(line), redaction.redact_text(line)):
            doc = json.loads(out)
            self.assertNotIn(self.RESULT["session_id"], out)
            self.assertEqual(doc["session_id"], REDACTED)
            parsed = ClaudeAdapter().parse_output(out)
            self.assertIn("ECC_VERIFY", parsed["text"])
            self.assertEqual(parsed["cost_usd"], 0.4473)

    def test_counter_closing_an_object_stays_json(self):
        """Live finding (rm-m0d plan check, 2026-10-23): "thinking_tokens":15327} was masked with its
        brace, the result line stopped being JSON and the plan check read "no ECC_REVIEW line"."""
        from openclaw_ecc_orchestrator.runners.cli_adapters import ClaudeAdapter
        result = dict(self.RESULT, usage={"output_tokens": 15327,
                                          "output_tokens_details": {"thinking_tokens": 15327}},
                      result='Checked.\nECC_REVIEW {"verdict": "approved", "head": "2f6abdbb5c78", "findings": []}')
        line = json.dumps(result, separators=(",", ":"))
        for out in (Redactor().redact(line), redaction.redact_text(line)):
            doc = json.loads(out)
            self.assertEqual(doc["usage"]["output_tokens_details"], {"thinking_tokens": 15327})
            self.assertIn("ECC_REVIEW", ClaudeAdapter().parse_output(out)["text"])

    def test_escaped_quotes_inside_a_json_string_stay_escaped(self):
        """Live finding (rm-m0f M0-20, 2026-10-23): inside Claude's result string,
        `SecretState.INVALID = \\"invalid\\"` and `test_secrets.py:62\\"` lost the backslash that
        escapes a quote, the line stopped being JSON and six reviews read "no ECC_REVIEW line"."""
        from openclaw_ecc_orchestrator.runners.cli_adapters import ClaudeAdapter
        text = ('`SecretState.INVALID = "invalid"` matches the LLD.\n'
                'ECC_REVIEW {"verdict": "approved", "head": "2f6abdbb5c78", "findings": [{"severity": "minor", '
                '"where": "tests/unit/platform/test_secrets.py:62", "what": "order", "api_key": "abc123xyz"}]}')
        line = json.dumps(dict(self.RESULT, result=text), separators=(",", ":"))
        for out in (Redactor().redact(line), redaction.redact_text(line)):
            doc = json.loads(out)
            self.assertNotIn("abc123xyz", out)
            self.assertIn("ECC_REVIEW", ClaudeAdapter().parse_output(out)["text"])
            self.assertEqual(doc["result"].splitlines()[-1][:11], "ECC_REVIEW ")

    def test_quoted_secret_value_is_masked_inside_its_quotes(self):
        out = Redactor().redact('{"api_key": "abc123xyz", "n": 1}')
        self.assertNotIn("abc123xyz", out)
        self.assertEqual(json.loads(out), {"api_key": REDACTED, "n": 1})


class ArgvRedactionTests(unittest.TestCase):
    def test_secret_flags(self):
        argv = ["tool", "--token", "hunter2hunter2xyz", "--password", "Sup3rS3cretPass", "--api-key=abcdef1",
                "--secret", "s", "-p", "mysqlpw", "--client-secret=zz", "--verbose", "keep"]
        out = redact_argv(argv)
        for secret in ("hunter2hunter2xyz", "Sup3rS3cretPass", "abcdef1", "mysqlpw"):
            self.assertNotIn(secret, " ".join(out))
        self.assertEqual(out[0], "tool")
        self.assertIn("--api-key=" + REDACTED, out)
        self.assertEqual(out[-2:], ["--verbose", "keep"])
        self.assertEqual(out[7], REDACTED)

    def test_p_followed_by_option_is_kept(self):
        out = redact_argv(["claude", "-p", "--output-format", "json", "--", "hello"])
        self.assertEqual(out, ["claude", "-p", "--output-format", "json", "--", "hello"])

    def test_env_assignments(self):
        out = redact_argv(["env", "MOONSHOT_KEY=abc123456", "GITHUB_TOKEN=x", "LANG=C", "cmd"])
        self.assertEqual(out, ["env", "MOONSHOT_KEY=" + REDACTED, "GITHUB_TOKEN=" + REDACTED, "LANG=C", "cmd"])

    def test_literal_secrets_in_argv(self):
        r = Redactor(["literal-value-42"])
        self.assertEqual(redact_argv(["x", "--msg=literal-value-42"], r), ["x", "--msg=" + REDACTED])


class SupervisorRedactionTests(unittest.TestCase):
    SCRIPT = (
        "import os\n"
        "print('-----BEGIN OPENSSH PRIVATE KEY-----')\n"  # gitleaks:allow
        "print('b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW')\n"
        "print('-----END OPENSSH PRIVATE KEY-----')\n"
        "print('export GROQ_KEY=' + 'gsk_' + 'abcdefghijklmnopqrstuvwxyz0123456789ABCD')\n"
        "print('AIza' + 'SyA1234567890abcdefghijklmnopqrstu')\n"
        "print('Authorization: token ghp_' + 'a' * 36)\n"
        "print('key ' + os.environ.get('MOONSHOT_KEY', 'none'))\n"
        "print('done')\n"
    )
    SECRETS = ("b3BlbnNzaC1rZXkt", "gsk_abcdefghij", "AIzaSyA123", "ghp_aaaa", "mk-zzzzzzzzzzzzzzzzzzzz",
               "hunter2hunter2xyz", "Sup3rS3cretPass")

    def test_logs_status_and_callback_are_clean(self):
        with tempfile.TemporaryDirectory() as d:
            seen = []
            sup = Supervisor(base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
            res = sup.run([PY, "-c", self.SCRIPT, "--token", "hunter2hunter2xyz", "--password", "Sup3rS3cretPass"],
                          cwd=d, extra_env={"MOONSHOT_KEY": "mk-zzzzzzzzzzzzzzzzzzzz"},
                          log_path=os.path.join(d, "log.txt"), status_path=os.path.join(d, "status.json"),
                          on_line=lambda s, l: seen.append(l))
            self.assertEqual(res["exit_code"], 0, res)
            with open(os.path.join(d, "log.txt")) as fh:
                log = fh.read()
            with open(os.path.join(d, "status.json")) as fh:
                status = fh.read()
            self.assertIn("done", log)
            for blob in (log, status, json.dumps(res), "\n".join(seen)):
                for secret in self.SECRETS:
                    self.assertNotIn(secret, blob)
            self.assertIn("--token", json.loads(status)["argv"])

    def test_extra_env_value_registered_regardless_of_name(self):
        with tempfile.TemporaryDirectory() as d:
            seen = []
            sup = Supervisor(base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
            res = sup.run([PY, "-c", "import os; print('v=' + os.environ['PLAIN_NAME'])"], cwd=d,
                          extra_env={"PLAIN_NAME": "not-obviously-secret-value"},
                          on_line=lambda s, l: seen.append(l))
            self.assertEqual(res["exit_code"], 0)
            self.assertEqual(seen, ["v=" + REDACTED])
            # The registration lives only as long as that process's redactor.
            self.assertNotIn("not-obviously-secret-value", sup.redactor.secrets)

    def test_spawn_error_never_echoes_secret(self):
        with tempfile.TemporaryDirectory() as d:
            sup = Supervisor(base_env={"PATH": "/usr/bin:/bin"})
            secret = "gsk_" + "Q1w2E3r4T5y6U7i8O9p0A1s2D3f4G5h6"
            res = sup.run([os.path.join(d, secret)], cwd=d, status_path=os.path.join(d, "s.json"))
            self.assertTrue(res["error"])
            with open(os.path.join(d, "s.json")) as fh:
                self.assertNotIn(secret, fh.read())
            self.assertNotIn(secret, json.dumps(res))


class BranchNameTests(unittest.TestCase):
    """Orchestrator unit branches are judged segment by segment (smoke run
    smoke-20261019T005138Z failed handoff_valid with secret_detected on
    commit.branch before this rule)."""

    def test_timestamped_branches_are_not_secrets(self):
        flagged = []
        for hour in range(24):
            for minute in range(60):
                for second in range(0, 60, 7):
                    branch = f"ecc/smoke-20261019T{hour:02d}{minute:02d}{second:02d}Z/smoke"
                    if contains_secret(branch):
                        flagged.append(branch)
        self.assertEqual(flagged, [])
        for form in ("refs/heads/ecc/smoke-20261019T005138Z/smoke", "branch=ecc/smoke-20261019T005138Z/smoke",
                     "ecc/run-20261019-homesentinel-m0/settings-loader"):
            self.assertFalse(contains_secret(form), form)
        self.assertEqual(redaction.redact_text("pushed ecc/smoke-20261019T005138Z/smoke ok"),
                         "pushed ecc/smoke-20261019T005138Z/smoke ok")

    def test_random_segments_inside_a_branch_are_still_secrets(self):
        key = SAMPLES["high_entropy"] + "Ab12"
        for text in ("ecc/run1/" + key, "ecc/" + key + "/u1", "ecc/a/b" + key):
            self.assertTrue(contains_secret(text), text)
            self.assertIn(REDACTED, redaction.redact_text(text))
        for name, value in SAMPLES.items():
            self.assertTrue(contains_secret("ecc/run1/u1 " + value), name)

    def test_handoff_with_timestamped_branch_validates(self):
        from openclaw_ecc_orchestrator.handoffs.redaction import scan_strings
        doc = {"commit": {"sha": "dba11d66970f4aed210c325c27a4116711c3a613",
                          "branch": "ecc/smoke-20261019T005138Z/smoke", "worktree": "."}}
        self.assertEqual(scan_strings(doc), [])


class CounterKeyTests(unittest.TestCase):
    """Long camelCase usage counter keys with numeric values are not secrets."""

    # Built at runtime so the source holds no literal random token.
    FAKE = "".join("aZ9kQ7mX2pL4vR8tW1yB6nC3dF5gH0jK"[i % 32] for i in range(40))

    def result_line(self, extra=""):
        usage = {"inputTokens": 2, "cacheCreationEphemeral5mInputTokens": 0,
                 "cacheCreationEphemeral1hInputTokens": 60157}
        line = json.dumps({"type": "result", "modelUsage": {"claude-haiku-5-5": usage}})
        return line[:-1] + extra + "}"

    def test_counter_keys_are_not_findings(self):
        self.assertEqual(redaction.inspection_findings([self.result_line()]), [])

    def test_counter_keys_stay_visible_when_redacting(self):
        text = self.result_line()
        self.assertEqual(redaction.redact_text(text), text)

    def test_escaped_json_counter_key(self):
        text = json.dumps(self.result_line())  # the line inside a JSON string
        self.assertEqual(redaction.inspection_findings([text]), [])

    def test_random_string_value_still_caught(self):
        text = self.result_line(', "blob": "%s"' % self.FAKE)
        kinds = [f["kind"] for f in redaction.inspection_findings([text])]
        self.assertIn("high_entropy", kinds)

    def test_random_key_with_number_still_caught(self):
        text = '{"%s": 1}' % self.FAKE
        kinds = [f["kind"] for f in redaction.inspection_findings([text])]
        self.assertIn("high_entropy", kinds)

    def test_digit_separated_key_still_caught(self):
        # Splits into camelCase-like pieces, but its many digits mark it random.
        key = "".join("%s%d%s" % (c, i % 10, c.upper()) for i, c in enumerate("bcdfghjkmnpqr"))
        text = '{"%s": 1}' % key
        kinds = [f["kind"] for f in redaction.inspection_findings([text])]
        self.assertIn("high_entropy", kinds)

    def test_double_escaped_counter_key(self):
        text = json.dumps(json.dumps(self.result_line()))
        self.assertEqual(redaction.inspection_findings([text]), [])

    def test_camel_key_with_string_value_still_caught(self):
        text = '{"cacheCreationEphemeral1hInputTokens": "x"}'
        kinds = [f["kind"] for f in redaction.inspection_findings([text])]
        self.assertIn("high_entropy", kinds)


class DottedTempPathTests(unittest.TestCase):
    """mktemp names such as /tmp/ecc-check.Ab3Xy9 split at the dot (issue 57)."""

    FAKE = "Xq7Lp2Zr9Vt4Mb8Nc1Kd6Hf3Jg5Ws0Ye"
    ROOTS = ("/tmp/ecc-check.Ab3Xy9", "/private/tmp/ecc-check.Ab3Xy9")
    TAILS = ("/xdg-state/openclaw-ecc-orchestrator",
             "/xdg-state/openclaw-ecc-orchestrator/backups/backup-20261117T120000Z",
             "/xdg-state/openclaw-ecc-orchestrator/archive/run-20261117T120000Z.tar.gz",
             "/gates/unit-implementation-01/output.log")

    def benign_forms(self):
        for root in self.ROOTS:
            for tail in self.TAILS:
                path = root + tail
                yield path
                yield "state_dir=" + path
                yield json.dumps({"state_dir": path})
                yield json.dumps(json.dumps({"output_tail": "wrote " + path + "\n"}))

    def test_dotted_temp_paths_are_not_secrets(self):
        r = Redactor()
        for text in self.benign_forms():
            with self.subTest(text=text):
                self.assertFalse(contains_secret(text))
                self.assertEqual(r.redact(text), text)

    def test_repeated_dotted_directories(self):
        text = "/tmp/ecc-check.Ab3Xy9/sub.Qm4Zt8/more.Wv7Kp2/xdg-state/openclaw-ecc-orchestrator"
        self.assertFalse(contains_secret(text))
        self.assertEqual(Redactor().redact(text), text)

    def test_random_segment_after_dot_still_caught(self):
        for text in ("/tmp/ecc-check.%s" % self.FAKE,
                     "/tmp/ecc-check.Ab3Xy9/%s/x" % self.FAKE,
                     "/tmp/ecc-check.Ab3Xy9/sub.%s/x" % self.FAKE):
            with self.subTest(text=text):
                self.assertTrue(contains_secret(text))
                self.assertNotIn(self.FAKE, Redactor().redact(text))

    def test_known_credentials_inside_dotted_paths_still_caught(self):
        for label, secret in SAMPLES.items():
            text = "/tmp/ecc-check.Ab3Xy9/%s/file" % secret
            with self.subTest(label=label):
                self.assertTrue(contains_secret(text))
                self.assertNotIn(secret, Redactor().redact(text))

    def test_registered_literal_inside_dotted_path_still_masked(self):
        r = Redactor()
        r.add_secret("hunter2-literal")
        out = r.redact("/tmp/ecc-check.Ab3Xy9/hunter2-literal/x")
        self.assertNotIn("hunter2-literal", out)

    def test_unrelated_dotted_text_is_not_exempt(self):
        secret = "Xq7Lp2Zr9Vt4/Mb8Nc1Kd6Hf3Jg5Ws0Ye/Ab3Xy9"  # gitleaks:allow (fake)
        for text in ("config.%s" % secret,
                     "relative/dir.%s" % secret,
                     "foo/tmp/x.%s" % secret):
            with self.subTest(text=text):
                self.assertTrue(contains_secret(text))
                self.assertNotIn(secret, Redactor().redact(text))

    def test_whitespace_and_quotes_break_path_context(self):
        secret = "Xq7Lp2Zr9Vt4/Mb8Nc1Kd6Hf3Jg5Ws0Ye/Ab3Xy9"  # gitleaks:allow (fake)
        for text in ("/tmp/ecc-check.Ab3Xy9 other.%s" % secret,
                     "/tmp/ecc-check.Ab3Xy9\tother.%s" % secret,
                     '"/tmp/ecc-check.Ab3Xy9" "x.%s"' % secret,
                     "'/tmp/a' b.%s" % secret,
                     "/tmp/a=b.%s" % secret,
                     "/tmp/a:b.%s" % secret):
            with self.subTest(text=text):
                self.assertTrue(contains_secret(text))
                self.assertNotIn(secret, Redactor().redact(text))


class PathSegmentWindowTests(unittest.TestCase):
    """Slash split credentials under a path root (issue 64, follow-up to issue 57)."""

    P1 = "Xq7Lp2Zr9Vt4"  # gitleaks:allow (fake)
    P2 = "Mb8Nc1Kd6Hf3Jg5"  # gitleaks:allow (fake)
    P3 = "Ws0YeR2uT8iO"  # gitleaks:allow (fake)
    CRED = P1 + "/" + P2 + "/" + P3
    # First two pieces reach 32 characters; the suffix cannot form its own window.
    SPLIT = "Xq7Lp2Zr9Vt4/Mb8Nc1Kd6Hf3Jg5Ws0Ye/Ab3Xy9"  # gitleaks:allow (fake)
    SPLIT_PIECES = ["Xq7Lp2Zr9Vt4", "Mb8Nc1Kd6Hf3Jg5Ws0Ye", "Ab3Xy9"]

    def cases(self):
        p1, p2, p3, cred = self.P1, self.P2, self.P3, self.CRED
        return [
            ("/tmp/" + cred, [p1, p2, p3]),
            ("/tmp/." + cred, [p1, p2, p3]),
            ("/tmp/Xq7Lp2Zr9Vt4/Mb8/Nc1Kd6Hf3Jg5/Ws0YeR2uT8iO",  # gitleaks:allow (fake)
             ["Xq7Lp2Zr9Vt4", "Mb8", "Nc1Kd6Hf3Jg5", "Ws0YeR2uT8iO"]),
            ("/tmp/" + cred + "/" + "A" * 100, [p1, p2, p3]),
            ("/tmp/" + cred + "/" + "Ab" * 50, [p1, p2, p3]),
            ("/tmp/" + "A" * 100 + "/" + cred, [p1, p2, p3]),
            ("/tmp/ecc-check.Ab3Xy9/" + cred + "/file", [p1, p2, p3]),
            ("/Users/dev/Xq7Lp2Zr9Vt4Mb8Nc1/Kd6Hf3Jg5Ws0YeR2u/x",  # gitleaks:allow (fake)
             ["Xq7Lp2Zr9Vt4Mb8Nc1", "Kd6Hf3Jg5Ws0YeR2u"]),
            ("cwd=/private/var/" + cred, [p1, p2, p3]),
            ("/tmp/" + self.SPLIT, self.SPLIT_PIECES),
            ("/tmp/." + self.SPLIT, self.SPLIT_PIECES),
            ("/tmp/ecc-check." + self.SPLIT, self.SPLIT_PIECES),
            ("/tmp/ecc-check.Ab3Xy9/" + self.SPLIT + "/file", self.SPLIT_PIECES),
            # A short piece at either end of the credential.
            ("/tmp/Mb8/Xq7Lp2Zr9Vt4Ab/Nc1Kd6Hf3Jg5Ws",  # gitleaks:allow (fake)
             ["Mb8", "Xq7Lp2Zr9Vt4Ab", "Nc1Kd6Hf3Jg5Ws"]),
            ("/tmp/Xq7Lp2Zr9Vt4Ab/Nc1Kd6Hf3Jg5Ws/Mb8",  # gitleaks:allow (fake)
             ["Mb8", "Xq7Lp2Zr9Vt4Ab", "Nc1Kd6Hf3Jg5Ws"]),
            # Only the full three piece window is random enough.
            ("/tmp/Ab1Ab1Ab1Ab1Ab1A/Cd2Cd2Cd2Cd2Cd2C/Ef3Gh4Ij5Kl6Mn7Op",  # gitleaks:allow (fake)
             ["Ab1Ab1Ab1Ab1Ab1A", "Cd2Cd2Cd2Cd2Cd2C", "Ef3Gh4Ij5Kl6Mn7Op"]),
            # A longer piece without mixed case between two strong pieces.
            ("/tmp/Xq7Lp2Zr9Vt4/mb8n/Nc1Kd6Hf3Jg5Ws0YeR2uT8iO",  # gitleaks:allow (fake)
             ["Xq7Lp2Zr9Vt4", "mb8n", "Nc1Kd6Hf3Jg5Ws0YeR2uT8iO"]),
            ("/tmp/Xq7Lp2Zr9Vt4/MB8N/Nc1Kd6Hf3Jg5Ws0YeR2uT8iO",  # gitleaks:allow (fake)
             ["Xq7Lp2Zr9Vt4", "MB8N", "Nc1Kd6Hf3Jg5Ws0YeR2uT8iO"]),
        ]

    def test_slash_split_credentials_are_caught(self):
        for text, pieces in self.cases():
            with self.subTest(text=text):
                self.assertTrue(contains_secret(text))
                for out in (Redactor().redact(text), redaction.redact_text(text)):
                    for piece in pieces:
                        self.assertNotIn(piece, out)
                    self.assertIn(REDACTED, out)
                kinds = [f["kind"] for f in redaction.inspection_findings([text])]
                self.assertIn("high_entropy", kinds)

    def test_benign_paths_stay_visible(self):
        for text in ("/private/var/folders/6j/8_5x46rs3db2852wd7wb16v40000gn/T/tmpabc",
                     "/private/var/folders/6j/8x5x46rs3db2852wd7wb16v40000gn/T/tmpAb3xY9z",
                     "/var/folders/6j/8_5x46rs3db2852wd7wb16v40000gn/T/venv-oc/bin/python",
                     "/Users/Dev1/Documents/GitHub/openclaw-ecc-orchestrator/.venv/bin/python3.11",
                     "/Users/Dev1/Projects/Orchestrator2/build/tmpAb3xY9z/worktrees/unit-a",
                     "/Users/JaneDoe/Documents/GitHub/MyProject2/src/main",
                     "/tmp/ecc-check.Ab3Xy9/sub.Qm4Zt8/more.Wv7Kp2/xdg-state/openclaw-ecc-orchestrator",
                     "/tmp/x/20261117T120000Z/20261117T130000Z/20261117T140000Z",
                     "/home/user/.cache/pip/http/d/4/e/a/"
                     "0123456789abcdef0123456789abcdef0123456789abcdef",
                     # Root names are not credential pieces.
                     "/tmp/Xq7Lp2Zr9Vt4Mb8Nc1Kd6Hf3Jg5W",  # gitleaks:allow (fake)
                     "/private/var/Xq7Lp2Zr9Vt4Mb8Nc1Kd6Hf3Jg5W",  # gitleaks:allow (fake)
                     "/usr/lib/Xq7Lp2Zr9Vt4Mb8Nc1Kd6Hf3Jg5W/bin",  # gitleaks:allow (fake)
                     # Acronym heavy directory names.
                     "/Users/Dev1/GitHub/JSON2XML/CLI3/ARM64/SDK",
                     "/opt/LLVM17/AArch64/X86/MCJIT/IR/SDKs/iOS17",
                     "/tmp/x/20261117T120000Z/ab/20261117T130000Z/Ab3Xy9"):
            with self.subTest(text=text):
                self.assertFalse(contains_secret(text))
                self.assertEqual(Redactor().redact(text), text)
                self.assertEqual(redaction.redact_text("cwd=" + text + " ok"), "cwd=" + text + " ok")
                self.assertEqual(redaction.inspection_findings([text]), [])

if __name__ == "__main__":
    unittest.main()
