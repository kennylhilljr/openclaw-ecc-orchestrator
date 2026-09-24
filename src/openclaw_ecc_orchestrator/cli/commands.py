"""Plan builders and executors for every gated (mutating) subcommand.

Each gated command is a `Spec`:

* `plan(ctx)` is read only. It returns a `Plan` whose `doc` is a
  deterministic JSON document (no timestamps) describing exactly what the
  command will do; its SHA-256 over canonical JSON is the plan hash that
  review records and broker approvals bind to.
* `execute(ctx, plan)` performs the plan through the runtime's public APIs.
* `authorize(ctx, plan, digest)` exists for destructive plans only; it
  admits the plan through the ApprovalBroker or an interactive
  confirmation, never through `--yes`.
* `lease_run(plan)` names the run whose conductor lease must be held.
"""

import hashlib
import os
import uuid

from ..merge_queue.conflicts import diff_stats, out_of_scope_changes
from ..merge_queue.dispatch import ConflictError, assert_parallel_safe
from ..plugin.approvals import plan_digest
from ..plugin.inbox import DecisionInbox
from ..routing.classify import classify
from ..routing.escalation import AttemptOutcome, EscalationController
from ..routing.selection import select_runner
from ..runs import state as S
from ..runs.envelope import SCHEMA_VERSION, check, envelope
from ..runs.fsutil import atomic_write_json
from ..runs.manager import RunError
from ..runs.store import StoreError
from ..worktrees.git import git_out, run_git
from .config import UsageError, checked_path
from .runtime import CommandFailed, fail, read_document

_HEX = set("0123456789abcdef")


class Plan:
    def __init__(self, doc, destructive=False, checks=None, warnings=None, extra=None):
        self.doc = doc
        self.destructive = bool(destructive)
        self.doc["destructive"] = self.destructive
        self.checks = list(checks or [])
        self.warnings = list(warnings or [])
        self.extra = dict(extra or {})

    @property
    def operation(self):
        return self.doc["operation"]


def is_sha(value):
    return isinstance(value, str) and len(value) >= 40 and set(value) <= _HEX


def branch_tip(repo, branch):
    if not branch:
        return None
    proc = run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"], repo, check=False)
    return proc.stdout.strip() or None


def load_run(ctx, op, run_id):
    store = ctx.rt.store
    try:
        if not store.exists(run_id):
            fail(op, "run_exists", f"run {run_id} not found under {store.root}")
        return ctx.rt.manager.load(run_id)
    except (RunError, StoreError, OSError, ValueError) as exc:
        fail(op, "run_loadable", f"{type(exc).__name__}: {exc}")


def load_unit(op, run, unit_id):
    unit = run["units"].get(unit_id)
    if unit is None:
        fail(op, "unit_exists", f"unit {unit_id} is not part of run {run['run_id']}")
    return unit


def plan_unit(ctx, run, unit_id):
    return ctx.rt.manager.plan_unit(run, unit_id)


def base_doc(op, **fields):
    doc = {"schema_version": SCHEMA_VERSION, "operation": op}
    doc.update(fields)
    return doc


# == create-run ==
def plan_create_run(ctx):
    op = "create-run"
    a, cfg = ctx.args, ctx.cfg
    path = checked_path(a.plan, "--plan", ctx.cwd)
    text, plan = read_document(path, "plan", op)
    if isinstance(plan, list):
        plan = {"units": plan}
    repo = ctx.rt.repo_top(op)
    policy, policy_path = ctx.rt.load_policy(op)
    tip = branch_tip(repo, cfg.target_branch)
    if tip is None:
        fail(op, "target_branch_exists", cfg.target_branch)
    if ctx.rt.store.exists(a.run_id):
        fail(op, "run_id_unique", f"run {a.run_id} already exists")
    res = ctx.rt.manager.create_run(plan, cfg.conductor_id, run_id=a.run_id, dry_run=True, policy=policy,
                                    metadata={"base_commit": tip, "target_branch": cfg.target_branch})
    if not res["ok"]:
        res["operation"] = op
        raise CommandFailed(res)
    summary = res["data"]
    doc = base_doc(op, run_id=a.run_id, plan_file=path,
                   plan_file_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                   plan_sha256=summary["plan_sha256"], layers=summary["layers"], ready=summary["ready"],
                   repo=repo, target_branch=cfg.target_branch, base_commit=tip, policy_file=policy_path,
                   policy_sha256=plan_digest(policy) if policy is not None else None,
                   worktree_root=cfg.worktree_root, conductor_id=cfg.conductor_id)
    return Plan(doc, checks=res["checks"], extra={"plan": plan, "policy": policy})


