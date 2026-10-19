"""Live round-trip smoke test on the operator's Mac (run by bin/roundtrip-smoke).

What it exercises, all real and live:
  operator CLI  -> the live event log (default state dir)
  OpenClaw gateway plugin (ECC Orchestrator tab, /ecc-decide)  <- you decide
  launchd inbox processor  -> approval.resolved
  operator CLI merge  -> the throwaway repository only

Everything it changes lives in a new temporary directory: a throwaway git
repository, its worktrees, and a config naming fake runner certifications
for that repository only (your real certifications are not read or touched).
The run, its events and its approval request do go to the live state dir so
that OpenClaw shows them; they are labelled `smoke-<stamp>`. No model is
called, no key is read, nothing is pushed.

The script waits for your decision without a time limit of its own; the
approval request expires after the runtime's approval_ttl (default one hour).

Usage: python3 roundtrip_smoke.py [--session agent:main:main] [--keep]
Exit codes: 0 round trip verified, 1 failed or expired, 2 usage.
"""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(REPO, "src")
sys.path.insert(0, SRC)

from openclaw_ecc_orchestrator.plugin.approvals import ApprovalBroker  # noqa: E402
from openclaw_ecc_orchestrator.plugin.inbox import probe_processor_lock, processor_lock_path  # noqa: E402
from openclaw_ecc_orchestrator.schemas import CERTIFICATION_CHECKS  # noqa: E402

OPERATOR = "smoke-operator"
RUNNER = r'''
import json, os, subprocess
with open("smoke.txt", "w") as fh:
    fh.write("round trip smoke\n")
subprocess.run(["git", "add", "smoke.txt"], check=True)
subprocess.run(["git", "commit", "-q", "-m", "smoke: runner work"], check=True)
print("committed", flush=True)
head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip()
with open(os.environ["ECC_HANDOFF_PATH"], "w") as fh:
    json.dump({
        "schema_version": "1.0", "unit_id": os.environ["ECC_UNIT_ID"], "status": "succeeded",
        "outcome": "done", "files_changed": ["smoke.txt"], "behavior": "writes smoke.txt",
        "commands": [{"command": "git commit", "exit_code": 0, "result": "ok"}],
        "unresolved_failures": [], "assumptions": [], "risks": [], "next_action": "review",
        "commit": {"sha": head, "branch": branch or "unit", "worktree": "."},
        "usage": {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "model": "smoke"},
        "user_input_required": False,
    }, fh)
'''


def say(msg=""):
    print(msg, flush=True)


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def cert(runner):
    return {"schema_version": "1.0", "runner": runner, "provider": runner, "status": "certified",
            "checks": [{"name": n, "status": "pass", "reason": "smoke"} for n in CERTIFICATION_CHECKS],
            "certified_at": "2026-01-01T00:00:00Z", "expires_at": "2099-01-01T00:00:00Z",
            "version": "smoke", "models": [], "kind": "coding_cli"}


