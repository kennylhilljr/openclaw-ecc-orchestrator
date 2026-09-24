"""`ecc-orchestrator` / `python3 -m openclaw_ecc_orchestrator`: the operator CLI.

Exit codes: 0 ok, 1 operation failed, 2 usage error, 3 approval or user
action required. See docs/cli.md.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from .. import __version__
from ..plugin.approvals import plan_digest
from ..plugin.summary import summarize_required_user_actions
from ..runs.envelope import SCHEMA_VERSION, check, envelope
from ..runs.fsutil import atomic_write_json, read_jsonl
from ..runs.store import iso
from ..worktrees.git import git_out, toplevel
from ..worktrees.manager import validate_root
from . import output
from .commands import GATED, load_run
from .config import UsageError, checked_path, forbidden_location, load_config, valid_handle, valid_id
from .reviews import ReviewStore
from .runtime import CommandFailed, Runtime

PROG = "ecc-orchestrator"
MIN_GIT = (2, 38)
MIN_PYTHON = (3, 11)


# == argument parsing ==
class _Parser(argparse.ArgumentParser):
    out = None

    def error(self, message):
        raise UsageError(f"{self.format_usage().strip()}\n{self.prog}: error: {message}")

    def _print_message(self, message, file=None):
        if message:
            (self.out or file or sys.stdout).write(message)


def _id(value):
    if not valid_id(value):
        raise argparse.ArgumentTypeError(f"invalid identifier {value!r} (letters, digits, . _ -; no '..')")
    return value


def _handle(value):
    if not valid_handle(value):
        raise argparse.ArgumentTypeError(f"invalid handle {value!r} (a short display id, not an email or secret)")
    return value


def _nonneg_int(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from None
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or more")
    return number


def _nonneg_float(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from None
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or more")
    return number


def _common(parser):
    g = parser.add_argument_group("configuration")
    g.add_argument("--config", help="JSON config file (paths inside resolve relative to it)")
    g.add_argument("--state-dir", dest="state_dir")
    g.add_argument("--worktree-root", dest="worktree_root")
    g.add_argument("--repo")
    g.add_argument("--target-branch", dest="target_branch")
    g.add_argument("--policy-file", dest="policy_file",
                   help="repository policy; JSON compatible YAML only (default <repo>/.orchestration/config.yaml)")
    g.add_argument("--event-log", dest="event_log")
    g.add_argument("--decision-inbox", dest="decision_inbox")
    g.add_argument("--certifications", help="JSON list of runner certification records")
    o = parser.add_argument_group("output")
    o.add_argument("--json", action="store_true", help="print the result envelope as JSON")
    o.add_argument("--verbose", action="store_true", help="print every check, the data, and runner output")


def _mutating(parser):
    m = parser.add_argument_group("safety")
    m.add_argument("--dry-run", action="store_true", help="print the plan and its hash; change nothing")
    m.add_argument("--yes", action="store_true", help="skip the prompt; requires --review-id (non destructive only)")
    m.add_argument("--review-id", dest="review_id", type=_id, help="review record approving this exact plan")
    m.add_argument("--backup-dir", dest="backup_dir", help="copy affected state and refs here before executing")


def _unit(parser):
    parser.add_argument("--run-id", dest="run_id", type=_id, required=True)
    parser.add_argument("--unit", type=_id, required=True)


def build_parser():
    parser = _Parser(prog=PROG, description="Operator CLI for the OpenClaw ECC orchestrator runtime.")
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<subcommand>")

    p = sub.add_parser("create-run", help="validate a plan and create a run")
    p.add_argument("--run-id", dest="run_id", type=_id, required=True)
    p.add_argument("--plan", required=True, help="plan JSON: {\"units\": [...]} or a list of work units")
    _common(p), _mutating(p)

    p = sub.add_parser("status", help="runs, units, budgets, pending approvals, required user actions")
    p.add_argument("--run-id", dest="run_id", type=_id)
    _common(p)

    p = sub.add_parser("dispatch", help="route, assign and run one ready unit through verification")
    _unit(p)
    p.add_argument("--worker", type=_id, help="owner id (default: the unit owner or the selected runner)")
    p.add_argument("--runner-command-json", dest="runner_command_json",
                   help="TEST ONLY: JSON argv replacing the registry command; needs --allow-test-runner")
    p.add_argument("--allow-test-runner", dest="allow_test_runner", action="store_true",
                   help="honor --runner-command-json")
    _common(p), _mutating(p)

    p = sub.add_parser("verify", help="re-run gates and head and scope checks for a verified unit")
    _unit(p)
    _common(p), _mutating(p)

    p = sub.add_parser("review", help="record an independent review; an approval requests a merge approval")
    _unit(p)
    p.add_argument("--reviewer", type=_id, required=True)
    p.add_argument("--runner", required=True, type=_id, help="runner that produced the review")
    p.add_argument("--model", required=True)
    p.add_argument("--tier", type=int, choices=(0, 1, 2), required=True)
    p.add_argument("--verdict", choices=("approved", "changes_requested", "rejected"), required=True)
    p.add_argument("--head", required=True, help="commit the reviewer approved (the verified head)")
    p.add_argument("--session", type=_handle, required=True, help="session bound to the merge approval")
    p.add_argument("--notes", default="")
    _common(p), _mutating(p)

    p = sub.add_parser("merge", help="enqueue and merge a reviewed unit (destructive)")
    _unit(p)
    _common(p), _mutating(p)

    p = sub.add_parser("cleanup", help="remove a stopped unit's worktree and branch")
    _unit(p)
    p.add_argument("--archive", action="store_true", help="bundle unique work first (destructive plan)")
    p.add_argument("--session", type=_handle, help="session for the broker approval of a destructive cleanup")
    p.add_argument("--approval-request", dest="approval_request", type=_id,
                   help="approved broker request admitting this destructive cleanup")
    _common(p), _mutating(p)

    p = sub.add_parser("approvals", help="list, approve or reject approval requests")
    asub = p.add_subparsers(dest="approvals_command", required=True, metavar="<action>")
    q = asub.add_parser("list", help="pending approval requests")
    q.add_argument("--run-id", dest="run_id", type=_id)
    _common(q)
    for verb in ("approve", "reject"):
        q = asub.add_parser(verb, help=f"{verb} a request through the decision inbox")
        q.add_argument("request_id", type=_id)
        q.add_argument("--operator", type=_handle, required=True, help="your operator handle (never inferred)")
        q.add_argument("--session", type=_handle, required=True, help="your session id (must match the request)")
        _common(q), _mutating(q)

    p = sub.add_parser("events", help="read the OpenClaw event log")
    esub = p.add_subparsers(dest="events_command", required=True, metavar="<action>")
    q = esub.add_parser("tail", help="print events (redacted as stored)")
    q.add_argument("--since-seq", dest="since_seq", type=_nonneg_int, default=0)
    q.add_argument("--run-id", dest="run_id", type=_id)
    q.add_argument("--limit", type=_nonneg_int, help="only the last N events of the first read")
    q.add_argument("--follow", action="store_true", help="keep polling for new events")
    q.add_argument("--interval", type=_nonneg_float, default=1.0, help="poll interval in seconds")
    q.add_argument("--max-polls", dest="max_polls", type=_nonneg_int, help="stop following after N polls")
    _common(q)

    p = sub.add_parser("doctor", help="local self checks")
    _common(p)

    p = sub.add_parser("review-plan", help="print a plan and its hash; --approve writes a review record")
    p.add_argument("--approve", action="store_true")
    p.add_argument("--operator", type=_handle, help="approving operator handle (never inferred)")
    p.add_argument("--session", type=_handle, help="approving operator session id")
    p.add_argument("--review-id", dest="review_id", type=_id, help="id for the new record (default generated)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("inner", nargs=argparse.REMAINDER, metavar="-- <subcommand> [args]")
    return parser


def operation_of(args):
    if args.command == "approvals":
        return f"approvals.{args.approvals_command}"
    if args.command == "events":
        return f"events.{args.events_command}"
    return args.command


# == context ==
class Ctx:
    def __init__(self, args, cfg, *, stdin, stdout, stderr, env, is_tty, clock, cwd):
        self.args = args
        self.cfg = cfg
        self.stdin, self.stdout, self.stderr = stdin, stdout, stderr
        self.env = env
        self._is_tty = is_tty
        self.clock = clock
        self.cwd = cwd
        self.rt = Runtime(cfg, clock) if cfg is not None else None

    def is_tty(self):
        try:
            return bool(self._is_tty())
        except Exception:
            return False

    def progress(self, line):
        if getattr(self.args, "verbose", False):
            self.stderr.write(output.redact_line(line) + "\n")
            self.stderr.flush()

    def confirm(self, plan, digest, destructive=False):
        """Show the plan on stderr and ask. Destructive plans need the word yes."""
        text = json.dumps(output.redact(plan.doc), indent=2, sort_keys=True)
        self.stderr.write(output.redact_line(f"{plan.operation} plan (sha256 {digest}):") + "\n")
        for line in text.splitlines():
            self.stderr.write(output.redact_line("  " + line) + "\n")
        if destructive:
            question = "This plan is DESTRUCTIVE. Type yes to proceed: "
        else:
            question = "Proceed? [y/N]: "
        self.stderr.write(question)
        self.stderr.flush()
        answer = (self.stdin.readline() or "").strip().lower()
        return answer == "yes" if destructive else answer in ("y", "yes")


def _validate_gated_args(args, ctx_cwd):
    if args.yes and not args.review_id:
        raise UsageError("--yes requires --review-id (a review record from review-plan --approve)")
    if args.review_id and not args.yes:
        raise UsageError("--review-id is only used together with --yes")
    args.runner_command_argv = None
    raw = getattr(args, "runner_command_json", None)
    if raw is not None:
        if not getattr(args, "allow_test_runner", False):
            raise UsageError("--runner-command-json is honored only with --allow-test-runner")
        try:
            argv = json.loads(raw)
        except ValueError:
            raise UsageError("--runner-command-json must be a JSON list of strings") from None
        if not (isinstance(argv, list) and argv and all(isinstance(x, str) and x for x in argv)):
            raise UsageError("--runner-command-json must be a non-empty JSON list of strings")
        args.runner_command_argv = argv
    args.backup_path = checked_path(args.backup_dir, "--backup-dir", ctx_cwd) if args.backup_dir else None


# == gated execution ==
def build_plan(ctx, spec):
    plan = spec.plan(ctx)
    repo = ctx.rt.repo_top(plan.operation)
    if ctx.args.backup_path:
        reason = forbidden_location(ctx.args.backup_path, ctx.cfg.home, repo)
        if reason:
            raise UsageError(f"--backup-dir is {reason}")
    plan.doc["context"] = {"state_dir": ctx.cfg.state_dir, "repo": repo, "target_branch": ctx.cfg.target_branch,
                           "conductor_id": ctx.cfg.conductor_id, "event_log": ctx.cfg.event_log}
    plan.doc["backup_dir"] = ctx.args.backup_path
    return plan


def make_backup(ctx, plan, digest):
    """Copy the state this plan may change, and the branch refs, aside."""
    root = ctx.args.backup_path
    op = plan.operation
    dest = os.path.join(root, f"{op.replace('.', '-')}-{int(ctx.clock())}-{uuid.uuid4().hex[:8]}")
    os.makedirs(dest)
    files = []
    run_id = plan.doc.get("run_id")
    if run_id:
        src = os.path.join(ctx.cfg.runs_dir, run_id)
        if os.path.isdir(src):
            target = os.path.join(dest, "state", "runs", run_id)
            shutil.copytree(src, target, ignore=shutil.ignore_patterns(".lock", ".tmp-*"))
            files.append(os.path.relpath(target, dest))
    for path in (ctx.cfg.approvals_path, ctx.cfg.queue_path):
        if os.path.isfile(path):
            target = os.path.join(dest, "state", os.path.basename(path))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(path, target)
            files.append(os.path.relpath(target, dest))
    refs = {}
    repo = ctx.rt.repo_top(op)
    for line in git_out(["for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"], repo).splitlines():
        name, _, sha = line.partition(" ")
        refs[name] = sha
    manifest = {"schema_version": SCHEMA_VERSION, "operation": op, "plan_sha256": digest,
                "created_at": iso(ctx.clock()), "state_dir": ctx.cfg.state_dir, "repo": repo, "files": files,
                "refs": refs,
                "restore": "copy the files under state/ back into the state dir and reset each ref with "
                           "git update-ref <ref> <sha>"}
    atomic_write_json(os.path.join(dest, "manifest.json"), manifest)
    return {"path": dest, "files": files, "refs": refs}


def run_gated(ctx, op):
    spec = GATED[op]
    a = ctx.args
    plan = build_plan(ctx, spec)
    digest = plan_digest(plan.doc)
    base_data = {"plan": plan.doc, "plan_sha256": digest, "destructive": plan.destructive}
    checks, warnings = list(plan.checks), list(plan.warnings)

    def refuse(env_, extra_checks=()):
        env_["checks"] = checks + list(extra_checks) + list(env_.get("checks") or [])
        env_["warnings"] = warnings + list(env_.get("warnings") or [])
        env_["data"] = dict(base_data, **(env_.get("data") or {}))
        env_["_summary"] = [f"plan sha256 {digest}" + (" (destructive)" if plan.destructive else "")]
        return env_

    if a.dry_run:
        env_ = envelope(op, ok=True, changed=False, checks=checks + [check("dry_run", True, "nothing changed")],
                        warnings=warnings, data=dict(base_data, dry_run=True))
        text = json.dumps(output.redact(plan.doc), indent=2, sort_keys=True)
        env_["_summary"] = [f"dry run: plan sha256 {digest}" + (" (destructive)" if plan.destructive else "")] + \
            text.splitlines()
        return env_
    review_id = None
    if plan.destructive:
        if a.yes:
            warnings.append("--yes ignored: destructive plans need an interactive confirmation or an approval "
                            "admitted through the ApprovalBroker")
        auth_checks, refusal = spec.authorize(ctx, plan, digest)
        if refusal is not None:
            return refuse(refusal)
        checks += auth_checks
    elif a.yes:
        store = ReviewStore(ctx.cfg.reviews_dir)
        rchecks, _ = store.validate(a.review_id, op, digest, ctx.clock())
        checks += rchecks
        if not all(c["ok"] for c in rchecks):
            return refuse(envelope(op, ok=False, required_user_actions=[{
                "kind": "review_plan", "run_id": plan.doc.get("run_id"), "unit_id": plan.doc.get("unit_id"),
                "plan_sha256": digest,
                "detail": "create a review record for the current plan: review-plan --approve --operator <handle> "
                          "--session <id> -- <command>"}]))
        review_id = a.review_id
    elif ctx.is_tty():
        if not ctx.confirm(plan, digest):
            return refuse(envelope(op, ok=False), [check("operator_confirmed", False, "declined")])
        checks.append(check("operator_confirmed", True, "interactive"))
    else:
        return refuse(envelope(op, ok=False, required_user_actions=[{
            "kind": "confirm_plan", "run_id": plan.doc.get("run_id"), "unit_id": plan.doc.get("unit_id"),
            "plan_sha256": digest,
            "detail": "not a TTY: re-run interactively, or approve the plan with review-plan --approve and pass "
                      "--yes --review-id"}]), [check("operator_confirmed", False, "stdin is not a TTY and no --yes")])

    backup = make_backup(ctx, plan, digest) if a.backup_path else None
    run_id = spec.lease_run(plan)
    if run_id:
        lease = ctx.rt.manager.acquire_lease(run_id, ctx.cfg.conductor_id)
        if not lease["ok"]:
            return refuse(envelope(op, ok=False, checks=lease["checks"]))
    try:
        again = build_plan(ctx, spec)
    except CommandFailed as exc:
        return refuse(exc.envelope, [check("plan_hash_stable", False, "plan could not be rebuilt")])
    again_digest = plan_digest(again.doc)
    if again_digest != digest:
        return refuse(envelope(op, ok=False), [check("plan_hash_stable", False,
                                                     f"approved={digest} current={again_digest}")])
    checks.append(check("plan_hash_stable", True, digest))
    if review_id and not ReviewStore(ctx.cfg.reviews_dir).consume(review_id, ctx.clock(), op):
        return refuse(envelope(op, ok=False, required_user_actions=[{"kind": "review_plan", "detail": "the review "
                                                                     "record was used concurrently"}]),
                      [check("review_not_consumed", False, "already used")])
    result = spec.execute(ctx, again)
    result["checks"] = checks + list(result.get("checks") or [])
    result["warnings"] = warnings + list(result.get("warnings") or [])
    rollback = result.get("rollback_checkpoint")
    if backup:
        rollback = dict(rollback or {}, backup=backup)
    result["rollback_checkpoint"] = rollback
    result["data"] = dict(result.get("data") or {}, plan=plan.doc, plan_sha256=digest,
                          review_id=review_id)
    result["operation"] = op
    result["_summary"] = [f"plan sha256 {digest}"] + _result_lines(op, result.get("data") or {})
    return result


def _result_lines(op, data):
    """One or two operation specific lines for the human summary."""
    if op == "dispatch":
        ver = data.get("verification") or {}
        return [f"unit is {data.get('state')} ({data.get('reason')}); log {data.get('log_path')}",
                f"verified head {ver.get('head')}" if ver.get("head") else ""]
    if op == "review":
        req = data.get("approval_request") or {}
        return [f"merge approval requested: {req.get('request_id')}"] if req else []
    if op.startswith("approvals."):
        dec = data.get("decision") or {}
        return [f"request {dec.get('request_id')} {dec.get('decision')} by {dec.get('decided_by')}"]
    if op == "merge":
        res = data.get("result") or {}
        return [f"{res.get('status')}: {res.get('merged_commit') or res.get('detail') or ''}".strip()]
    if op == "create-run":
        return [f"run {data.get('run_id')} ready: {', '.join(data.get('ready') or []) or 'none'}"]
    return []


# == read only commands ==
def _unit_view(run, uid):
    unit = run["units"][uid]
    routing = unit.get("routing") or {}
    budget = unit.get("budget") or {}
    annotations = unit.get("annotations") or {}
    return {
        "unit_id": uid, "state": unit["state"], "reason": unit.get("last_reason"), "owner": unit.get("owner"),
        "tier": routing.get("tier"), "runner": routing.get("runner"), "model": routing.get("model"),
        "attempts": unit.get("attempts", 0), "attempts_by_tier": unit.get("attempts_by_tier") or {},
        "budget": {"attempts_per_tier": budget.get("attempts", budget.get("max_attempts")),
                   "minutes": budget.get("minutes", budget.get("max_minutes")),
                   "maximum_cost_usd": budget.get("maximum_cost_usd")},
        "used": {"minutes": round(float(unit.get("minutes_used") or 0.0), 4),
                 "cost_usd": round(float(unit.get("cost_usd") or 0.0), 6)},
        "verified_head": (annotations.get("verification") or {}).get("head"),
        "approval_request_id": (annotations.get("review") or {}).get("approval_request_id"),
        "depends_on": unit.get("depends_on") or [],
    }


def _approval_view(req):
    return {"request_id": req.get("request_id"), "run_id": req.get("run_id"), "unit_id": req.get("unit_id"),
            "action": req.get("action"), "head_sha": req.get("head_sha"), "plan_sha256": req.get("plan_sha256"),
            "expires_at": req.get("expires_at"), "requesting_session": req.get("session_id"),
            "summary": req.get("summary", ""), "status": req.get("status")}


def cmd_status(ctx):
    op = "status"
    rt = ctx.rt
    if not ctx.args.run_id:
        runs = []
        for run_id in rt.store.list_runs():
            if not rt.store.exists(run_id):
                continue
            run = load_run(ctx, op, run_id)
            states = {}
            for unit in run["units"].values():
                states[unit["state"]] = states.get(unit["state"], 0) + 1
            runs.append({"run_id": run_id, "created_at": run.get("created_at"), "units": len(run["units"]),
                         "states": states, "pending_approvals": len(rt.broker.pending(run_id))})
        env = envelope(op, data={"runs": runs, "state_dir": ctx.cfg.state_dir})
        env["_summary"] = [f"{r['run_id']}: {r['units']} unit(s) " + ", ".join(
            f"{k}={v}" for k, v in sorted(r["states"].items())) for r in runs] or ["no runs"]
        return env
    run = load_run(ctx, op, ctx.args.run_id)
    pending = rt.broker.pending(run["run_id"])
    actions = summarize_required_user_actions(run, pending, queue_state=rt.queue_state(), now=ctx.clock())
    order = run.get("order") or sorted(run["units"])
    units = [_unit_view(run, uid) for uid in order]
    lease = run.get("lease") or {}
    queue = [{"run_id": i.get("run_id"), "unit_id": i.get("unit_id")} for i in (rt.queue_state() or {}).get("items", [])]
    data = {"run_id": run["run_id"], "plan_sha256": run.get("plan_sha256"), "created_at": run.get("created_at"),
            "target_branch": (run.get("metadata") or {}).get("target_branch"),
            "base_commit": (run.get("metadata") or {}).get("base_commit"),
            "lease": {"conductor_id": lease.get("conductor_id"), "expires_at": lease.get("expires_at")},
            "units": units, "pending_approvals": [_approval_view(r) for r in pending], "merge_queue": queue}
    env = envelope(op, required_user_actions=actions, data=data)
    lines = [f"run {run['run_id']} target {data['target_branch']} base {str(data['base_commit'])[:12]}"]
    for u in units:
        cost = f"{u['used']['cost_usd']:.2f}/{u['budget']['maximum_cost_usd']}"
        minutes = f"{u['used']['minutes']:.1f}/{u['budget']['minutes']}"
        lines.append(f"{u['unit_id']:<16} {u['state']:<17} tier {u['tier'] if u['tier'] is not None else '-'} "
                     f"runner {u['runner'] or '-'} attempts {u['attempts']} cost {cost} minutes {minutes}")
    for p in data["pending_approvals"]:
        lines.append(f"pending approval {p['request_id']} {p['action']} {p['unit_id']}")
    env["_summary"] = lines
    return env


def cmd_approvals_list(ctx):
    pending = [_approval_view(r) for r in ctx.rt.broker.pending(ctx.args.run_id)]
    pending.sort(key=lambda r: (r["run_id"] or "", r["unit_id"] or "", r["request_id"] or ""))
    env = envelope("approvals.list", data={"pending": pending})
    env["_summary"] = [f"{p['request_id']} {p['action']} {p['run_id']}/{p['unit_id']} head "
                       f"{str(p['head_sha'])[:12]} session {p['requesting_session']}" for p in pending] or [
        "no pending approvals"]
    return env


def _event_line(event):
    unit = f"/{event.get('unit_id')}" if event.get("unit_id") else ""
    data = json.dumps(event.get("data") or {}, sort_keys=True, separators=(",", ":"), default=str)
    return f"{event.get('seq')} {event.get('emitted_at')} {event.get('type')} {event.get('run_id')}{unit} {data}"


def cmd_events_tail(ctx):
    a = ctx.args
    path = ctx.cfg.event_log

    def read(after):
        events = [e for e in read_jsonl(path) if int(e.get("seq", 0)) > after] if os.path.exists(path) else []
        if a.run_id:
            events = [e for e in events if e.get("run_id") == a.run_id]
        return events

    events = read(a.since_seq)
    if a.limit is not None:
        events = events[-a.limit:] if a.limit else []
    if not a.follow:
        last = max([a.since_seq] + [int(e.get("seq", 0)) for e in events])
        env = envelope("events.tail", data={"events": events, "last_seq": last, "event_log": path})
        env["_summary"] = [_event_line(e) for e in events] or ["no events"]
        env["_raw_lines"] = True
        return env
    last = a.since_seq
    polls = 0
    try:
        while True:
            for event in events:  # BrokenPipeError is handled by main()
                last = max(last, int(event.get("seq", 0)))
                if a.json:
                    output.write_json(ctx.stdout, event)
                else:
                    ctx.stdout.write(output.redact_line(_event_line(event)) + "\n")
                    ctx.stdout.flush()
            polls += 1
            if a.max_polls is not None and polls >= a.max_polls:
                break
            time.sleep(a.interval)
            events = read(last)
    except KeyboardInterrupt:
        pass
    return None


def _git_version():
    try:
        proc = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=30,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", proc.stdout)
    if proc.returncode != 0 or not match:
        return None, proc.stdout.strip() or proc.stderr.strip()
    return tuple(int(x or 0) for x in match.groups()), proc.stdout.strip()


def _writable(path):
    """(ok, detail) without creating `path`: probe inside it if it exists,
    else check the nearest existing ancestor."""
    if os.path.isdir(path):
        try:
            fd, probe = tempfile.mkstemp(prefix=".doctor-", dir=path)
            os.close(fd)
            os.unlink(probe)
            return True, path
        except OSError as exc:
            return False, f"{path}: {exc.strerror}"
    if os.path.exists(path):
        return False, f"{path} exists and is not a directory"
    parent = path
    while not os.path.exists(parent):
        parent = os.path.dirname(parent)
    ok = os.path.isdir(parent) and os.access(parent, os.W_OK | os.X_OK)
    return ok, f"{path} does not exist; would be created under {parent}" if ok else f"{parent} is not writable"


def cmd_doctor(ctx):
    cfg = ctx.cfg
    checks = []
    py = sys.version_info[:3]
    checks.append(check("python_version", py >= MIN_PYTHON, ".".join(map(str, py))))
    version, text = _git_version()
    checks.append(check("git_available", version is not None, text))
    checks.append(check("git_version", version is not None and version[:2] >= MIN_GIT,
                        f"{text}; merge-tree --write-tree needs >= {MIN_GIT[0]}.{MIN_GIT[1]}"))
    repo = None
    try:
        repo = toplevel(cfg.repo)
        checks.append(check("repo_is_git", True, repo))
    except Exception as exc:
        checks.append(check("repo_is_git", False, f"{cfg.repo}: {getattr(exc, 'stderr', '') or exc}".strip()))
    reasons = [f"{key}: {r}" for key in ("state_dir", "event_log", "decision_inbox")
               for r in [forbidden_location(getattr(cfg, key), cfg.home, repo)] if r]
    checks.append(check("state_dir_location", not reasons, "; ".join(reasons) or cfg.state_dir))
    ok, detail = _writable(cfg.state_dir)
    checks.append(check("state_dir_writable", ok, detail))
    if repo:
        res = validate_root(cfg.worktree_root, repo)
        failed = [f"{c['name']}: {c['detail']}" for c in res["checks"] if not c["ok"]]
        claw = forbidden_location(cfg.worktree_root, cfg.home)
        if claw:
            failed.append(claw)
        checks.append(check("worktree_root_valid", not failed, "; ".join(failed) or res["data"]["root"]))
    else:
        checks.append(check("worktree_root_valid", False, "no repository to validate against"))
    if ctx.rt.policy_path():
        try:
            ctx.rt.load_policy("doctor")
            checks.append(check("policy_valid", True, ctx.rt.policy_path()))
        except CommandFailed as exc:
            detail = "; ".join(c["detail"] for c in exc.envelope["checks"] if not c["ok"])
            checks.append(check("policy_valid", False, detail))
    if cfg.certifications:
        try:
            certified, excluded = ctx.rt.certified("doctor")
            checks.append(check("certifications_loaded", True, f"certified: {', '.join(sorted(certified)) or 'none'}"
                                + (f"; excluded {len(excluded)}" if excluded else "")))
        except CommandFailed as exc:
            checks.append(check("certifications_loaded", False,
                                "; ".join(c["detail"] for c in exc.envelope["checks"] if not c["ok"])))
    data = {"config": dict(cfg.public(), policy_file=ctx.rt.policy_path()), "python": ".".join(map(str, py)),
            "git": text}
    env = envelope("doctor", ok=all(c["ok"] for c in checks), checks=checks, data=data)
    env["_summary"] = [f"[{'pass' if c['ok'] else 'FAIL'}] {c['name']}: {c['detail']}" for c in checks]
    return env


def cmd_review_plan(ctx, parser, make_ctx):
    a = ctx.args
    inner = list(a.inner or [])
    if inner and inner[0] == "--":
        inner = inner[1:]
    if not inner:
        raise UsageError("review-plan needs a subcommand: review-plan [--approve ...] -- <subcommand> [args]")
    inner_args = parser.parse_args(inner)
    op = operation_of(inner_args)
    if op not in GATED:
        raise UsageError(f"review-plan takes a mutating subcommand, not {op}")
    if a.approve and not (a.operator and a.session):
        raise UsageError("review-plan --approve requires --operator and --session (identity is never inferred)")
    inner_args.yes, inner_args.review_id, inner_args.dry_run = False, None, True
    inner_ctx = make_ctx(inner_args)
    _validate_gated_args(inner_args, inner_ctx.cwd)
    plan = build_plan(inner_ctx, GATED[op])
    digest = plan_digest(plan.doc)
    data = {"operation": op, "plan": plan.doc, "plan_sha256": digest, "destructive": plan.destructive}
    summary = [f"plan for {op}: sha256 {digest}" + (" (destructive)" if plan.destructive else "")]
    if not a.approve:
        env = envelope("review-plan", checks=[check("plan_built", True)] + plan.checks, data=data)
        env["_summary"] = summary
        return env
    if plan.destructive:
        return envelope("review-plan", ok=False, checks=[check("plan_not_destructive", False, (
            "destructive plans are never approved by review records; they need an interactive confirmation or an "
            "approval admitted through the ApprovalBroker"))], data=data)
    store = ReviewStore(inner_ctx.cfg.reviews_dir)
    review_id = a.review_id or f"rev-{uuid.uuid4().hex}"
    if store.exists(review_id):
        return envelope("review-plan", ok=False, checks=[check("review_id_unique", False, review_id)], data=data)
    now = ctx.clock()
    record = store.write(review_id=review_id, operation=op, plan_sha256=digest, operator=a.operator,
                         session=a.session, now=now, ttl=inner_ctx.cfg.review_ttl, plan_redacted=output.redact(plan.doc))
    data.update(review_id=review_id, review_record=store.path(review_id), approved_by=a.operator,
                session=a.session, approved_at=record["approved_at"], expires_at=record["expires_at"])
    env = envelope("review-plan", changed=True, checks=[check("plan_built", True), check("plan_not_destructive", True),
                                                        check("review_record_written", True, review_id)], data=data)
    env["_summary"] = summary + [f"review record {review_id} by {a.operator} (session {a.session}) expires "
                                 f"{record['expires_at']}; run the command with --yes --review-id {review_id}"]
    return env


# == entry point ==
def _emit(env, stream, json_mode, verbose):
    if json_mode:
        output.write_json(stream, output.public(env))
    elif env.get("_raw_lines") and env.get("ok"):  # events tail: one line per event
        for line in env.get("_summary") or []:
            stream.write(output.redact_line(line) + "\n")
        stream.flush()
    else:
        output.render_human(env, stream, verbose=verbose, summary_lines=env.get("_summary") or ())


def main(argv=None, *, stdin=None, stdout=None, stderr=None, env=None, is_tty=None, clock=None, cwd=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    env = dict(os.environ) if env is None else dict(env)
    clock = clock or time.time
    cwd = cwd or os.getcwd()
    if is_tty is None:
        def is_tty():
            return stdin.isatty() and stderr.isatty()
    json_mode = "--json" in argv
    verbose = "--verbose" in argv
    parser = build_parser()
    _Parser.out = stdout

    def usage(exc):
        if json_mode:
            output.write_json(stdout, output.public(output.usage_envelope(str(exc), getattr(exc, "check_name",
                                                                                            "usage"))))
        else:
            stderr.write(output.redact_line(str(exc)) + "\n")
            stderr.flush()
        return output.EXIT_USAGE

    def make_ctx(args):
        cfg = load_config(args, env, cwd)
        return Ctx(args, cfg, stdin=stdin, stdout=stdout, stderr=stderr, env=env, is_tty=is_tty, clock=clock,
                   cwd=cwd)

    try:
        args = parser.parse_args(argv)
    except UsageError as exc:
        return usage(exc)
    except SystemExit as exc:  # --help and --version
        return exc.code if isinstance(exc.code, int) else 0
    finally:
        _Parser.out = None
    op = operation_of(args)
    try:
        if op == "review-plan":
            ctx = Ctx(args, None, stdin=stdin, stdout=stdout, stderr=stderr, env=env, is_tty=is_tty, clock=clock,
                      cwd=cwd)
            _Parser.out = stdout
            try:
                result = cmd_review_plan(ctx, parser, make_ctx)
            finally:
                _Parser.out = None
        elif op == "doctor":
            result = cmd_doctor(_doctor_ctx(args, env, cwd, stdin, stdout, stderr, is_tty, clock))
        else:
            ctx = make_ctx(args)
            if op in GATED:
                _validate_gated_args(args, cwd)
                result = run_gated(ctx, op)
            elif op == "status":
                result = cmd_status(ctx)
            elif op == "approvals.list":
                result = cmd_approvals_list(ctx)
            elif op == "events.tail":
                result = cmd_events_tail(ctx)
            else:  # pragma: no cover - argparse restricts the choices
                raise UsageError(f"unknown subcommand {op}")
    except UsageError as exc:
        return usage(exc)
    except CommandFailed as exc:
        result = exc.envelope
        result["operation"] = op
    except KeyboardInterrupt:
        result = envelope(op, ok=False, checks=[check("interrupted", False, "interrupted by the operator")])
    except BrokenPipeError:
        _silence_stdout(stdout)
        return output.EXIT_OK
    except Exception as exc:  # never a traceback with unredacted data
        result = envelope(op, ok=False, checks=[check("unexpected_error", False, f"{type(exc).__name__}: {exc}")])
    if result is None:  # streamed output (events tail --follow)
        return output.EXIT_OK
    try:
        _emit(result, stdout, json_mode, verbose)
    except BrokenPipeError:  # reader went away (for example `| head`)
        _silence_stdout(stdout)
    return output.exit_code(result)


def _silence_stdout(stdout):
    """Point a closed pipe at /dev/null so interpreter shutdown stays quiet."""
    try:
        fd = stdout.fileno()
    except (AttributeError, OSError, ValueError):
        return
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, fd)
    finally:
        os.close(devnull)


def _doctor_ctx(args, env, cwd, stdin, stdout, stderr, is_tty, clock):
    """Doctor reports bad locations as failed checks instead of refusing."""
    cfg = load_config(args, env, cwd, validate_locations=False)
    return Ctx(args, cfg, stdin=stdin, stdout=stdout, stderr=stderr, env=env, is_tty=is_tty, clock=clock, cwd=cwd)
