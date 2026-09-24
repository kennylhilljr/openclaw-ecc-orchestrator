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


if __name__ == "__main__":
    unittest.main()