def execute_create_run(ctx, plan):
    c = ctx.rt.conductor(plan.extra["policy"], "create-run")
    return c.create_run(plan.extra["plan"], run_id=plan.doc["run_id"])


# == dispatch ==
def predict_routing(ctx, op, run, unit_id):
    """The routing the conductor will choose: classification, the escalation
    controller rebuilt from the persisted record, then select_runner."""
    policy = run.get("policy")
    punit = plan_unit(ctx, run, unit_id)
    classification = classify(punit, policy)
    if not classification.ok:
        fail(op, "classification_valid", "; ".join(classification.errors),
             actions=[{"kind": "resolve_needs_user", "run_id": run["run_id"], "unit_id": unit_id,
                       "detail": "fix the unit's routing or policy"}])
    ctrl = EscalationController(punit, classification, tier_costs=ctx.rt.tier_costs(), clock=ctx.clock, policy=policy)
    record = (run["units"][unit_id].get("annotations") or {}).get("escalation") or {}
    for attempt in record.get("attempts") or []:
        if ctrl.finished:
            break
        ctrl.record(AttemptOutcome(
            passed=bool(attempt.get("passed")), objective_failure=bool(attempt.get("objective_failure")),
            cost_usd=float(attempt.get("cost_usd") or 0.0), elapsed_seconds=attempt.get("elapsed_seconds"),
            runner=attempt.get("runner"), model=attempt.get("model"), usage=attempt.get("usage"),
            reason=attempt.get("reason") or ""))
    action = ctrl.next_action()
    if action["action"] in ("stop", "done"):
        fail(op, "escalation_allows_attempt", action["reason"],
             actions=[{"kind": "resolve_needs_user", "run_id": run["run_id"], "unit_id": unit_id,
                       "detail": f"escalation {action['action']}: {action['reason']}"}])
    certified, _ = ctx.rt.certified(op)
    selection = select_runner(action["tier"], certified, ctx.rt.catalog(op), policy, role="author",
                              risk=classification.risk, now=ctx.clock())
    if not selection.ok:
        fail(op, "runner_selected", "; ".join(selection.reasons) or "no eligible certified runner",
             actions=[{"kind": "resolve_needs_user", "run_id": run["run_id"], "unit_id": unit_id,
                       "detail": "certify a runner for tier %s or adjust the policy" % action["tier"]}])
    return {"tier": action["tier"], "runner": selection.runner, "provider": selection.provider,
            "model": selection.model, "family": selection.family,
            "estimated_cost_usd": selection.estimated_cost_usd, "escalation": action["action"]}


def runner_prompt(punit):
    """Deterministic instructions for a coding CLI runner."""
    lines = [f"You are implementing work unit {punit['id']}: {punit.get('title', '')}."]
    if punit.get("description"):
        lines.append(str(punit["description"]))
    files = (punit.get("scope") or {}).get("files") or []
    lines.append("Only change these paths: " + (", ".join(files) if files else "(whole repository)") + ".")
    commands = (punit.get("acceptance") or {}).get("commands") or []
    if commands:
        lines.append("Acceptance commands that must pass: " + "; ".join(commands) + ".")
    lines.append("Commit your work on the current branch. Never push, never change other branches.")
    lines.append("Finally write a JSON handoff document (schema_version 1.0: unit_id, status, outcome, "
                 "files_changed, behavior, commands, unresolved_failures, assumptions, risks, next_action, "
                 "commit {sha, branch, worktree}, usage, user_input_required) to the file named by the "
                 "ECC_HANDOFF_PATH environment variable.")
    return "\n".join(lines)


