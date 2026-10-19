"""bin/roundtrip-smoke, with a simulated operator.

Runs the live smoke script against an isolated HOME (so the "live" state dir
is a temp dir), with a standing inbox processor subprocess, and plays the
operator through the plugin engine (plugin-driver.mjs) in the session the
script names. Checks both outcomes and that the script removes its temp dir.
Skipped without node 22 or newer.
"""

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest

try:
    from .test_roundtrip_openclaw import DRIVER, ROOT, SRC, node_ok
except ImportError:  # discovered as a top level module
    from test_roundtrip_openclaw import DRIVER, ROOT, SRC, node_ok

SMOKE = os.path.join(ROOT, "bin", "roundtrip-smoke")
WAIT_S = 120.0  # test harness bound only


@unittest.skipUnless(node_ok(), "node 22 or newer is required for the plugin engine")
class RoundTripSmokeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.home = os.path.join(self.tmp, "home")
        self.smoke_tmp = os.path.join(self.tmp, "t")
        os.makedirs(self.home)
        os.makedirs(self.smoke_tmp)
        self.state = os.path.join(self.home, ".local", "state", "openclaw-ecc-orchestrator")
        self.env = dict(os.environ, HOME=self.home, TMPDIR=self.smoke_tmp, ECC_PYTHON=sys.executable,
                        PYTHONPATH=SRC)
        self.env.pop("XDG_STATE_HOME", None)
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
                try:
                    p.communicate(timeout=30)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.communicate()
        self._tmp.cleanup()

    def spawn(self, cmd):
        p = subprocess.Popen(cmd, env=self.env, cwd=self.tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True)
        self.procs.append(p)
        return p

    def plugin(self, *args):
        out = subprocess.run(["node", DRIVER, os.path.join(self.state, "openclaw-events.jsonl"),
                              os.path.join(self.state, "decisions"), os.path.join(self.tmp, "plugin"), *args],
                             env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def run_smoke(self, decision):
        self.spawn([sys.executable, "-m", "openclaw_ecc_orchestrator", "approvals", "process-inbox", "--watch",
                    "--interval", "0.5", "--json"])
        smoke = self.spawn([SMOKE])
        lines, request_id, prefix = [], None, None
        deadline = time.monotonic() + WAIT_S
        while request_id is None and time.monotonic() < deadline:
            line = smoke.stdout.readline()
            if not line:
                break
            lines.append(line)
            m = re.search(r"/ecc-decide approve (\S+) ([0-9a-f]{12})", line)
            if m:
                request_id, prefix = m.groups()
        self.assertIsNotNone(request_id, "".join(lines))
        view = self.plugin("snapshot")
        self.assertEqual([a["request_id"] for a in view["pending_approvals"]], [request_id])
        res = self.plugin("decide", request_id, decision, prefix if decision == "approved" else "-",
                          "owner-profile-1", "agent:main:main")
        self.assertTrue(res["ok"], res)
        rest, _ = smoke.communicate(timeout=WAIT_S)
        return smoke.returncode, "".join(lines) + rest, self.plugin("snapshot")

    def test_approve(self):
        code, out, view = self.run_smoke("approved")
        self.assertEqual(code, 0, out)
        self.assertIn("PASS: approve round trip", out)
        self.assertEqual([u["state"] for r in view["runs"] for u in r["units"]], ["merged"])
        self.assertEqual(os.listdir(self.smoke_tmp), [], "the script removed its temporary directory")

    def test_reject(self):
        code, out, view = self.run_smoke("rejected")
        self.assertEqual(code, 0, out)
        self.assertIn("PASS: reject round trip", out)
        self.assertEqual(view["merges"], [])
        self.assertEqual(os.listdir(self.smoke_tmp), [])


if __name__ == "__main__":
    unittest.main()
