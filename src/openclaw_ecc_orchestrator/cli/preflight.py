"""`ecc preflight`: a round trip through every certified coding runner
(TEAM-DELIVERY-v2 section 9).

Each runner gets a throwaway worktree on a throwaway branch and the exact
prompt, command line, environment and permissions a real unit gets. It must
write a file, commit it, run the gate commands, and write a schema-valid
handoff to the real handoff path. The orchestrator then runs a gate command
itself. Every defect found in the 2026-10-19 pilot (old CLI, missing USER,
unreachable handoff path, malformed handoff, no uv) fails one of these checks
before a real unit can hit it. The worktree and branch are removed afterward.
"""

import json
import os
import time

from .. import schemas
from ..process.supervisor import Supervisor
from ..runs import repair as R
from ..runs.conductor import HANDOFF_ENV, UNIT_ENV, fill_handoff_tokens
from ..runs.envelope import check, envelope
from ..worktrees.git import git_out, run_git
from .commands import _with_handoff_dir, runner_prompt

PROMPT_TOKEN = "{ECC_PROMPT}"
PREFLIGHT_FILE = "PREFLIGHT.txt"
GATE_COMMANDS = ["uv --version", "git status --short"]
PREFLIGHT_UNIT = {
    "id": "PREFLIGHT",
    "title": "Preflight round trip",
    "description": (f"This is a platform check, not product work. Create the file {PREFLIGHT_FILE} containing the "
                    "single line ok. Commit it with the message preflight. Run each acceptance command and record "
                    "it in the handoff with its exit code."),
    "scope": {"files": [PREFLIGHT_FILE]},
    "acceptance": {"commands": list(GATE_COMMANDS)},
}


def preflight_runner(*, repo, target_branch, worktree_root, logs_dir, runner, adapter, model=None,
                     supervisor=None, timeout=600.0, keep=False, argv_override=None, clock=time.time):
    """Run one runner's round trip. Returns a dict with `ok` and `checks`.
    `argv_override` (tests) is a command template; `{ECC_PROMPT}` is replaced
    by the real unit prompt and the handoff placeholders are filled as usual."""
    stamp = int(clock() * 1000)
    name = f"{runner}-{stamp}"
    path = os.path.join(worktree_root, "_preflight", name)
    branch = f"ecc-preflight/{name}"
    handoff_path = os.path.join(logs_dir, "_preflight", name + ".handoff.json")
    log_path = handoff_path[:-len(".handoff.json")] + ".log"
    os.makedirs(os.path.dirname(handoff_path), exist_ok=True)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    checks = []
    result = {"runner": runner, "model": model, "branch": branch, "log_path": log_path}
    base = git_out(["rev-parse", f"refs/heads/{target_branch}"], repo)
    run_git(["worktree", "add", "-q", "-b", branch, path, base], repo)
    try:
        prompt = runner_prompt(PREFLIGHT_UNIT)
        if argv_override is not None:
            argv = [prompt if a == PROMPT_TOKEN else a for a in argv_override]
        else:
            argv = _with_handoff_dir(adapter.build_invocation(prompt, model=model, cwd=path, writable=True))
        argv = fill_handoff_tokens(argv, handoff_path)
        sup = supervisor or Supervisor(clock=clock)
        env_allow = tuple(adapter.env_allow()) if hasattr(adapter, "env_allow") else ()
        proc = sup.run(argv, cwd=path, env_allow=env_allow,
                       extra_env={HANDOFF_ENV: handoff_path, UNIT_ENV: PREFLIGHT_UNIT["id"]},
                       log_path=log_path, timeout=timeout)
        result["duration_s"] = proc.get("duration_s")
        exited = proc.get("exit_code") == 0 and not proc.get("error") and not proc.get("timed_out")
        detail = "" if exited else (proc.get("error") or f"exit {proc.get('exit_code')}")
        if not exited:
            stop = R.classify_runner_failure(R.read_tail(log_path), proc.get("exit_code"))
            if stop:
                detail = f"{stop[0]}: {stop[1]}; fix: {R.stop_fix(stop[0], runner)}"
        checks.append(check("runner_exited_cleanly", exited, detail))

        changed = git_out(["log", "-1", "--name-only", "--format=", "HEAD"], path).split()
        head = git_out(["rev-parse", "HEAD"], path)
        committed = head != base and PREFLIGHT_FILE in changed
        checks.append(check("runner_committed", committed, f"head {head[:12]}, files {changed}"))

        doc, problem = _read(handoff_path)
        checks.append(check("handoff_written_to_path", doc is not None, problem or handoff_path))
        if doc is not None:
            report = schemas.validate_handoff(doc, PREFLIGHT_UNIT)
            problems = list(report.errors) + [v.get("kind", "violation") for v in report.violations]
            if report.ok and doc.get("status") != "succeeded":
                problems.append(f"status {doc.get('status')}")
            checks.append(check("handoff_valid", not problems, "; ".join(problems)))
            ran = {c.get("command"): c.get("exit_code") for c in doc.get("commands") or [] if isinstance(c, dict)}
            missing = [c for c in GATE_COMMANDS if ran.get(c) != 0]
            checks.append(check("runner_ran_gate_commands", not missing,
                                "not run or failed: " + ", ".join(missing) if missing else ""))
        else:
            checks.append(check("handoff_valid", False, "no handoff"))
            checks.append(check("runner_ran_gate_commands", False, "no handoff"))

        gate = sup.run(["uv", "--version"], cwd=path, env_allow=("HOME",), timeout=60)
        checks.append(check("gate_runner_has_uv", gate.get("exit_code") == 0,
                            gate.get("error") or f"exit {gate.get('exit_code')}"))
    finally:
        if not keep:
            run_git(["worktree", "remove", "--force", path], repo, check=False)
            run_git(["branch", "-D", branch], repo, check=False)
    result["checks"] = checks
    result["ok"] = all(c["ok"] for c in checks)
    return result