def runner_command(ctx, op, punit, routing, worktree_path):
    a = ctx.args
    if a.runner_command_argv is not None:
        return {"source": "test_override", "argv": list(a.runner_command_argv), "env_allow": []}
    from ..runners.probes import get_adapter
    try:
        adapter = get_adapter(routing["runner"])
    except (KeyError, ValueError) as exc:
        fail(op, "runner_command_available", str(exc))
    kind = getattr(getattr(adapter, "profile", None), "kind", None)
    if kind != "coding_cli" or not hasattr(adapter, "build_invocation"):
        fail(op, "runner_command_available",
             f"runner {routing['runner']} is an API runner; CLI dispatch launches coding CLI runners only",
             actions=[{"kind": "resolve_needs_user", "run_id": ctx.args.run_id, "unit_id": punit["id"],
                       "detail": "certify a coding CLI runner for this tier or drive the unit from Python"}])
    argv = adapter.build_invocation(runner_prompt(punit), model=routing.get("model"), cwd=worktree_path,
                                    writable=True)
    env_allow = list(adapter.env_allow()) if hasattr(adapter, "env_allow") else []
    return {"source": "registry", "argv": list(argv), "env_allow": env_allow}


def plan_dispatch(ctx):
    op = "dispatch"
    a = ctx.args
    run = load_run(ctx, op, a.run_id)
    unit = load_unit(op, run, a.unit)
    if unit["state"] != S.READY:
        fail(op, "unit_ready", f"unit {a.unit} is {unit['state']}")
    punit = plan_unit(ctx, run, a.unit)
    plan_units = {u["id"]: u for u in run["plan"]["units"]}
    active = [plan_units[uid] for uid in sorted(run["units"]) if run["units"][uid]["state"] in S.ACTIVE]
    try:
        assert_parallel_safe(punit, active)
    except ConflictError as exc:
        fail(op, "no_parallel_overlap", str(exc))
    routing = predict_routing(ctx, op, run, a.unit)
    paths = ctx.rt.worktrees(op).paths(a.run_id, a.unit)
    command = runner_command(ctx, op, punit, routing, paths["path"])
    worker = a.worker or unit.get("owner") or routing["runner"]
    doc = base_doc(op, run_id=a.run_id, unit_id=a.unit, unit_state=unit["state"], attempt=unit["attempts"] + 1,
                   owner=unit.get("owner"), worker=worker, routing=routing, runner_command=command,
                   worktree={"path": paths["path"], "branch": paths["branch"],
                             "base_commit": (run.get("metadata") or {}).get("base_commit"),
                             "exists": os.path.isdir(paths["path"])},
                   scope_files=list((punit.get("scope") or {}).get("files") or []),
                   acceptance_commands=list((punit.get("acceptance") or {}).get("commands") or []),
                   runner_timeout=ctx.cfg.runner_timeout, run_plan_sha256=run["plan_sha256"])
    return Plan(doc, checks=[check("unit_ready", True), check("no_parallel_overlap", True),
                             check("runner_selected", True, routing["runner"])], extra={"policy": run.get("policy")})


def _verification_summary(ver):
    if not isinstance(ver, dict):
        return None
    return {"passed": ver.get("passed"), "head": ver.get("head"), "changed_files": ver.get("changed_files"),
            "out_of_scope": ver.get("out_of_scope"),
            "gates": [{"name": g.get("name"), "status": g.get("status"), "exit_code": g.get("exit_code")}
                      for g in ver.get("gates") or []]}