class Smoke:
    def __init__(self, root, session):
        self.root = root
        self.session = session
        self.run_id = "smoke-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.repo = os.path.join(root, "repo")
        self.config = os.path.join(root, "ecc.json")

    def write(self, name, obj):
        path = os.path.join(self.root, name)
        with open(path, "w") as fh:
            json.dump(obj, fh, indent=1) if not isinstance(obj, str) else fh.write(obj)
        return path

    def setup(self):
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "smoke@example.invalid")
        git(self.repo, "config", "user.name", "ECC smoke")
        with open(os.path.join(self.repo, "README.md"), "w") as fh:
            fh.write("throwaway repository for the ECC round-trip smoke test\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-q", "-m", "base")
        self.write("runner.py", RUNNER)
        py = shlex.quote(sys.executable)
        self.write("policy.json", {
            "schema_version": "1.0", "high_risk_paths": [], "required_checks": ["unit-tests"],
            "protected_commands": ["git push*"], "allowed_providers": ["claude", "codex"],
            "budget": {"max_cost_usd_per_unit": 1, "max_cost_usd_total": 1, "max_minutes_per_unit": 600},
            "cli_models": {"codex": {"economical": "smoke", "standard": "smoke", "advanced": "smoke"},
                           "claude": {"economical": "smoke", "standard": "smoke", "advanced": "smoke"}},
            "runner_costs": {"codex": {"economical": 0, "standard": 0, "advanced": 0},
                             "claude": {"economical": 0, "standard": 0, "advanced": 0}},
        })
        self.write("certs.json", [cert("codex"), cert("claude")])
        # No state_dir: the default live state dir, the one the plugin reads.
        self.write("ecc.json", {
            "worktree_root": "worktrees", "repo": "repo", "target_branch": "main",
            "policy_file": "policy.json", "certifications": "certs.json",
            "repo_checks": {"required": ["unit-tests"], "commands": {"unit-tests": f"{py} -c pass"}},
        })
        self.write("plan.json", {"units": [{
            "schema_version": "1.0", "id": "smoke", "title": "round-trip smoke", "depends_on": [],
            "scope": {"files": ["smoke.txt"]},
            "acceptance": {"commands": [f"{py} -c \"import os,sys; sys.exit(0 if os.path.exists('smoke.txt') else 1)\""]},
            "risk": "low", "capabilities": {"network": False, "secrets": []},
            "routing": {"initial_tier": 0, "maximum_tier": 2, "reviewer_must_differ_from_author": True},
            "budget": {"attempts": 1, "minutes": 600, "maximum_cost_usd": 1},
            "rollback": "delete the temporary directory",
        }]})

    # == CLI ==
    def cli(self, *args, check=True):
        cmd = [sys.executable, "-m", "openclaw_ecc_orchestrator", *args, "--config", self.config, "--json"]
        env = dict(os.environ, PYTHONPATH=SRC)
        proc = subprocess.run(cmd, cwd=self.root, env=env, capture_output=True, text=True)
        try:
            doc = json.loads(proc.stdout)
        except ValueError:
            raise SystemExit(f"FAIL: {' '.join(args[:2])} printed no JSON (exit {proc.returncode}): {proc.stderr[-500:]}")
        if check and (proc.returncode != 0 or not doc.get("ok")):
            failed = [c for c in doc.get("checks") or [] if not c.get("ok")]
            raise SystemExit(f"FAIL: {' '.join(args[:2])} exit {proc.returncode}: "
                             f"{json.dumps(failed or doc, default=str)[:1200]}")
        return proc.returncode, doc

    def reviewed(self, *cmd):
        argv = [sys.executable, "-m", "openclaw_ecc_orchestrator", "review-plan", "--json", "--approve",
                "--operator", OPERATOR, "--session", self.session, "--", *cmd, "--config", self.config]
        proc = subprocess.run(argv, cwd=self.root, env=dict(os.environ, PYTHONPATH=SRC), capture_output=True, text=True)
        try:
            review_id = json.loads(proc.stdout)["data"]["review_id"]
        except (ValueError, KeyError, TypeError):
            raise SystemExit(f"FAIL: review-plan for {cmd[0]} (exit {proc.returncode}): {proc.stdout[-500:]}{proc.stderr[-300:]}")
        return self.cli(*cmd, "--yes", "--review-id", review_id)

    def runtime_config(self):
        _, doc = self.cli("doctor", check=False)
        return (doc.get("data") or {}).get("config") or {}

    def status(self, unit="smoke"):
        _, doc = self.cli("status", "--run-id", self.run_id)
        return {u["unit_id"]: u for u in doc["data"]["units"]}[unit]