def _read(path):
    if not os.path.isfile(path):
        return None, "not written"
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), None
    except (OSError, ValueError) as exc:
        return None, f"unreadable: {type(exc).__name__}"


def cmd_preflight(ctx):
    op = "preflight"
    a = ctx.args
    from ..runners.probes import get_adapter
    repo = ctx.rt.repo_top(op)
    policy, _ = ctx.rt.load_policy(op)
    certified, _ = ctx.rt.certified(op)
    override = None
    if getattr(a, "runner_command_json", None):
        if not getattr(a, "allow_test_runner", False):
            return envelope(op, ok=False, checks=[check("test_runner_allowed", False, "needs --allow-test-runner")])
        override = json.loads(a.runner_command_json)
    results, checks = [], []
    for name in certified or []:
        if a.runner and name != a.runner:
            continue
        try:
            adapter = get_adapter(name)
        except (KeyError, ValueError):
            continue
        if getattr(getattr(adapter, "profile", None), "kind", None) != "coding_cli":
            continue
        model = (((policy or {}).get("cli_models") or {}).get(name) or {}).get("economical")
        ctx.progress(f"preflight {name} ({model or 'default model'})")
        res = preflight_runner(repo=repo, target_branch=ctx.cfg.target_branch, worktree_root=ctx.cfg.worktree_root,
                               logs_dir=ctx.cfg.logs_dir, runner=name, adapter=adapter, model=model,
                               timeout=float(a.timeout), keep=a.keep, argv_override=override, clock=ctx.clock)
        results.append(res)
        failed = [f"{c['name']}: {c['detail']}" for c in res["checks"] if not c["ok"]]
        checks.append(check(f"preflight_{name}", res["ok"], "; ".join(failed)))
    if not results:
        checks.append(check("coding_runner_certified", False, "no certified coding CLI runner to check"))
    return envelope(op, ok=all(c["ok"] for c in checks), changed=False, checks=checks, data={"runners": results})