def execute_dispatch(ctx, plan):
    op = "dispatch"
    doc = plan.doc
    run_id, unit_id = doc["run_id"], doc["unit_id"]
    c = ctx.rt.conductor(plan.extra["policy"], op, runner_env_allow=doc["runner_command"]["env_allow"])
    assigned = c.assign_unit(run_id, unit_id, doc["worker"])
    checks = list(assigned["checks"])
    if not assigned["ok"]:
        assigned["operation"] = op
        return assigned
    routing = (assigned.get("data") or {}).get("routing") or {}
    planned = doc["routing"]
    mismatch = [k for k in ("tier", "runner", "model") if routing.get(k) != planned.get(k)]
    if mismatch:
        c.manager.transition(run_id, c.id, unit_id, S.NEEDS_USER, reason="routing_changed_since_plan")
        return envelope(op, ok=False, changed=True,
                        checks=checks + [check("routing_matches_plan", False, ",".join(mismatch))],
                        required_user_actions=[{"kind": "resolve_needs_user", "run_id": run_id, "unit_id": unit_id,
                                                "detail": "routing changed after the plan was reviewed"}])
    checks.append(check("routing_matches_plan", True))
    started = c.start_unit(run_id, unit_id, doc["runner_command"]["argv"],
                           on_line=lambda stream, line: ctx.progress(f"[{unit_id} {stream}] {line}"))
    checks += started["checks"]
    if not started["ok"]:
        return envelope(op, ok=False, changed=True, checks=checks, warnings=started["warnings"],
                        required_user_actions=started["required_user_actions"])
    ctx.progress(f"runner started pid {started['data']['pid']} in {started['data']['worktree']}")
    try:
        done = c.wait_unit(run_id, unit_id)
    except KeyboardInterrupt:
        done = c.cancel_unit(run_id, unit_id, reason="operator_interrupt", terminal=False)
    checks += done["checks"]
    unit = c.manager.load(run_id)["units"][unit_id]
    data = {"state": unit["state"], "reason": unit.get("last_reason"), "pid": started["data"]["pid"],
            "log_path": started["data"]["log_path"], "worktree": started["data"]["worktree"],
            "verification": _verification_summary((done.get("data") or {}).get("verification"))}
    return envelope(op, ok=done["ok"], changed=True, checks=checks, warnings=done["warnings"],
                    required_user_actions=done["required_user_actions"], data=data)


# == verify ==
def gate_list(ctx, punit, policy):
    rc = ctx.rt.repo_checks(policy)
    gates = [{"name": f"acceptance[{i}]", "command": cmd}
             for i, cmd in enumerate((punit.get("acceptance") or {}).get("commands") or [])]
    gates += [{"name": name, "command": rc["commands"].get(name)} for name in rc["required"]]
    return gates


def plan_verify(ctx):
    op = "verify"
    a = ctx.args
    run = load_run(ctx, op, a.run_id)
    unit = load_unit(op, run, a.unit)
    if unit["state"] not in (S.VERIFYING, S.REVIEWING):
        fail(op, "unit_verifiable", f"unit {a.unit} is {unit['state']}; verify needs verifying or reviewing")
    ws = unit.get("workspace") or {}
    if not ws.get("path"):
        fail(op, "workspace_recorded", "unit has no worktree")
    repo = ctx.rt.repo_top(op)
    punit = plan_unit(ctx, run, a.unit)
    head = ((unit.get("annotations") or {}).get("verification") or {}).get("head")
    doc = base_doc(op, run_id=a.run_id, unit_id=a.unit, unit_state=unit["state"], worktree=ws["path"],
                   branch=ws.get("branch"), base_commit=ws.get("base_commit"), verified_head=head,
                   branch_tip=branch_tip(repo, ws.get("branch")), gates=gate_list(ctx, punit, run.get("policy")),
                   gate_timeout=ctx.cfg.gate_timeout, changes_state=False)
    return Plan(doc, extra={"policy": run.get("policy"), "unit": punit})


