"""Produce real runtime events for the plugin contract test.

Usage: python3 gen_events.py <workdir> <session_id>

Creates a throwaway git repository under <workdir>/repo to obtain a real head
commit, then drives the runtime's own JsonlEventSink and ApprovalBroker (the
same classes the CLI uses) to write events into <workdir>/state. Prints one
JSON object describing what was produced. PYTHONPATH must include the
runtime's src directory.
"""

import json
import os
import subprocess
import sys

from openclaw_ecc_orchestrator.plugin.approvals import ApprovalBroker, plan_digest
from openclaw_ecc_orchestrator.plugin.events import JsonlEventSink


def git(repo, *args):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_AUTHOR_NAME": "contract-test",
        "GIT_AUTHOR_EMAIL": "contract-test@invalid",
        "GIT_COMMITTER_NAME": "contract-test",
        "GIT_COMMITTER_EMAIL": "contract-test@invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "HOME": repo,
    }
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout.strip()


def main():
    workdir, session = sys.argv[1], sys.argv[2]
    repo = os.path.join(workdir, "repo")
    state = os.path.join(workdir, "state")
    os.makedirs(repo)
    os.makedirs(state)
    git(repo, "init", "-q", "-b", "main")
    with open(os.path.join(repo, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("contract\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "init")
    head = git(repo, "rev-parse", "HEAD")

    event_log = os.path.join(state, "openclaw-events.jsonl")
    sink = JsonlEventSink(event_log)
    broker = ApprovalBroker(os.path.join(state, "approvals.json"), emitter=sink)

    plan = {"units": [{"id": "alpha"}, {"id": "beta"}], "base": head}
    plan_sha = plan_digest(plan)
    sink.emit("run.created", "run1", data={"plan_sha256": plan_sha, "unit_ids": ["alpha", "beta"]})
    sink.emit("unit.state_changed", "run1", unit_id="alpha", data={"from": "ready", "to": "running", "owner": "codex"})
    fake_token = "gh" + "p_" + "Zq8Wv7Ut6Sr5Qp4On3Ml2Kj1Ih0Gf9"
    sink.emit("unit.progress", "run1", unit_id="alpha", data={"message": f"pushing with {fake_token}", "stream": "stdout"})
    sink.emit("unit.state_changed", "run1", unit_id="alpha", data={"from": "running", "to": "reviewing"})
    sink.emit("attention.required", "run1", unit_id="beta",
              data={"reason": "out_of_scope_changes", "state": "blocked", "files": ["other.py"]})
    res = broker.request(run_id="run1", unit_id="alpha", action="merge", plan_sha256=plan_sha,
                         session_id=session, ttl_s=3600, summary="merge alpha into main", head_sha=head)
    assert res["ok"], res
    # A second request that will expire immediately (for the expiry refusal).
    expired = broker.request(run_id="run1", unit_id="beta", action="merge", plan_sha256=plan_sha,
                             session_id=session, ttl_s=0.001, summary="expires", head_sha=head)
    assert expired["ok"], expired
    print(json.dumps({
        "event_log": event_log,
        "approvals_state": os.path.join(state, "approvals.json"),
        "request_id": res["data"]["request_id"],
        "expired_request_id": expired["data"]["request_id"],
        "plan_sha256": plan_sha,
        "head_sha": head,
        "fake_token": fake_token,
    }))


if __name__ == "__main__":
    main()
