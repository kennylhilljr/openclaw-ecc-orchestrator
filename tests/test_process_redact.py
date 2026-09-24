import unittest

from openclaw_ecc_orchestrator.process.redact import Redactor, looks_secret_name, redact_obj


class RedactTests(unittest.TestCase):
    def test_patterns(self):
        r = Redactor()
        samples = {
            "token=abcdef123456": "abcdef123456",  # gitleaks:allow
            "API_KEY: 'qwertyuiop123'": "qwertyuiop123",  # gitleaks:allow
            "Authorization: Bearer abc.def.ghijklmnop": "abc.def.ghijklmnop",
            "push ghp_" + "a" * 36: "ghp_" + "a" * 36,
            "key sk-ant-" + "b" * 30: "sk-ant-" + "b" * 30,
            "aws AKIAABCDEFGHIJKLMNOP": "AKIAABCDEFGHIJKLMNOP",  # gitleaks:allow
            "https://user:hunter2pass@example.invalid/repo.git": "hunter2pass",
            "password = \"p a s s\"": "p a s s",
        }
        for text, secret in samples.items():
            out = r.redact(text)
            self.assertNotIn(secret, out, text)
            self.assertIn("[REDACTED]", out)

    def test_exact_secret_values(self):
        r = Redactor(secrets=["s3cr3t-value-xyz"])
        self.assertEqual(r.redact("got s3cr3t-value-xyz here"), "got [REDACTED] here")
        r.add_secret("ab")  # too short to redact safely, ignored
        self.assertEqual(r.redact("ab"), "ab")

    def test_plain_text_untouched(self):
        self.assertEqual(Redactor().redact("ran 12 tests in 0.3s OK"), "ran 12 tests in 0.3s OK")

    def test_secret_names(self):
        for name in ["GITHUB_TOKEN", "OPENAI_API_KEY", "DB_PASSWORD", "session_cookie", "AWS_SECRET_ACCESS_KEY"]:
            self.assertTrue(looks_secret_name(name), name)
        for name in ["PATH", "HOME", "LANG", "unit_id"]:
            self.assertFalse(looks_secret_name(name), name)

    def test_redact_obj(self):
        obj = {"api_token": "zzz", "nested": [{"msg": "token=abcdef123456"}], "n": 3}  # gitleaks:allow
        out = redact_obj(obj)
        self.assertEqual(out["api_token"], "[REDACTED]")
        self.assertNotIn("abcdef123456", out["nested"][0]["msg"])
        self.assertEqual(out["n"], 3)


if __name__ == "__main__":
    unittest.main()