def execute_verify(ctx, plan):
    op = "verify"
    doc = plan.doc
    c = ctx.rt.conductor(plan.extra["policy"], op)
    repo = ctx.rt.repo_top(op)
    path, head = doc["worktree"], doc["verified_head"]
    exists = os.path.isdir(path)
    checks = [check("worktree_exists", exists, path), check("verified_head_recorded", is_sha(head), str(head))]
    tip = branch_tip(repo, doc["branch"])
    checks.append(check("branch_matches_verified_head", bool(head) and tip == head, f"tip={tip} verified={head}"))
    data = {"head": head, "branch_tip": tip}
    if exists:
        wt_head = git_out(["rev-parse", "HEAD"], path)
        checks.append(check("worktree_head_matches", wt_head == head, f"worktree={wt_head}"))
        dirty = git_out(["status", "--porcelain", "--untracked-files=all"], path)
        checks.append(check("work_committed", not dirty, dirty.splitlines()[0] if dirty else ""))
        verification = c.gate(plan.extra["unit"], path)
        checks.append(check("gates_passed", bool(verification["passed"]),
                            ", ".join(f"{g['name']}={g['status']}" for g in verification["gates"])))
        data["gates"] = _verification_summary(verification)["gates"]
    if tip:
        stats = diff_stats(repo, doc["branch"], doc["base_commit"])
        outside = out_of_scope_changes(stats["files"], (plan.extra["unit"].get("scope") or {}).get("files"))
        checks.append(check("changes_within_scope", not outside, ", ".join(outside)))
        data.update(changed_files=stats["files"], out_of_scope=outside)
    ok = all(ch["ok"] for ch in checks)
    return envelope(op, ok=ok, changed=False, checks=checks, data=data)


# == review ==
def plan_review(ctx):
    op = "review"
    a = ctx.args
    run = load_run(ctx, op, a.run_id)
    unit = load_unit(op, run, a.unit)
    if unit["state"] != S.REVIEWING:
        fail(op, "unit_reviewing", f"unit {a.unit} is {unit['state']}")
    repo = ctx.rt.repo_top(op)
    ws = unit.get("workspace") or {}
    head = ((unit.get("annotations") or {}).get("verification") or {}).get("head")
    routing = unit.get("routing") or {}
    review = {"reviewer": a.reviewer, "runner": a.runner, "model": a.model, "tier": a.tier, "verdict": a.verdict,
              "head": a.head, "notes": a.notes or ""}
    doc = base_doc(op, run_id=a.run_id, unit_id=a.unit, unit_state=unit["state"], verified_head=head,
                   branch_tip=branch_tip(repo, ws.get("branch")), review=review, session=a.session,
                   approval_ttl=ctx.cfg.approval_ttl, author=unit.get("owner"),
                   author_routing={k: routing.get(k) for k in ("runner", "model", "tier", "family")},
                   target_branch=ctx.cfg.target_branch)
    # Head binding, independence and the review tier are enforced by the
    # conductor at execution; the plan shows the verified head beside --head.
    return Plan(doc, extra={"policy": run.get("policy")})


def execute_review(ctx, plan):
    doc = plan.doc
    c = ctx.rt.conductor(plan.extra["policy"], "review")
    res = c.record_review(doc["run_id"], doc["unit_id"], dict(doc["review"]), session_id=doc["session"],
                          ttl_s=doc["approval_ttl"])
    res["operation"] = "review"
    return res


# == approvals approve / reject ==
def plan_decision(ctx):
    a = ctx.args
    verb = a.approvals_command
    op = f"approvals.{verb}"
    req = ctx.rt.broker.get(a.request_id)
    if req is None:
        fail(op, "request_known", a.request_id)
    request = {k: req.get(k) for k in ("run_id", "unit_id", "action", "plan_sha256", "head_sha", "status",
                                       "expires_at")}
    doc = base_doc(op, request_id=a.request_id, decision="approved" if verb == "approve" else "rejected",
                   decided_by=a.operator, session=a.session, request=request,
                   decision_inbox=ctx.cfg.decision_inbox)
    # Status and expiry are shown in the plan; the broker decides at execution.
    return Plan(doc, checks=[check("request_known", True, a.request_id)], extra={"request": req})


