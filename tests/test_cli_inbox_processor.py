"""`approvals process-inbox`: the standing decision inbox processor.

Covers once mode, dry run, watch mode (a real subprocess stopped with
SIGTERM), the single processor lock, crash recovery between the broker
resolve and the move into processed/, per file fault isolation in the watch
loop, the doctor lock report and redaction of everything printed.
"""

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from openclaw_ecc_orchestrator.cli import main
from openclaw_ecc_orchestrator.plugin.approvals import ApprovalBroker, plan_digest
from openclaw_ecc_orchestrator.plugin.events import JsonlEventSink
from openclaw_ecc_orchestrator.plugin.inbox import DecisionInbox, ProcessorLock, probe_processor_lock, \
    processor_lock_path

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
PLAN = plan_digest({"action": "merge", "unit": "u1"})
HEAD = "a" * 40
# Two secret shaped values the redaction engine recognises.
SECRET_GH = "ghp_" + "Q7vX" * 9
SECRET_ANT = "sk-ant-" + "abcDEF123" * 3


def failed(result):
    return [c["name"] for c in result.get("checks") or [] if not c["ok"]]


class ProcessorBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.state = os.path.join(self.tmp, "state")
        self.inbox = os.path.join(self.state, "decisions")
        self.processed_dir = os.path.join(self.inbox, "processed")
        self.events_path = os.path.join(self.state, "openclaw-events.jsonl")
        self.approvals_path = os.path.join(self.state, "approvals.json")
        self.config_path = os.path.join(self.tmp, "ecc.json")
        with open(self.config_path, "w") as fh:
            json.dump({"state_dir": "state"}, fh)
        self.broker = ApprovalBroker(self.approvals_path, emitter=JsonlEventSink(self.events_path))

    def tearDown(self):
        self._tmp.cleanup()

    # == fixtures ==
    def request(self, unit="u1", session="sess-A", head_sha=HEAD):
        res = self.broker.request(run_id="run1", unit_id=unit, action="merge", plan_sha256=PLAN, session_id=session,
                                  ttl_s=3600, summary=f"merge {unit}", head_sha=head_sha)
        self.assertTrue(res["ok"], res)
        return res["data"]

    def decision(self, req, **over):
        body = {k: req[k] for k in ("request_id", "run_id", "unit_id", "action", "plan_sha256", "session_id")}
        body.update(decision="approved", decided_by="op-alice")
        body.update(over)
        return {k: v for k, v in body.items() if v is not None}

    def drop(self, name, payload):
        """Write like the plugin: a hidden temporary name, then a rename."""
        os.makedirs(self.inbox, exist_ok=True)
        tmp = os.path.join(self.inbox, f".{name}.tmp")
        with open(tmp, "w") as fh:
            fh.write(payload if isinstance(payload, str) else json.dumps(payload))
        os.replace(tmp, os.path.join(self.inbox, name))
        return name

    # == running ==
    def env(self):
        return {"HOME": self.home, "PATH": os.environ.get("PATH", "/usr/bin:/bin")}

    def run_cli(self, *args, json_mode=True):
        argv = ["approvals", "process-inbox", *args, "--config", self.config_path] + (["--json"] if json_mode else [])
        out, err = io.StringIO(), io.StringIO()
        code = main(argv, stdin=io.StringIO(""), stdout=out, stderr=err, env=self.env(), is_tty=lambda: False,
                    cwd=self.tmp)
        return code, out.getvalue(), err.getvalue()

    def once(self, *args):
        code, out, err = self.run_cli(*args)
        try:
            doc = json.loads(out)
        except ValueError:
            self.fail(f"no JSON envelope (exit {code}) stdout={out!r} stderr={err!r}")
        return code, doc

    def module_cmd(self, *args):
        return [sys.executable, "-m", "openclaw_ecc_orchestrator", "approvals", "process-inbox", *args,
                "--config", self.config_path]

    def module_env(self):
        return dict(os.environ, PYTHONPATH=SRC, HOME=self.home)

    # == inspection ==
    def status(self, req):
        return ApprovalBroker(self.approvals_path).get(req["request_id"])["status"]

    def resolved_events(self):
        if not os.path.exists(self.events_path):
            return []
        with open(self.events_path) as fh:
            events = [json.loads(line) for line in fh if line.strip()]
        return [e for e in events if e["type"] == "approval.resolved"]

    def result(self, name):
        with open(os.path.join(self.processed_dir, name + ".result.json")) as fh:
            return json.load(fh)

    def pending(self):
        return sorted(n for n in os.listdir(self.inbox) if n != "processed") if os.path.isdir(self.inbox) else []

    def tree(self, root):
        """Every path under root with its bytes (or a marker for links and dirs)."""
        snap = {}
        for dirpath, dirnames, filenames in os.walk(root):
            for d in dirnames:
                snap[os.path.relpath(os.path.join(dirpath, d), root)] = "<dir>"
            for f in filenames:
                path = os.path.join(dirpath, f)
                if os.path.islink(path):
                    snap[os.path.relpath(path, root)] = "<link>" + os.readlink(path)
                else:
                    with open(path, "rb") as fh:
                        snap[os.path.relpath(path, root)] = fh.read()
        return snap