def main():
    ap = argparse.ArgumentParser(description="Live ECC round trip through OpenClaw on this Mac.")
    ap.add_argument("--session", default="agent:main:main",
                    help="OpenClaw session you will decide from (default: the Main Session, agent:main:main)")
    ap.add_argument("--keep", action="store_true", help="keep the temporary directory")
    args = ap.parse_args()

    root = os.path.realpath(tempfile.mkdtemp(prefix="ecc-roundtrip-smoke."))
    smoke = Smoke(root, args.session)
    ok = False
    try:
        smoke.setup()
        rcfg = smoke.runtime_config()
        state, inbox = rcfg.get("state_dir"), rcfg.get("decision_inbox")
        if not state or not inbox:
            raise SystemExit("FAIL: could not resolve the runtime state dir (doctor)")
        lock = probe_processor_lock(processor_lock_path(state, inbox))
        say(f"state dir:        {state}")
        say(f"event log:        {rcfg.get('event_log')}  (the plugin's eventLog must be this file)")
        say(f"inbox processor:  {'running (pid %s)' % lock.get('pid') if lock.get('held') else 'NOT running'}")
        if not lock.get("held"):
            say("  The launchd inbox processor is not holding its lock; decisions will wait in the inbox.")
            say("  Start it: launchctl kickstart -k \"gui/$(id -u)/local.openclaw-ecc-orchestrator.inbox-processor\"")

        smoke.reviewed("create-run", "--run-id", smoke.run_id, "--plan", os.path.join(root, "plan.json"))
        runner = json.dumps([sys.executable, os.path.join(root, "runner.py")])
        smoke.reviewed("dispatch", "--run-id", smoke.run_id, "--unit", "smoke", "--allow-test-runner",
                       "--runner-command-json", runner)
        head = smoke.status()["verified_head"]
        _, doc = smoke.reviewed("review", "--run-id", smoke.run_id, "--unit", "smoke", "--reviewer", "smoke-reviewer",
                                "--runner", "claude", "--model", "smoke", "--tier", "2", "--verdict", "approved",
                                "--head", head, "--session", args.session)
        req = doc["data"]["approval_request"]
        request_id, prefix = req["request_id"], req["plan_sha256"][:12]

        say()
        say(f"Run {smoke.run_id} is waiting for a merge approval ({request_id}).")
        say("Check that the ECC Orchestrator tab at http://127.0.0.1:18789/ shows it pending, then decide")
        say("from another terminal in this repository with ONE of:")
        say(f"    bin/ecc-decide approve {request_id}")
        say(f"    bin/ecc-decide reject {request_id}")
        say("(From a chat channel that reports the sender, the equivalent command is")
        say(f"    /ecc-decide approve {request_id} {prefix}")
        say(" ; the Control UI chat gives plugin commands no sender, so it refuses there.)")
        say("Waiting for your decision (Ctrl-C to stop; the request expires after the approval TTL)...")

        broker = ApprovalBroker(os.path.join(state, "approvals.json"))
        started = time.monotonic()
        last_note = started
        status = "pending"
        while status == "pending":
            time.sleep(2)
            rec = broker.get(request_id) or {}
            status = rec.get("status", "pending")
            if status == "pending" and time.time() > float(rec.get("expires_at") or 0):
                status = "expired"
            if status == "pending" and time.monotonic() - last_note >= 60:
                last_note = time.monotonic()
                waiting = len([n for n in os.listdir(inbox) if n.endswith(".json")]) if os.path.isdir(inbox) else 0
                say(f"  still waiting ({int(time.monotonic() - started)}s); decision files in the inbox: {waiting}")
        say(f"decision applied by the runtime: {status} (decided by {(broker.get(request_id) or {}).get('decided_by')})")

        base = git(smoke.repo, "rev-parse", "main")
        if status == "approved":
            _, doc = smoke.cli("merge", "--run-id", smoke.run_id, "--unit", "smoke")
            merged = doc["data"]["result"]["merged_commit"]
            if git(smoke.repo, "rev-parse", "main") != merged or git(smoke.repo, "show", "main:smoke.txt") != "round trip smoke":
                raise SystemExit("FAIL: merge reported success but the throwaway main does not match")
            say(f"merged into the throwaway repository: {merged[:12]}")
            say("PASS: approve round trip. The ECC Orchestrator tab should now show the unit merged.")
            ok = True
        elif status == "rejected":
            code, _ = smoke.cli("merge", "--run-id", smoke.run_id, "--unit", "smoke", check=False)
            if code == 0 or git(smoke.repo, "rev-parse", "main") != base:
                raise SystemExit("FAIL: a rejected approval admitted the merge")
            say("PASS: reject round trip. The merge was refused and main is unchanged.")
            ok = True
        else:
            say(f"FAIL: the request ended {status}")
    except KeyboardInterrupt:
        say("\nstopped; the approval request stays pending until it expires")
    finally:
        if args.keep:
            say(f"kept {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