def execute_decision(ctx, plan):
    doc = plan.doc
    op = doc["operation"]
    req = plan.extra["request"]
    decision = {"request_id": doc["request_id"], "run_id": req["run_id"], "unit_id": req["unit_id"],
                "action": req["action"], "plan_sha256": req["plan_sha256"], "session_id": doc["session"],
                "decision": doc["decision"], "decided_by": doc["decided_by"]}
    inbox_dir = ctx.cfg.decision_inbox
    name = f"{int(ctx.clock() * 1000):013d}-{doc['request_id']}-{uuid.uuid4().hex[:8]}.json"
    atomic_write_json(os.path.join(inbox_dir, name), decision)
    results = DecisionInbox(inbox_dir, ctx.rt.broker).process()
    mine = next((r for r in results if r.get("source_file") == name), None)
    if mine is None:
        return envelope(op, ok=False, changed=True, checks=[check("decision_processed", False, name)])
    others = [r.get("source_file") for r in results if r is not mine]
    warnings = list(mine.get("warnings") or [])
    if others:
        warnings.append(f"also processed {len(others)} other decision file(s) waiting in the inbox")
    data = {"decision": {"request_id": doc["request_id"], "decision": doc["decision"],
                         "decided_by": doc["decided_by"]},
            "inbox_file": name, "result": mine.get("data")}
    return envelope(op, ok=mine["ok"], changed=True, checks=mine["checks"], warnings=warnings, data=data)


# == merge ==
def plan_merge(ctx):
    op = "merge"
    a = ctx.args
    run = load_run(ctx, op, a.run_id)
    unit = load_unit(op, run, a.unit)
    if unit["state"] not in (S.REVIEWING, S.QUEUED_FOR_MERGE):
        fail(op, "unit_mergeable", f"unit {a.unit} is {unit['state']}")
    repo = ctx.rt.repo_top(op)
    ws = unit.get("workspace") or {}
    annotations = unit.get("annotations") or {}
    review = annotations.get("review") or {}
    rid = review.get("approval_request_id")
    req = ctx.rt.broker.get(rid) if rid else None
    queue = ctx.rt.queue_state() or {}
    ahead = [{"run_id": i.get("run_id"), "unit_id": i.get("unit_id")} for i in queue.get("items") or []]
    doc = base_doc(op, run_id=a.run_id, unit_id=a.unit, unit_state=unit["state"], branch=ws.get("branch"),
                   verified_head=(annotations.get("verification") or {}).get("head"),
                   branch_tip=branch_tip(repo, ws.get("branch")), target_branch=ctx.cfg.target_branch,
                   target_tip=branch_tip(repo, ctx.cfg.target_branch), approval_request_id=rid,
                   approval_status=(req or {}).get("status"), queue_ahead=ahead, push=False,
                   run_plan_sha256=run["plan_sha256"])
    return Plan(doc, destructive=True, extra={"policy": run.get("policy"), "review": review, "request": req})