class OnceModeTest(ProcessorBase):
    def test_applies_valid_and_refuses_tampered_without_consuming(self):
        good = self.request("u1")
        target = self.request("u2")
        self.drop("001-good.json", self.decision(good))
        self.drop("002-plan.json", self.decision(target, plan_sha256="0" * 64))
        self.drop("003-session.json", self.decision(target, session_id="sess-B"))
        self.drop("004-no-decider.json", self.decision(target, decided_by=None))
        self.drop("005-blank-decider.json", self.decision(target, decided_by="   "))
        self.drop("006-head.json", self.decision(target, head_sha="b" * 40))
        code, doc = self.once()
        self.assertEqual(code, 0, doc)
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["operation"], "approvals.process-inbox")
        files = {f["file"]: f for f in doc["data"]["files"]}
        self.assertEqual(files["001-good.json"]["outcome"], "applied")
        self.assertEqual(files["001-good.json"]["decided_by"], "op-alice")
        for name in ("002-plan.json", "003-session.json", "004-no-decider.json", "005-blank-decider.json",
                     "006-head.json"):
            self.assertEqual(files[name]["outcome"], "refused", name)
        self.assertEqual((doc["data"]["applied"], doc["data"]["refused"], doc["data"]["errors"]), (1, 5, 0))
        self.assertIn("binding_matches", failed(self.result("002-plan.json")))
        self.assertIn("binding_matches", failed(self.result("003-session.json")))
        self.assertIn("decided_by_present", failed(self.result("004-no-decider.json")))
        self.assertIn("decided_by_present", failed(self.result("005-blank-decider.json")))
        self.assertIn("head_sha_matches", failed(self.result("006-head.json")))
        self.assertTrue(self.result("001-good.json")["ok"])
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.status(good), "approved")
        self.assertEqual(self.status(target), "pending")
        events = self.resolved_events()
        self.assertEqual([e["data"]["request_id"] for e in events], [good["request_id"]])
        self.assertEqual(events[0]["data"]["decided_by"], "op-alice")
        # The tampered decisions did not consume the request: the real one still applies.
        self.drop("007-real.json", self.decision(target, decision="rejected", head_sha=HEAD))
        code, doc = self.once()
        self.assertEqual(code, 0, doc)
        self.assertEqual(doc["data"]["applied"], 1)
        self.assertEqual(self.status(target), "rejected")
        self.assertEqual(len(self.resolved_events()), 2)

    def test_replayed_file_is_refused_as_already_resolved(self):
        req = self.request()
        self.drop("001.json", self.decision(req))
        self.drop("002.json", self.decision(req))
        code, doc = self.once()
        self.assertEqual(code, 0)
        self.assertEqual([f["outcome"] for f in doc["data"]["files"]], ["applied", "refused"])
        self.assertIn("request_pending", failed(self.result("002.json")))
        self.assertEqual(len(self.resolved_events()), 1)

    def test_empty_or_missing_inbox(self):
        code, doc = self.once()
        self.assertEqual(code, 0, doc)
        self.assertEqual(doc["data"]["files"], [])
        self.assertFalse(doc["changed"])

    def test_human_output(self):
        req = self.request()
        self.drop("001.json", self.decision(req))
        self.drop("002.json", "{nope")
        code, out, err = self.run_cli(json_mode=False)
        self.assertEqual(code, 0, out + err)
        self.assertIn("approvals.process-inbox: ok", out)
        self.assertIn("001.json", out)
        self.assertIn("applied", out)
        self.assertIn("002.json", out)
        self.assertIn("decision_readable", out)

    def test_malformed_oversized_and_symlink_rejected_into_processed(self):
        req = self.request()
        outside = os.path.join(self.tmp, "outside.json")
        with open(outside, "w") as fh:
            json.dump(self.decision(req), fh)
        self.drop("001-bad.json", "{nope")
        self.drop("002-big.json", json.dumps(dict(self.decision(req), pad="x" * 70_000)))
        os.symlink(outside, os.path.join(self.inbox, "003-link.json"))
        self.drop("004-list.json", "[1, 2]")
        code, doc = self.once()
        self.assertEqual(code, 0, doc)
        outcomes = {f["file"]: f["outcome"] for f in doc["data"]["files"]}
        self.assertEqual(outcomes, {"001-bad.json": "refused", "002-big.json": "refused", "003-link.json": "refused",
                                    "004-list.json": "refused"})
        self.assertEqual(self.pending(), [])
        self.assertTrue(os.path.islink(os.path.join(self.processed_dir, "003-link.json")))
        self.assertIn("symlink", json.dumps(self.result("003-link.json")))
        self.assertEqual(self.status(req), "pending")
        self.assertTrue(os.path.exists(outside))

    def test_secret_in_decision_never_printed(self):
        req = self.request()
        self.drop("001.json", self.decision(req, note=SECRET_GH, api_token=SECRET_ANT, comment=f"key {SECRET_ANT}"))
        self.drop("002.json", self.decision(req, request_id=SECRET_GH, extra={"nested": SECRET_ANT}))
        self.drop("003.json", "{" + SECRET_GH)
        outputs = []
        for args, json_mode in ((("--dry-run",), True), (("--dry-run",), False), ((), False)):
            code, out, err = self.run_cli(*args, json_mode=json_mode)
            self.assertIn(code, (0,), out + err)
            outputs.append(out + err)
        self.drop("004.json", self.decision(req, note=SECRET_GH))
        code, out, err = self.run_cli("--watch", "--interval", "0.5", "--max-passes", "1")
        outputs.append(out + err)
        code, out, err = self.run_cli("--watch", "--interval", "0.5", "--max-passes", "1", json_mode=False)
        outputs.append(out + err)
        for text in outputs:
            self.assertNotIn(SECRET_GH, text)
            self.assertNotIn(SECRET_ANT, text)
            self.assertNotIn(SECRET_ANT[:20], text)
        self.assertEqual(self.status(req), "approved")


