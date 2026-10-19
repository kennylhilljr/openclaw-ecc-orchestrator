"""Round trip across every real component except the OpenClaw gateway itself.

    operator CLI (create-run, dispatch, review)        -> event log
    plugin engine in Node (snapshot, decide)            <- event log, -> decision inbox
    standing inbox processor (subprocess, --watch)      <- inbox, -> approvals + approval.resolved
    operator CLI (merge)                                <- approval
    plugin engine (snapshot)                            <- approval.resolved, merge.completed

The plugin is driven through tests/roundtrip/plugin-driver.mjs, which calls
the same engine.decide and snapshot() the gateway uses, as a separate
process per call (each call is also a plugin restart on the same state).
The gateway layer itself is covered by
integrations/openclaw-plugin/tests/integration/isolated-gateway.sh, and the
live Mac setup by bin/roundtrip-smoke.

Skipped when node 22 or newer is not on PATH.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import unittest

from openclaw_ecc_orchestrator.plugin.approvals import ApprovalBroker
from openclaw_ecc_orchestrator.plugin.inbox import probe_processor_lock, processor_lock_path

try:
    from . import _integration as H
    from . import test_cli_support as S
except ImportError:  # discovered as a top level module
    import _integration as H
    import test_cli_support as S

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
DRIVER = os.path.join(ROOT, "integrations", "openclaw-plugin", "tests", "roundtrip", "plugin-driver.mjs")
FAKE_TOKEN = "gh" + "p_" + "Rt8Wv7Ut6Sr5Qp4On3Ml2Kj1Ih0Gf9Ed"
WAIT_S = 60.0  # bound for the test harness only; the runtime itself has no such limit


def node_ok():
    node = shutil.which("node")
    if not node:
        return False
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=30).stdout
        return int(out.strip().lstrip("v").split(".")[0]) >= 22
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


@unittest.skipUnless(node_ok(), "node 22 or newer is required for the plugin engine")
class OpenClawRoundTripTest(S.CliTestBase):
    def setUp(self):
        super().setUp()
        # The fake runner also prints a credential shaped string, which must
        # never reach the event log or the plugin's view.
        with open(self.runner, "w") as fh:
            fh.write(H.RUNNER.replace(
                'print("start", mode, flush=True)',
                f'print("start", mode, flush=True)\nprint("auth with {FAKE_TOKEN}", flush=True)', 1))
        self.inbox = os.path.join(self.state, "decisions")
        self.plugin_state = os.path.join(self.tmp, "plugin-state")
        self.watcher = None

    def tearDown(self):
        self.stop_processor()
        super().tearDown()

    # == plugin (Node) ==
    def plugin(self, *args):
        proc = subprocess.run(["node", DRIVER, self.events_path, self.inbox, self.plugin_state, *args],
                              capture_output=True, text=True, timeout=120,
                              env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.home})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def view(self):
        return self.plugin("snapshot")

    def decide(self, request_id, decision, prefix="-", decided_by="owner-profile-1", session=S.SESSION):
        return self.plugin("decide", request_id, decision, prefix, decided_by, session)

    @staticmethod
    def approval(view, request_id):
        return next(a for a in view["approvals"] if a["request_id"] == request_id)

    @staticmethod
    def unit_view(view, unit_id, run_id="run1"):
        run = next(r for r in view["runs"] if r["run_id"] == run_id)
        return next(u for u in run["units"] if u["unit_id"] == unit_id)

    # == standing processor (subprocess) ==
    def start_processor(self):
        cmd = [sys.executable, "-m", "openclaw_ecc_orchestrator", "approvals", "process-inbox", "--watch",
               "--interval", "0.5", "--json", "--config", self.config_path]
        env = dict(os.environ, PYTHONPATH=SRC, HOME=self.home)
        self.watcher = subprocess.Popen(cmd, env=env, cwd=self.tmp, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True)
        lock = processor_lock_path(self.state, self.inbox)
        self.wait_for(lambda: self.watcher.poll() is not None or probe_processor_lock(lock)["held"], "processor start")
        self.assertIsNone(self.watcher.poll(), "processor exited early")

    def stop_processor(self):
        if not self.watcher:
            return ""
        proc, self.watcher = self.watcher, None
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        try:
            out, _ = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        return out

    def wait_for(self, predicate, what):
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        self.fail(f"timed out waiting for {what}")

    def runtime_status(self, request_id):
        """The runtime's own record (approvals list shows pending ones only)."""
        req = ApprovalBroker(os.path.join(self.state, "approvals.json")).get(request_id)
        return req["status"] if req else None

    def wait_resolved(self, request_id):
        self.wait_for(lambda: self.runtime_status(request_id) not in (None, "pending"), "approval.resolved")
        return self.runtime_status(request_id)

    def inbox_files(self):
        if not os.path.isdir(self.inbox):
            return []
        return sorted(n for n in os.listdir(self.inbox) if n != "processed" and not n.startswith("."))

    # == cases ==
    def test_approve_round_trip_through_standing_processor(self):
        base = S.git(self.repo, "rev-parse", "main")
        head, request_id = self.to_review()

        view = self.view()
        pending = self.approval(view, request_id)
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["head_sha"], head, "head sha reaches the plugin verbatim")
        self.assertEqual(pending["requesting_session"], S.SESSION)
        self.assertEqual(self.unit_view(view, "alpha")["state"], "reviewing")
        self.assertEqual([a["request_id"] for a in view["pending_approvals"]], [request_id])

        self.start_processor()
        res = self.decide(request_id, "approved", pending["plan_sha256"][:12])
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.wait_resolved(request_id), "approved")
        # The processor resolves first and moves the file after (crash safety).
        self.wait_for(lambda: self.inbox_files() == [], "the processor to consume the decision")

        after = self.approval(self.view(), request_id)
        self.assertEqual(after["status"], "approved")
        self.assertEqual(after["decided_by"], "owner-profile-1")
        self.assertEqual(after["runtime_result"], {"ok": True, "failed": []})

        doc = self.ok(self.cli("merge", "--run-id", self.RUN_ID, "--unit", "alpha"))
        merged = doc["data"]["result"]["merged_commit"]
        self.assertEqual(S.git(self.repo, "rev-parse", "main"), merged)
        self.assertNotEqual(merged, base)

        final = self.view()
        self.assertEqual(self.unit_view(final, "alpha")["state"], "merged")
        self.assertEqual(self.unit_view(final, "beta")["state"], "ready")
        self.assertEqual(final["pending_approvals"], [])
        self.assertTrue(any(m.get("merged_commit") == merged for m in final["merges"]), final["merges"])
        resolved = [e for e in self.events() if e["type"] == "approval.resolved"]
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["data"]["decided_by"], "owner-profile-1")

        # Secret hygiene end to end: the runner printed a token.
        with open(self.events_path) as fh:
            log = fh.read()
        self.assertIn("auth with [REDACTED]", log, "the token line was emitted (redacted), so this check is not vacuous")
        self.assertNotIn(FAKE_TOKEN, log)
        self.assertNotIn(FAKE_TOKEN, json.dumps(final))
        for name in os.listdir(self.plugin_state):
            with open(os.path.join(self.plugin_state, name)) as fh:
                self.assertNotIn(FAKE_TOKEN, fh.read())

    def test_reject_round_trip_keeps_main_unchanged(self):
        base = S.git(self.repo, "rev-parse", "main")
        _, request_id = self.to_review()
        self.start_processor()
        self.assertTrue(self.decide(request_id, "rejected")["ok"], "reject needs no plan prefix")
        self.assertEqual(self.wait_resolved(request_id), "rejected")

        code, _ = self.cli("merge", "--run-id", self.RUN_ID, "--unit", "alpha")
        self.assertNotEqual(code, 0, "a rejected approval never admits the merge")
        self.assertEqual(S.git(self.repo, "rev-parse", "main"), base)
        view = self.view()
        self.assertEqual(self.approval(view, request_id)["status"], "rejected")
        self.assertNotEqual(self.unit_view(view, "alpha")["state"], "merged")
        self.assertEqual(view["merges"], [])

    def test_decision_waits_in_the_inbox_until_the_processor_starts(self):
        _, request_id = self.to_review()
        prefix = self.approval(self.view(), request_id)["plan_sha256"][:8]
        res = self.decide(request_id, "approved", prefix)
        self.assertTrue(res["ok"], res)
        # No processor yet: the decision is queued, nothing is applied.
        self.assertEqual(self.inbox_files(), [res["file"]])
        self.assertEqual(self.runtime_status(request_id), "pending")
        self.assertEqual(self.approval(self.view(), request_id)["status"], "pending")
        # Starting the processor later applies the backlog.
        self.start_processor()
        self.assertEqual(self.wait_resolved(request_id), "approved")
        self.wait_for(lambda: self.inbox_files() == [], "the processor to consume the backlog")

    def test_plugin_refusals_write_nothing(self):
        _, request_id = self.to_review()
        plan = self.approval(self.view(), request_id)["plan_sha256"]
        cases = [
            (("approved", plan[:12], "owner-profile-1", "sess-intruder"), "session_mismatch"),
            (("approved", "-", "owner-profile-1", S.SESSION), "plan_confirmation_required"),
            (("approved", plan[:6], "owner-profile-1", S.SESSION), "plan_confirmation_required"),
            (("approved", ("0" if plan[0] != "0" else "1") * 12, "owner-profile-1", S.SESSION),
             "plan_confirmation_mismatch"),
        ]
        for (decision, prefix, who, session), code in cases:
            res = self.decide(request_id, decision, prefix, who, session)
            self.assertEqual(res, {"ok": False, "code": code}, (decision, prefix, session))
        self.assertEqual(self.decide("apr-unknown", "rejected"), {"ok": False, "code": "unknown_request"})
        self.assertEqual(self.inbox_files(), [], "no refused decision reached the inbox")

        self.assertTrue(self.decide(request_id, "approved", plan[:12])["ok"])
        self.assertEqual(self.decide(request_id, "rejected"), {"ok": False, "code": "already_submitted"})
        self.assertEqual(len(self.inbox_files()), 1)

    def test_forged_decision_is_refused_and_the_request_stays_decidable(self):
        _, request_id = self.to_review()
        pending = self.approval(self.view(), request_id)
        forged = {
            "request_id": request_id, "run_id": pending["run_id"], "unit_id": pending["unit_id"],
            "action": pending["action"], "plan_sha256": "0" * 64, "session_id": S.SESSION,
            "decision": "approved", "decided_by": "owner-profile-1",
        }
        os.makedirs(self.inbox, exist_ok=True)
        with open(os.path.join(self.inbox, ".000-forged.json.tmp"), "w") as fh:
            json.dump(forged, fh)
        os.replace(os.path.join(self.inbox, ".000-forged.json.tmp"), os.path.join(self.inbox, "000-forged.json"))

        self.start_processor()
        processed = os.path.join(self.inbox, "processed", "000-forged.json.result.json")
        self.wait_for(lambda: os.path.exists(processed), "the forged decision to be processed")
        with open(processed) as fh:
            result = json.load(fh)
        self.assertFalse(result["ok"])
        self.assertIn("binding_matches", [c["name"] for c in result["checks"] if not c["ok"]])
        self.assertEqual(self.runtime_status(request_id), "pending")
        self.assertEqual(self.approval(self.view(), request_id)["status"], "pending")

        # The genuine decision still goes through afterwards.
        self.assertTrue(self.decide(request_id, "approved", pending["plan_sha256"][:12])["ok"])
        self.assertEqual(self.wait_resolved(request_id), "approved")

    def test_plugin_restart_and_rebuild_show_the_same_view(self):
        _, request_id = self.to_review()
        first = self.view()
        again = self.view()  # new engine on the same state: resumes, no duplicates
        self.assertEqual(again, first)
        os.remove(os.path.join(self.plugin_state, "plugin-state.json"))
        rebuilt = self.view()  # state lost: rebuilt from the event log alone
        self.assertEqual(rebuilt, first)
        self.assertEqual([a["request_id"] for a in rebuilt["pending_approvals"]], [request_id])


if __name__ == "__main__":
    unittest.main()