def authorize_merge(ctx, plan, digest):
    """A merge is admitted only by the merge approval resolved through the
    ApprovalBroker (the queue consumes it). An interactive yes cannot
    replace it, and --yes is ignored."""
    op = "merge"
    doc = plan.doc
    if doc["unit_state"] == S.QUEUED_FOR_MERGE:
        return [check("broker_approval_admitted", True, "consumed when the unit was enqueued")], None
    rid = doc["approval_request_id"]
    run_id, unit_id = doc["run_id"], doc["unit_id"]
    if not rid:
        return [], envelope(op, ok=False, checks=[check("broker_approval_admitted", False, "no approved review")],
                            required_user_actions=[{"kind": "record_review", "run_id": run_id, "unit_id": unit_id,
                                                    "detail": "record an approving review to request a merge "
                                                              "approval"}])
    review = plan.extra["review"]
    usable = ctx.rt.broker.usable(rid, run_id=run_id, unit_id=unit_id, action="merge",
                                  plan_sha256=doc["run_plan_sha256"], session_id=review.get("session_id"),
                                  head_sha=doc["verified_head"])
    failed = [f"{c['name']}:{c['detail']}" for c in usable if not c["ok"]]
    if not failed:
        return [check("broker_approval_admitted", True, rid)], None
    req = plan.extra["request"] or {}
    if req.get("status") == "pending" and ctx.clock() < float(req.get("expires_at") or 0):
        action = {"kind": "approve", "run_id": run_id, "unit_id": unit_id, "request_id": rid, "action": "merge",
                  "expires_at": req.get("expires_at"), "detail": req.get("summary", "")}
    else:
        action = {"kind": "reverify_and_reapprove", "run_id": run_id, "unit_id": unit_id, "request_id": rid,
                  "detail": f"merge approval is {req.get('status') or 'unknown'}; review again to request a new one"}
    return [], envelope(op, ok=False, checks=[check("broker_approval_admitted", False, "; ".join(failed))],
                        required_user_actions=[action])


def execute_merge(ctx, plan):
    op = "merge"
    doc = plan.doc
    run_id, unit_id = doc["run_id"], doc["unit_id"]
    c = ctx.rt.conductor(plan.extra["policy"], op)
    checks, warnings = [], []
    if doc["unit_state"] == S.REVIEWING:
        queued = c.enqueue_merge(run_id, unit_id)
        checks += queued["checks"]
        warnings += queued["warnings"]
        if not queued["ok"]:
            return envelope(op, ok=False, changed=queued["changed"], checks=checks, warnings=warnings,
                            required_user_actions=queued["required_user_actions"])
    mine, before = None, []
    for _ in range(len(doc["queue_ahead"]) + 2):
        res = c.process_merge_queue()
        data = res.get("data") or {}
        if data.get("status") in (None, "empty"):
            break
        summary = {k: data.get(k) for k in ("run_id", "unit_id", "status", "merged_commit", "previous_commit",
                                             "conflicts", "detail")}
        if data.get("run_id") == run_id and data.get("unit_id") == unit_id:
            mine = (res, summary)
            break
        before.append(summary)
        warnings += list(res.get("warnings") or [])
    if mine is None:
        return envelope(op, ok=False, changed=True, checks=checks + [check("unit_processed", False)],
                        warnings=warnings, data={"processed_before": before})
    res, summary = mine
    state = c.manager.load(run_id)["units"][unit_id]["state"]
    return envelope(op, ok=res["ok"], changed=True, checks=checks + res["checks"],
                    warnings=warnings + list(res.get("warnings") or []),
                    required_user_actions=res["required_user_actions"], rollback_checkpoint=res["rollback_checkpoint"],
                    data={"result": summary, "processed_before": before, "state": state})


# == cleanup ==
def plan_cleanup(ctx):
    op = "cleanup"
    a = ctx.args
    run = load_run(ctx, op, a.run_id)
    unit = load_unit(op, run, a.unit)
    if unit["state"] in S.ACTIVE:
        fail(op, "unit_not_active", f"unit {a.unit} is {unit['state']}; cleanup only stopped or finished units")
    repo = ctx.rt.repo_top(op)
    wt = ctx.rt.worktrees(op)
    res = wt.cleanup(a.run_id, a.unit, ctx.cfg.target_branch, archive=a.archive, dry_run=True)
    if not res["ok"]:
        res["operation"] = op
        raise CommandFailed(res)
    d = res["data"]
    destructive = bool(d.get("bundle") or d.get("ignored_archive"))
    doc = base_doc(op, run_id=a.run_id, unit_id=a.unit, unit_state=unit["state"], archive=bool(a.archive),
                   path=d["path"], branch=d["branch"], branch_tip=branch_tip(repo, d["branch"]),
                   unique_commits=d["unique_commits"], orphaned_commits=d["orphaned_commits"],
                   ignored_files=d["ignored_files"], bundle=d["bundle"], ignored_archive=d["ignored_archive"],
                   commands=d["commands"], target_branch=ctx.cfg.target_branch)
    warnings = []
    if a.approval_request and not destructive:
        warnings.append("--approval-request ignored: this cleanup plan is not destructive")
    return Plan(doc, destructive=destructive, checks=res["checks"], warnings=warnings)