class DryRunTest(ProcessorBase):
    def test_dry_run_changes_nothing(self):
        good = self.request("u1")
        other = self.request("u2")
        self.drop("001.json", self.decision(good))
        self.drop("002.json", self.decision(other, plan_sha256="0" * 64))
        self.drop("003.json", "{nope")
        os.makedirs(self.processed_dir)
        with open(os.path.join(self.processed_dir, "000.json"), "w") as fh:
            fh.write("{}")
        before = self.tree(self.state)
        broker_before = ApprovalBroker(self.approvals_path).state()
        code, doc = self.once("--dry-run")
        self.assertEqual(code, 0, doc)
        self.assertFalse(doc["changed"])
        self.assertEqual(self.tree(self.state), before)
        self.assertEqual(ApprovalBroker(self.approvals_path).state(), broker_before)
        would = {f["file"]: f["would"] for f in doc["data"]["files"]}
        self.assertEqual(would, {"001.json": "apply", "002.json": "refuse", "003.json": "refuse"})
        refused = next(f for f in doc["data"]["files"] if f["file"] == "002.json")
        self.assertTrue(any(x.startswith("binding_matches") for x in refused["failed"]))
        self.assertEqual(self.status(good), "pending")
        self.assertEqual(self.resolved_events(), [])
        # No processor lock file is created by a dry run.
        self.assertFalse(os.path.exists(processor_lock_path(self.state, self.inbox)))

    def test_dry_run_without_state_creates_nothing(self):
        code, doc = self.once("--dry-run")
        self.assertEqual(code, 0, doc)
        self.assertFalse(os.path.exists(self.state))


