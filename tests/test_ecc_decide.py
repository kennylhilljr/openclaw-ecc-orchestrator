"""bin/ecc-decide against a fake OpenClaw install.

The fake package has the layout bin/ecc-decide expects (an `openclaw`
executable whose real path sits next to dist/plugin-sdk/gateway-runtime.js).
Its callGatewayFromCli answers ecc-orchestrator.snapshot from FAKE_SNAPSHOT,
records every call to FAKE_CALLS, and refuses decide when FAKE_REFUSE is set.
Skipped without node 22 or newer.
"""

import json
import os
import subprocess
import tempfile
import time
import unittest

try:
    from .test_roundtrip_openclaw import ROOT, node_ok
except ImportError:  # discovered as a top level module
    from test_roundtrip_openclaw import ROOT, node_ok

SCRIPT = os.path.join(ROOT, "bin", "ecc-decide")
PLAN = "4f1f984b0ac9" + "0" * 52
FAKE_CLIENT = r"""
import fs from "node:fs";
export async function callGatewayFromCli(method, opts, params, client) {
  fs.appendFileSync(process.env.FAKE_CALLS, JSON.stringify({ method, opts, params, client }) + "\n");
  if (method === "ecc-orchestrator.snapshot") return JSON.parse(process.env.FAKE_SNAPSHOT);
  if (method === "ecc-orchestrator.decide") {
    if (process.env.FAKE_REFUSE) throw new Error("Refused (session_mismatch): decide from the requesting session");
    return { ok: true, request_id: params.requestId, decision: params.decision === "approve" ? "approved" : "rejected",
             decided_by: "gateway-owner" };
  }
  throw new Error("unexpected method " + method);
}
"""


def approval(request_id, unit="alpha"):
    return {"request_id": request_id, "run_id": "run1", "unit_id": unit, "action": "merge", "status": "pending",
            "plan_sha256": PLAN, "head_sha": "a" * 40, "requesting_session": "agent:main:main",
            "expires_at": time.time() + 3000, "summary": f"merge {unit} into main"}


@unittest.skipUnless(node_ok(), "node 22 or newer is required")
class EccDecideTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = os.path.realpath(self._tmp.name)
        pkg = os.path.join(tmp, "lib", "openclaw")
        os.makedirs(os.path.join(pkg, "dist", "plugin-sdk"))
        with open(os.path.join(pkg, "dist", "plugin-sdk", "gateway-runtime.js"), "w") as fh:
            fh.write(FAKE_CLIENT)
        with open(os.path.join(pkg, "package.json"), "w") as fh:
            json.dump({"name": "openclaw", "type": "module"}, fh)
        entry = os.path.join(pkg, "openclaw.mjs")
        with open(entry, "w") as fh:
            fh.write("#!/usr/bin/env node\n")
        os.chmod(entry, 0o755)
        bindir = os.path.join(tmp, "bin")
        os.makedirs(bindir)
        os.symlink(entry, os.path.join(bindir, "openclaw"))
        self.calls = os.path.join(tmp, "calls.jsonl")
        self.env = dict(os.environ, PATH=bindir + os.pathsep + os.environ.get("PATH", ""), FAKE_CALLS=self.calls)
        self.env.pop("FAKE_REFUSE", None)
        self.env.pop("ECC_GATEWAY_CLIENT", None)

    def tearDown(self):
        self._tmp.cleanup()

    def run_script(self, *args, pending=(), stdin="", refuse=False):
        env = dict(self.env, FAKE_SNAPSHOT=json.dumps({"pending_approvals": list(pending)}))
        if refuse:
            env["FAKE_REFUSE"] = "1"
        proc = subprocess.run([SCRIPT, *args], env=env, input=stdin, capture_output=True, text=True, timeout=120)
        return proc.returncode, proc.stdout, proc.stderr

    def decides(self):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls) as fh:
            calls = [json.loads(line) for line in fh if line.strip()]
        for c in calls:
            self.assertEqual(c["client"]["clientName"], "openclaw-tui")
            self.assertEqual(c["client"]["mode"], "ui")
            self.assertIn("operator.approvals", c["client"]["scopes"])
            self.assertEqual(c["opts"]["port"], "18789")
        return [c["params"] for c in calls if c["method"] == "ecc-orchestrator.decide"]

    def test_list(self):
        code, out, _ = self.run_script("list")
        self.assertEqual((code, out.strip()), (0, "No pending approval requests."))
        code, out, _ = self.run_script("list", pending=[approval("apr-1"), approval("apr-2", "beta")])
        self.assertEqual(code, 0)
        self.assertIn("2 pending approval request(s)", out)
        self.assertIn("merge of run1/beta", out)
        self.assertIn("plan      4f1f984b0ac9", out)
        self.assertEqual(self.decides(), [])

    def test_approve_the_only_pending_request(self):
        code, out, err = self.run_script("approve", "--yes", pending=[approval("apr-1")])
        self.assertEqual(code, 0, err)
        self.assertIn("by gateway-owner", out)
        self.assertEqual(self.decides(), [{"requestId": "apr-1", "decision": "approve",
                                           "sessionKey": "agent:main:main", "planHashPrefix": "4f1f984b0ac9"}])

    def test_reject_by_id_sends_no_plan_prefix(self):
        code, _, err = self.run_script("reject", "apr-2", "--yes", pending=[approval("apr-1"), approval("apr-2")])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.decides(), [{"requestId": "apr-2", "decision": "reject",
                                           "sessionKey": "agent:main:main"}])

    def test_confirmation_is_required(self):
        code, out, _ = self.run_script("approve", pending=[approval("apr-1")], stdin="no\n")
        self.assertEqual(code, 1)
        self.assertIn("Nothing decided.", out)
        self.assertEqual(self.decides(), [])
        code, _, err = self.run_script("approve", pending=[approval("apr-1")], stdin="yes\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.decides()), 1)

    def test_ambiguous_unknown_and_none(self):
        code, _, err = self.run_script("approve", "--yes", pending=[approval("apr-1"), approval("apr-2")])
        self.assertEqual(code, 2)
        self.assertIn("2 requests are pending", err)
        code, _, err = self.run_script("approve", "apr-9", "--yes", pending=[approval("apr-1")])
        self.assertEqual(code, 1)
        self.assertIn("not a pending request", err)
        code, _, err = self.run_script("approve", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("no pending approval requests", err)
        self.assertEqual(self.decides(), [])

    def test_refusal_and_usage(self):
        code, _, err = self.run_script("approve", "--yes", pending=[approval("apr-1")], refuse=True)
        self.assertEqual(code, 1)
        self.assertIn("refused: Refused (session_mismatch)", err)
        for bad in (("bogus",), ("approve", "apr 1"), ("list", "apr-1"), ()):
            code, _, _ = self.run_script(*bad)
            self.assertEqual(code, 2, bad)


if __name__ == "__main__":
    unittest.main()