def authorize_cleanup(ctx, plan, digest):
    """Archiving then removing unique work: a consumed broker approval bound
    to this plan hash, or an interactive confirmation (the word yes)."""
    op = "cleanup"
    a, doc = ctx.args, plan.doc
    head = doc["branch_tip"] if is_sha(doc["branch_tip"]) else None
    if a.approval_request:
        if not a.session:
            raise UsageError("--approval-request needs --session (the session bound to the request)")
        res = ctx.rt.broker.consume(a.approval_request, run_id=doc["run_id"], unit_id=doc["unit_id"],
                                    action="cleanup", plan_sha256=digest, session_id=a.session, head_sha=head)
        if res["ok"]:
            return [check("broker_approval_admitted", True, a.approval_request)], None
        return [], envelope(op, ok=False, checks=[check("broker_approval_admitted", False, ", ".join(
            f"{c['name']}:{c['detail']}" for c in res["checks"] if not c["ok"]))])
    if ctx.is_tty():
        if ctx.confirm(plan, digest, destructive=True):
            return [check("operator_confirmed", True, "interactive, destructive")], None
        return [], envelope(op, ok=False, checks=[check("operator_confirmed", False, "declined")])
    if not a.session:
        raise UsageError("a destructive cleanup outside a TTY requests an ApprovalBroker approval and needs "
                         "--session")
    count = len(doc["unique_commits"]) + len(doc["orphaned_commits"])
    summary = (f"cleanup {doc['unit_id']}: archive {count} commit(s) and {len(doc['ignored_files'])} ignored "
               f"file(s), then remove the worktree and branch (plan {digest[:12]})")
    req = ctx.rt.broker.request(run_id=doc["run_id"], unit_id=doc["unit_id"], action="cleanup", plan_sha256=digest,
                                session_id=a.session, ttl_s=ctx.cfg.approval_ttl, summary=summary, head_sha=head)
    if not req["ok"]:
        return [], envelope(op, ok=False, checks=req["checks"])
    actions = [dict(act, action="cleanup", expires_at=req["data"]["expires_at"])
               for act in req["required_user_actions"]]
    return [], envelope(op, ok=False, changed=True,
                        checks=[check("broker_approval_admitted", False,
                                      "approval requested; approve it, then re-run with --approval-request")],
                        required_user_actions=actions,
                        data={"approval_request": {"request_id": req["data"]["request_id"],
                                                   "expires_at": req["data"]["expires_at"]}})


def execute_cleanup(ctx, plan):
    doc = plan.doc
    res = ctx.rt.worktrees("cleanup").cleanup(doc["run_id"], doc["unit_id"], ctx.cfg.target_branch,
                                              archive=doc["archive"])
    res["operation"] = "cleanup"
    return res


class Spec:
    def __init__(self, plan, execute, authorize=None, lease=False):
        self.plan = plan
        self.execute = execute
        self.authorize = authorize
        self.lease = lease

    def lease_run(self, plan):
        return plan.doc.get("run_id") if self.lease else None


GATED = {
    "create-run": Spec(plan_create_run, execute_create_run),
    "dispatch": Spec(plan_dispatch, execute_dispatch, lease=True),
    "verify": Spec(plan_verify, execute_verify),
    "review": Spec(plan_review, execute_review, lease=True),
    "merge": Spec(plan_merge, execute_merge, authorize=authorize_merge, lease=True),
    "cleanup": Spec(plan_cleanup, execute_cleanup, authorize=authorize_cleanup),
    "approvals.approve": Spec(plan_decision, execute_decision),
    "approvals.reject": Spec(plan_decision, execute_decision),
}