class UsageTest(ProcessorBase):
    def test_interval_bounds_and_combinations(self):
        for args in (("--watch", "--interval", "0.1"), ("--watch", "--interval", "61"),
                     ("--watch", "--interval", "abc"), ("--interval", "2"), ("--max-passes", "1"),
                     ("--watch", "--dry-run")):
            code, out, err = self.run_cli(*args)
            self.assertEqual(code, 2, (args, out, err))
        code, out, err = self.run_cli("--watch", "--interval", "0.5", "--max-passes", "1")
        self.assertEqual(code, 0, out + err)
        code, out, err = self.run_cli("--watch", "--interval", "60", "--max-passes", "1")
        self.assertEqual(code, 0, out + err)

    def test_module_help(self):
        proc = subprocess.run(self.module_cmd("--help"), env=self.module_env(), cwd=self.tmp, capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for word in ("--watch", "--interval", "--dry-run"):
            self.assertIn(word, proc.stdout)


class LockTest(ProcessorBase):
    def test_second_instance_refused(self):
        req = self.request()
        self.drop("001.json", self.decision(req))
        lock = ProcessorLock(processor_lock_path(self.state, self.inbox))
        self.assertTrue(lock.acquire(wait=0))
        try:
            for args in ((), ("--watch", "--interval", "0.5")):
                started = time.monotonic()
                code, doc = self.once(*args)
                self.assertEqual(code, 3, doc)
                self.assertLess(time.monotonic() - started, 10)
                self.assertFalse(doc["ok"])
                self.assertIn("processor_lock_acquired", failed(doc))
                self.assertEqual(doc["required_user_actions"][0]["kind"], "inbox_processor_running")
                self.assertIn(str(os.getpid()), json.dumps(doc))
            code, out, err = self.run_cli(json_mode=False)
            self.assertEqual(code, 3)
            self.assertIn("another inbox processor", out + err)
            # Nothing was processed while the lock was held elsewhere.
            self.assertEqual(self.pending(), ["001.json"])
            self.assertEqual(self.status(req), "pending")
        finally:
            lock.release()
        code, doc = self.once()
        self.assertEqual(code, 0, doc)
        self.assertEqual(self.status(req), "approved")

    def test_lock_lives_in_state_dir_not_inbox(self):
        path = processor_lock_path(self.state, self.inbox)
        self.assertEqual(os.path.dirname(path), self.state)
        self.assertNotIn(os.path.realpath(self.inbox), os.path.realpath(path))
        self.assertNotEqual(processor_lock_path(self.state, self.inbox),
                            processor_lock_path(self.state, os.path.join(self.tmp, "other-inbox")))

    def test_probe_is_read_only(self):
        path = processor_lock_path(self.state, self.inbox)
        self.assertEqual(probe_processor_lock(path)["held"], False)
        self.assertFalse(os.path.exists(self.state))
        lock = ProcessorLock(path)
        self.assertTrue(lock.acquire(wait=0))
        try:
            info = probe_processor_lock(path)
            self.assertTrue(info["held"])
            self.assertEqual(info["pid"], os.getpid())
        finally:
            lock.release()
        self.assertFalse(probe_processor_lock(path)["held"])

    def test_doctor_reports_lock(self):
        path = processor_lock_path(self.state, self.inbox)

        def doctor():
            out, err = io.StringIO(), io.StringIO()
            main(["doctor", "--config", self.config_path, "--json"], stdin=io.StringIO(""), stdout=out, stderr=err,
                 env=self.env(), is_tty=lambda: False, cwd=self.tmp)
            doc = json.loads(out.getvalue())
            chk = next(c for c in doc["checks"] if c["name"] == "inbox_processor")
            return chk, doc["data"]["inbox_processor"]

        chk, data = doctor()
        self.assertTrue(chk["ok"])
        self.assertFalse(data["held"])
        self.assertIn("not running", chk["detail"])
        self.assertFalse(os.path.exists(path))
        lock = ProcessorLock(path)
        self.assertTrue(lock.acquire(wait=0))
        try:
            chk, data = doctor()
            self.assertTrue(chk["ok"])
            self.assertTrue(data["held"])
            self.assertEqual(data["pid"], os.getpid())
            self.assertIn("running", chk["detail"])
        finally:
            lock.release()


class CrashRecoveryTest(ProcessorBase):
    def test_crash_between_resolve_and_move_does_not_double_apply(self):
        req = self.request()
        self.drop("001.json", self.decision(req))

        class Crash(BaseException):
            pass

        with mock.patch.object(DecisionInbox, "_record", side_effect=Crash("power cut")):
            with self.assertRaises(Crash):
                self.run_cli()
        # The broker persisted the decision; the file never moved.
        self.assertEqual(self.status(req), "approved")
        self.assertEqual(self.pending(), ["001.json"])
        self.assertFalse(os.path.exists(os.path.join(self.processed_dir, "001.json.result.json")))
        self.assertEqual(len(self.resolved_events()), 1)
        # The lock died with the "process": a new processor starts.
        code, doc = self.once()
        self.assertEqual(code, 0, doc)
        self.assertEqual([f["outcome"] for f in doc["data"]["files"]], ["refused"])
        result = self.result("001.json")
        self.assertFalse(result["ok"])
        self.assertIn("request_pending", failed(result))
        self.assertEqual(self.pending(), [])
        self.assertTrue(os.path.exists(os.path.join(self.processed_dir, "001.json")))
        self.assertEqual(self.status(req), "approved")
        self.assertEqual(len(self.resolved_events()), 1)
        audit = ApprovalBroker(self.approvals_path).state()["rejected_attempts"]
        self.assertEqual([a["failed"] for a in audit], [["request_pending"]])


class WatchLoopTest(ProcessorBase):
    def lines(self, out):
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    def test_exception_in_one_file_does_not_stop_the_loop(self):
        bad = self.request("u1")
        good = self.request("u2")
        self.drop("001-bad.json", self.decision(bad))
        self.drop("002-good.json", self.decision(good))
        self.drop("003-garbage.json", "{nope")
        real = ApprovalBroker.resolve

        def flaky(broker, decision):
            if decision.get("request_id") == bad["request_id"]:
                raise RuntimeError(f"disk on fire {SECRET_GH}")
            return real(broker, decision)

        with mock.patch.object(ApprovalBroker, "resolve", flaky):
            code, out, err = self.run_cli("--watch", "--interval", "0.5", "--max-passes", "3")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn(SECRET_GH, out + err)
        lines = self.lines(out)
        errors = [ln for ln in lines if ln["data"]["event"] == "error"]
        files = [ln for ln in lines if ln["data"]["event"] == "file"]
        stopped = [ln for ln in lines if ln["data"]["event"] == "stopped"]
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[-1], lines[-1])
        self.assertEqual(stopped[0]["data"]["reason"], "max_passes")
        self.assertGreaterEqual(len(errors), 1)
        self.assertTrue(all(e["data"]["file"] == "001-bad.json" for e in errors))
        self.assertTrue(all(not e["ok"] for e in errors))
        self.assertIn("RuntimeError", json.dumps(errors[0]))
        outcomes = {ln["data"]["file"]: ln["data"]["outcome"] for ln in files}
        self.assertEqual(outcomes["002-good.json"], "applied")
        self.assertEqual(outcomes["003-garbage.json"], "refused")
        self.assertEqual(self.status(good), "approved")
        # The bad file was retried, then set aside with a reason; its request was never resolved.
        self.assertEqual(len(errors), 3)
        self.assertEqual(outcomes["001-bad.json"], "refused")
        self.assertIn("processing_error", failed(self.result("001-bad.json")))
        self.assertNotIn(SECRET_GH, json.dumps(self.result("001-bad.json")))
        self.assertEqual(self.status(bad), "pending")
        self.assertEqual(self.pending(), [])

    def test_human_lines(self):
        req = self.request()
        self.drop("001.json", self.decision(req))
        code, out, err = self.run_cli("--watch", "--interval", "0.5", "--max-passes", "1", json_mode=False)
        self.assertEqual(code, 0, out + err)
        lines = [ln for ln in out.splitlines() if ln.strip()]
        self.assertEqual(len([ln for ln in lines if "001.json" in ln]), 1)
        self.assertIn("applied", next(ln for ln in lines if "001.json" in ln))
        self.assertIn("stopped", lines[-1])


class WatchSubprocessTest(ProcessorBase):
    def wait_for(self, predicate, timeout=30.0, what="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}")

    def test_watch_processes_new_file_and_stops_on_sigterm(self):
        req = self.request()
        lock_path = processor_lock_path(self.state, self.inbox)
        proc = subprocess.Popen(self.module_cmd("--watch", "--interval", "0.5", "--json"), env=self.module_env(),
                                cwd=self.tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.wait_for(lambda: probe_processor_lock(lock_path)["held"] or proc.poll() is not None,
                          what="the processor lock")
            self.assertIsNone(proc.poll(), proc.stderr.read() if proc.poll() is not None else "")
            # A second instance is refused while the first runs.
            second = subprocess.run(self.module_cmd("--json"), env=self.module_env(), cwd=self.tmp,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(second.returncode, 3, second.stdout + second.stderr)
            self.assertIn("another inbox processor", second.stdout)
            # A decision written after start is processed by the standing processor.
            self.drop("100.json", self.decision(req))
            result_path = os.path.join(self.processed_dir, "100.json.result.json")
            self.wait_for(lambda: os.path.exists(result_path), what="the result file")
            self.assertEqual(self.status(req), "approved")
            proc.send_signal(signal.SIGTERM)
            out, err = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertEqual(proc.returncode, 0, out + err)
        lines = [json.loads(line) for line in out.splitlines() if line.strip()]
        files = [ln for ln in lines if ln["data"]["event"] == "file"]
        self.assertEqual([(f["data"]["file"], f["data"]["outcome"]) for f in files], [("100.json", "applied")])
        self.assertTrue(files[0]["ok"])
        self.assertEqual(lines[-1]["data"]["event"], "stopped")
        self.assertEqual(lines[-1]["data"]["reason"], "SIGTERM")
        self.assertEqual(len(self.resolved_events()), 1)
        self.assertFalse(probe_processor_lock(lock_path)["held"])


if __name__ == "__main__":
    unittest.main()
