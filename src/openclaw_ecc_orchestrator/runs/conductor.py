"""Conductor facade: wires run state, worktrees, runners, gates, review,
approvals and the merge queue into one lifecycle per unit.

The conductor holds no authority of its own beyond the run lease; every
state change goes through the RunManager, so a replacement conductor can
resume from the store after a restart.

Lifecycle of one unit:

1. `assign_unit` classifies the plan unit, asks the escalation controller
   for the attempt tier, selects a certified runner for it, and records the
   routing decision with the assignment.
2. `start_unit` snapshots shared git state, creates the worktree and starts
   the runner with `ECC_HANDOFF_PATH` (where the runner writes its handoff)
   and `ECC_UNIT_ID` in its environment.
3. `wait_unit` renews the lease while the runner works, then: marks the
   process finished (and emits `unit.completed`), compares shared git state,
   records usage, validates the handoff (`schemas.validate_handoff`),
   verifies (gates, actual diff against scope, re-classification) and
   records the escalation outcome.

Runners and gates have no wall clock limit. While they run, the supervisor
checks their health every `health.interval_seconds` (`process.health`); only
`health.flat_checks_for_hang` consecutive checks without any progress signal
cancel the process group (reason `hung`). The first flat check emits
`attention.required` (`suspected_hang`), a confirmed hang emits
`attention.required` (`hung_cancelled`), and `unit.health` is emitted on each
health state change. `budget.minutes` is advisory: a runner that passes it
gets `attention.required` (`long_running`) once per multiple of the budget
and is never cancelled for it.
4. `record_review` accepts only a review that names the verified head and
   meets the review tier and independence for the actual diff, then
   requests a merge approval bound to that head.
5. `enqueue_merge` hands the broker request id to the merge queue, which
   consumes the approval through the broker.
"""

import copy
import json
import os
import threading
import time

from .. import schemas
from ..gates.runner import run_gates
from ..merge_queue.conflicts import diff_stats, out_of_scope_changes
from ..merge_queue.dispatch import Dispatcher
from ..merge_queue.queue import approval_request_id
from ..plugin.summary import summarize_required_user_actions
from ..process import health as H
from ..routing.classify import classify
from ..routing.escalation import AttemptOutcome, EscalationController, owner_grant, replay
from ..routing.selection import review_satisfies, select_reviewer, select_runner
from ..runners.registry import model_family
from ..worktrees.git import git_out, rev_exists
from . import repair as R
from . import state as S
from .envelope import SCHEMA_VERSION, check, envelope
from .store import StoreError, iso

try:  # provided by the worktrees package; optional until it lands
    from ..worktrees.guard import diff_shared_git_state, snapshot_shared_git_state
    DEFAULT_GIT_STATE_GUARD = (snapshot_shared_git_state, diff_shared_git_state)
except ImportError:  # pragma: no cover - depends on the installed tree
    DEFAULT_GIT_STATE_GUARD = None

MAX_PROGRESS_EVENTS = 200
MAX_HANDOFF_BYTES = 256 * 1024
HANDOFF_ENV = "ECC_HANDOFF_PATH"
# Placeholders a runner command may carry; start_unit replaces them with the
# attempt's handoff file and its folder. CLI runners cannot always read their
# environment (Claude print mode denies printenv) or write outside the
# worktree, so the literal path goes in the prompt and the folder is granted
# with --add-dir (live finding, run rmcp-m0d, 2026-10-19).
HANDOFF_PATH_TOKEN = "{ECC_HANDOFF_PATH}"
HANDOFF_DIR_TOKEN = "{ECC_HANDOFF_DIR}"


def fill_handoff_tokens(argv, handoff_path):
    """Return argv with the handoff placeholders replaced (no-op without them)."""
    folder = os.path.dirname(handoff_path)
    return [str(a).replace(HANDOFF_PATH_TOKEN, handoff_path).replace(HANDOFF_DIR_TOKEN, folder)
            for a in argv]
UNIT_ENV = "ECC_UNIT_ID"
_UNSET = object()
# Failures that leave durable state untouched and are worth retrying.
TRANSIENT_ERRORS = (OSError, StoreError, TimeoutError)


class Conductor:
    def __init__(self, conductor_id, *, manager, worktrees, supervisor, queue, broker=None, emitter=None,
                 repo_checks=None, target_branch="main", log_root, runner_timeout=None, gate_timeout=None,
                 runner_env_allow=(), protected_globs=(), clock=time.time, policy=None, certified_runners=None,
                 catalog=None, breaker=None, tier_costs=None, lease_renew_interval=10.0,
                 git_state_guard=_UNSET, health_override=None, auto_retry=False, handoff_repair_turns=0,
                 repair_timeout=900.0):
        """`runner_timeout` and `gate_timeout` are optional hard wall clock
        ceilings in seconds, None (off) by default; setting them is
        discouraged because health monitoring already stops hung processes.
        `health_override` (a `process.health.HealthSettings`) replaces the
        policy and unit health settings; tests use it for short intervals.
        `auto_retry` returns a failed unit to `ready` when its escalation
        controller allows another attempt (TEAM-DELIVERY-v2 5.1);
        `handoff_repair_turns` allows that many handoff-only repair turns per
        attempt (5.2), each limited to `repair_timeout` seconds."""
        self.id = conductor_id
        self.manager = manager
        self.worktrees = worktrees
        self.supervisor = supervisor
        self.queue = queue
        self.broker = broker
        self.emitter = emitter
        self.repo_checks = repo_checks
        self.target = target_branch
        self.log_root = os.path.abspath(log_root)
        self.runner_timeout = runner_timeout
        self.gate_timeout = gate_timeout
        self.runner_env_allow = tuple(runner_env_allow)
        self.protected_globs = tuple(protected_globs)
        self.clock = clock
        self.policy = policy
        self.certified_runners = certified_runners
        self.catalog = catalog
        self.breaker = breaker
        self.tier_costs = dict(tier_costs or {})
        self.lease_renew_interval = float(lease_renew_interval)
        # (snapshot(repo) -> dict, diff(before, after, allow_refs=()) -> list[str]) or None
        self.git_state_guard = DEFAULT_GIT_STATE_GUARD if git_state_guard is _UNSET else git_state_guard
        self.health_override = health_override
        self.auto_retry = bool(auto_retry)
        self.handoff_repair_turns = int(handoff_repair_turns)
        self.repair_timeout = float(repair_timeout)
        self.dispatcher = Dispatcher(manager)
        self._handles = {}
        self._cancel_terminal = {}
        self._handoff_paths = {}
        self._log_paths = {}
        self._repair_argv = {}
        self._repairs_used = {}
        self._git_before = {}
        self._stages = {}
        self._controllers = {}
        self._lease_due = {}
        self._lock = threading.Lock()

    # == run level ==
    def create_run(self, plan, run_id=None, dry_run=False):
        repo = self.worktrees.repo
        if not rev_exists(repo, f"refs/heads/{self.target}"):
            return envelope("run.create", ok=False, checks=[check("target_branch_exists", False, self.target)])
        base = git_out(["rev-parse", f"refs/heads/{self.target}"], repo)
        return self.manager.create_run(plan, self.id, run_id=run_id, dry_run=dry_run, policy=self.policy,
                                       metadata={"base_commit": base, "target_branch": self.target})

    def resume(self, run_id, dry_run=False):
        return self.manager.resume(run_id, self.id, dry_run=dry_run)

    def abandon(self):
        """Forget live handles without touching durable state (simulated crash
        or clean conductor shutdown before a handoff)."""
        with self._lock:
            self._handles.clear()
            self._cancel_terminal.clear()
            self._handoff_paths.clear()
            self._git_before.clear()
            self._stages.clear()
            self._controllers.clear()
            self._lease_due.clear()

    def required_user_actions(self, run_id):
        run = self.manager.load(run_id)
        pending = self.broker.pending(run_id) if self.broker else []
        return summarize_required_user_actions(run, pending, queue_state=self.queue.state(), now=self.clock())

    def _attention(self, run_id, unit_id, reason, **data):
        return self._emit("attention.required", run_id, unit_id, dict(data, reason=reason))

    def _emit(self, event_type, run_id, unit_id, data):
        if self.emitter is None:
            return []
        try:
            self.emitter.emit(event_type, run_id, unit_id=unit_id, data=data)
        except Exception as exc:  # emission must never break the lifecycle
            return [f"event emission failed: {exc}"]
        return []

    # == health ==
    def health_settings(self, unit=None, policy=_UNSET):
        """Health settings for a unit: repository policy, then the unit's own
        `health` block, then the conductor's `health_override`."""
        policy = self.policy if policy is _UNSET else policy
        try:
            return H.resolve_health_settings(policy, unit, self.health_override)
        except H.HealthSettingsError:  # validated at create_run; never fail open or closed here
            return H.HealthSettings()

    def _health_listener(self, run_id, unit_id, attempt, settings, process="runner", gate=None, budget=None):
        """Callback for every health check of one process: emits
        `unit.health` on state changes, `attention.required` for
        `suspected_hang`, `hung_cancelled` and (runners) `long_running`."""
        notified = {"long_running": 0}

        def on_check(result):
            base = {"process": process, "attempt": attempt}
            if gate is not None:
                base["gate"] = gate
            if result.changed:
                self._emit("unit.health", run_id, unit_id, dict(
                    base, state=result.state, previous=result.previous_state, flat_checks=result.flat_checks,
                    flat_checks_for_hang=settings.flat_checks_for_hang,
                    interval_seconds=settings.interval_seconds, evidence=result.evidence))
                if process == "runner":
                    try:
                        self.manager.annotate_unit(run_id, self.id, unit_id, "health", {
                            "state": result.state, "attempt": attempt, "flat_checks": result.flat_checks,
                            "interval_seconds": settings.interval_seconds,
                            "flat_checks_for_hang": settings.flat_checks_for_hang,
                            "checked_at": result.evidence.get("checked_at")})
                    except Exception:  # noqa: BLE001 - status only; the lease may be elsewhere
                        pass
                if result.state == H.SUSPECTED_HANG:
                    self._attention(run_id, unit_id, "suspected_hang", detail=dict(base, evidence=result.evidence))
                elif result.state == H.HUNG:
                    self._attention(run_id, unit_id, "hung_cancelled", detail=dict(base, evidence=result.evidence))
            if budget and budget.get("seconds"):
                total = budget["used_seconds"] + float(result.elapsed_seconds or 0.0)
                multiple = int(total // budget["seconds"])
                if multiple >= 1 and multiple > notified["long_running"]:
                    notified["long_running"] = multiple
                    self._attention(run_id, unit_id, "long_running", detail=dict(
                        base, elapsed_minutes=round(total / 60.0, 2), budget_minutes=budget["minutes"],
                        multiple=multiple, note="budget.minutes is advisory; the runner keeps running"))
        return on_check

    def _minutes_budget(self, unit, plan_unit):
        """Advisory minutes budget of a unit (per unit, all attempts), tightened
        by the policy's `max_minutes_per_unit` when present."""
        budget = (plan_unit or {}).get("budget") or unit.get("budget") or {}
        minutes = budget.get("minutes", budget.get("max_minutes"))
        ceiling = ((self.policy or {}).get("budget") or {}).get("max_minutes_per_unit")
        values = [float(v) for v in (minutes, ceiling)
                  if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0]
        if not values:
            return None
        limit = min(values)
        return {"minutes": limit, "seconds": limit * 60.0,
                "used_seconds": float(unit.get("minutes_used") or 0.0) * 60.0}

    # == gates ==
    def gate(self, unit, worktree, run_id=None):
        """Run the unit's gates in `worktree`, health monitored. With
        `run_id`, health changes of a gate command are emitted as events."""
        settings = self.health_settings(unit)
        on_health = None
        if run_id is not None and unit.get("id"):
            listeners = {}

            def on_health(name, result):
                if name not in listeners:
                    listeners[name] = self._health_listener(run_id, unit["id"], None, settings, process="gate",
                                                            gate=name)
                listeners[name](result)
        return run_gates(unit, worktree, repo_checks=self.repo_checks, supervisor=self.supervisor,
                         protected_globs=self.protected_globs, timeout=self.gate_timeout,
                         log_dir=os.path.join(self.log_root, "gates"), clock=self.clock,
                         health=settings, on_health=on_health)

    # == routing ==
    def _classify(self, run, unit_id, stats=None, previous_attempt_failed=False):
        return classify(self.manager.plan_unit(run, unit_id), self.policy, stats,
                        previous_attempt_failed=previous_attempt_failed)

    def _controller(self, run, unit_id):
        """The unit's escalation controller, rebuilt from the persisted record
        (by replaying its attempts) after a conductor restart."""
        key = (run["run_id"], unit_id)
        with self._lock:
            ctrl = self._controllers.get(key)
        if ctrl is not None:
            return ctrl
        classification = self._classify(run, unit_id)
        if not classification.ok:
            return None
        ctrl = EscalationController(self.manager.plan_unit(run, unit_id), classification,
                                    tier_costs=self.tier_costs, clock=self.clock, policy=self.policy)
        record = (run["units"][unit_id].get("annotations") or {}).get("escalation") or {}
        replay(ctrl, record.get("attempts"), owner_grant(run["units"][unit_id]))
        with self._lock:
            self._controllers[key] = ctrl
        return ctrl

    def _needs_user(self, run_id, unit_id, reason, detail, op):
        self.manager.transition(run_id, self.id, unit_id, S.NEEDS_USER, reason=reason)
        return envelope(op, ok=False, changed=True, checks=[check(reason, False, detail)],
                        required_user_actions=[{"kind": "resolve_needs_user", "run_id": run_id, "unit_id": unit_id,
                                                "detail": detail}])

    def assign_unit(self, run_id, unit_id, worker_id=None, *, large_context=False):
        """Route and assign a ready unit: classify, take the tier from the
        escalation controller, select the cheapest eligible certified runner,
        and record the routing decision with the assignment. `worker_id`
        defaults to the selected runner name."""
        op = "unit.assign"
        run = self.manager.load(run_id)
        unit = run["units"].get(unit_id)
        if unit is None or unit["state"] != S.READY:
            return envelope(op, ok=False, checks=[check("unit_ready", False, unit["state"] if unit else "unknown")])
        ctrl = self._controller(run, unit_id)
        if ctrl is None:
            errors = "; ".join(self._classify(run, unit_id).errors)
            return self._needs_user(run_id, unit_id, "classification_invalid", errors, op)
        action = ctrl.next_action()
        if action["action"] in ("stop", "done"):
            return self._needs_user(run_id, unit_id, "escalation_stopped", action["reason"], op)
        classification = self._classify(run, unit_id)
        tier = action["tier"]
        selection = select_runner(tier, self.certified_runners or [], self.catalog, self.policy, role="author",
                                  risk=classification.risk, breaker=self.breaker, now=self.clock(),
                                  large_context=large_context)
        if not selection.ok:
            return self._needs_user(run_id, unit_id, "no_eligible_runner", "; ".join(selection.reasons), op)
        routing = {"tier": tier, "runner": selection.runner, "provider": selection.provider,
                   "model": selection.model, "family": selection.family,
                   "estimated_cost_usd": selection.estimated_cost_usd, "escalation": action}
        res = self.dispatcher.dispatch_unit(run_id, self.id, unit_id, worker_id or selection.runner,
                                            routing=routing)
        res["operation"] = op
        return res

    def _record_outcome(self, run_id, unit_id, passed, objective, reason, cost_usd=0.0):
        """Feed one finished attempt to the escalation controller, persist
        its record, and stop the unit (needs_user) when the controller says
        no further attempt is allowed."""
        run = self.manager.load(run_id)
        ctrl = self._controller(run, unit_id)
        if ctrl is None or ctrl.finished:
            return None
        routing = run["units"][unit_id].get("routing") or {}
        nxt = ctrl.record(AttemptOutcome(passed=passed, objective_failure=objective, cost_usd=max(0.0, cost_usd),
                                         runner=routing.get("runner"), provider=routing.get("provider"),
                                         model=routing.get("model"), reason=reason))
        self.manager.annotate_unit(run_id, self.id, unit_id, "escalation", ctrl.to_record())
        if nxt["action"] == "stop":
            state = self.manager.load(run_id)["units"][unit_id]["state"]
            if S.NEEDS_USER in S.TRANSITIONS.get(state, ()):
                self.manager.transition(run_id, self.id, unit_id, S.NEEDS_USER, reason="escalation_stopped")
        elif not passed and self.auto_retry and nxt["action"] not in ("done",):
            state = self.manager.load(run_id)["units"][unit_id]["state"]
            if state == S.FAILED:
                self.manager.transition(run_id, self.id, unit_id, S.READY, reason=f"retry:{reason}")
                self._emit("unit.retry_scheduled", run_id, unit_id,
                           {"previous_reason": reason, "next_action": nxt.get("action"), "tier": nxt.get("tier")})
        return nxt

    # == repair support (TEAM-DELIVERY-v2 section 5) ==
    def return_for_changes(self, run_id, unit_id, reason, evidence):
        """A review, verification or merge found the work wanting (TEAM-DELIVERY-v2
        4.1 and 5.1): the attempt that passed the gates is re-recorded as an
        objective failure, so the escalation controller decides the next step
        (repair, escalate or stop) within the unit's budget, and the unit goes
        back to `ready` (reason `review:<reason>`) with the evidence for the
        failure brief. The worktree and branch stay."""
        op = "unit.return_for_changes"
        run = self.manager.load(run_id)
        unit = run["units"][unit_id]
        record = copy.deepcopy((unit.get("annotations") or {}).get("escalation") or {})
        attempts = record.get("attempts") or []
        if attempts and attempts[-1].get("passed"):
            attempts[-1].update(passed=False, objective_failure=True, reason=reason)
            record["final"] = None
            self.manager.annotate_unit(run_id, self.id, unit_id, "escalation", record)
        with self._lock:
            self._controllers.pop((run_id, unit_id), None)
        self._note_failure(run_id, unit_id, reason, evidence)
        ctrl = self._controller(self.manager.load(run_id), unit_id)
        nxt = ctrl.next_action() if ctrl is not None else {"action": "stop", "reason": "no controller"}
        if nxt["action"] in ("stop", "done"):
            return self._needs_user(run_id, unit_id, "escalation_stopped",
                                    f"{reason}; escalation: {nxt.get('reason')}", op)
        res = self.manager.transition(run_id, self.id, unit_id, S.READY, reason=f"review:{reason}")
        if not res["ok"]:
            return res
        self._emit("unit.returned_for_changes", run_id, unit_id,
                   {"reason": reason, "next_action": nxt.get("action"), "tier": nxt.get("tier")})
        return envelope(op, ok=True, changed=True, checks=[check("returned_to_coder", True, reason)],
                        data={"next_action": nxt})

    def _note_failure(self, run_id, unit_id, reason, evidence, fix=None):
        """Record what the next attempt needs to know (`last_failure`)."""
        try:
            unit = self.manager.load(run_id)["units"][unit_id]
            self.manager.annotate_unit(run_id, self.id, unit_id, "last_failure", {
                "reason": reason, "attempt": unit.get("attempts"), "evidence": evidence, "fix": fix,
                "at": iso(self.clock())})
        except TRANSIENT_ERRORS:
            pass

    def _stop_runner(self, run_id, unit_id, unit, stop, log_text, data):
        """Section 5.3: a failure retrying cannot fix. The unit goes to
        `needs_user` with the exact fix and the escalation record is not
        charged."""
        op = "unit.complete"
        kind, detail = stop
        runner = (unit.get("routing") or {}).get("runner")
        fix = R.stop_fix(kind, runner)
        self.manager.transition(run_id, self.id, unit_id, S.NEEDS_USER, reason=kind)
        self._note_failure(run_id, unit_id, kind, R.failure_evidence(kind, log_text=log_text), fix=fix)
        warnings = self._attention(run_id, unit_id, kind, detail=detail, fix=fix)
        return envelope(op, ok=False, changed=True, checks=[check("runner_ready", False, f"{kind}: {detail}")],
                        warnings=warnings, data=data,
                        required_user_actions=[{"kind": "resolve_needs_user", "run_id": run_id, "unit_id": unit_id,
                                                "detail": fix}])

    def _handoff_problems(self, run, unit_id, doc, missing):
        """(problems, repairable). Only a missing, unreadable or malformed
        handoff is repairable by a handoff-only turn. Semantic violations
        (claimed success with failing commands, files outside the scope) are
        about the work itself and never go to a repair turn, which could
        otherwise paper over them; a valid handoff that honestly reports
        failed, partial or blocked work is not a handoff problem at all."""
        if missing:
            return [missing], missing != "handoff_too_large"
        report = schemas.validate_handoff(doc, self.manager.plan_unit(run, unit_id))
        violations = [v.get("kind", "violation") for v in report.violations]
        return list(report.errors) + violations, not violations

    def _can_repair_handoff(self, key):
        with self._lock:
            return bool(self._repair_argv.get(key)) and self._repairs_used.get(key, 0) < self.handoff_repair_turns

    def _repair_handoff(self, run_id, unit_id, workspace, problems, data):
        """Section 5.2: one short turn that rewrites only the handoff. Not an
        attempt: the escalation record is not charged. Returns the re-read
        (doc, missing)."""
        key = (run_id, unit_id)
        with self._lock:
            self._repairs_used[key] = self._repairs_used.get(key, 0) + 1
            template = list(self._repair_argv.get(key) or [])
            handoff_path = self._handoff_paths.get(key)
            log_path = self._log_paths.get(key)
        prompt = R.handoff_repair_prompt(unit_id, HANDOFF_PATH_TOKEN, problems, R.HANDOFF_TEMPLATE, R.HANDOFF_RULES)
        argv = fill_handoff_tokens([prompt if a == R.REPAIR_PROMPT_TOKEN else a for a in template], handoff_path)
        self._emit("unit.handoff_repair", run_id, unit_id, {"problems": problems[:10]})
        record = {"problems": problems[:20], "at": iso(self.clock())}
        try:
            handle = self.supervisor.start(argv, cwd=workspace["path"], env_allow=self.runner_env_allow,
                                           extra_env={HANDOFF_ENV: handoff_path, UNIT_ENV: unit_id},
                                           log_path=(log_path[:-4] + "-handoff-repair.log") if log_path else None,
                                           timeout=self.repair_timeout)
            if handle.pid is None:
                result = handle.wait()
            else:
                while True:
                    try:
                        result = handle.wait(self.lease_renew_interval)
                        break
                    except TimeoutError:
                        self._keep_lease(run_id)
            record.update(exit_code=result.get("exit_code"), duration_s=result.get("duration_s"),
                          error=result.get("error"))
        except (OSError, TypeError, ValueError) as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
        doc, missing = self._read_handoff(key, None)
        record["handoff_found"] = missing is None
        data["handoff_repair"] = record
        self.manager.annotate_unit(run_id, self.id, unit_id, "handoff_repair", record)
        return doc, missing

    # == unit execution ==
    def start_unit(self, run_id, unit_id, runner_argv, extra_env=None, on_line=None, repair_argv=None):
        op = "unit.start"
        run = self.manager.load(run_id)
        unit = run["units"].get(unit_id)
        if unit is None or unit["state"] != S.ASSIGNED:
            return envelope(op, ok=False, checks=[check("unit_assigned", False, unit["state"] if unit else "unknown")])
        paths = self.worktrees.paths(run_id, unit_id)
        if not os.path.isdir(paths["path"]):
            base = (run.get("metadata") or {}).get("base_commit") or self.target
            created = self.worktrees.create(run_id, unit_id, base_ref=base, owner=unit["owner"])
            if not created["ok"]:
                return created
            workspace = {"path": created["data"]["path"], "branch": created["data"]["branch"],
                         "base_commit": created["data"]["base_commit"]}
            res = self.manager.record_workspace(run_id, self.id, unit_id, workspace)
            if not res["ok"]:
                return res
        before = None
        if self.git_state_guard is not None:
            try:
                before = self.git_state_guard[0](self.worktrees.repo)
            except Exception as exc:  # fail closed: never run a runner we cannot audit
                return envelope(op, ok=False, checks=[check("shared_git_state_snapshot", False, str(exc))])
        res = self.manager.transition(run_id, self.id, unit_id, S.RUNNING, reason=f"attempt {unit['attempts']}")
        if not res["ok"]:
            return res
        count = [0]

        def progress(stream, line):
            if on_line:
                on_line(stream, line)
            if self.emitter is not None and count[0] < MAX_PROGRESS_EVENTS:
                count[0] += 1
                try:
                    self.emitter.emit("unit.progress", run_id, unit_id=unit_id,
                                      data={"message": line, "stream": stream, "attempt": unit["attempts"]})
                except Exception:
                    pass

        log_path = os.path.join(self.log_root, run_id, f"{unit_id}-attempt{unit['attempts']}.log")
        status_path = log_path[:-4] + ".status.json"
        handoff_path = log_path[:-4] + ".handoff.json"
        os.makedirs(os.path.dirname(handoff_path), exist_ok=True)
        if os.path.lexists(handoff_path):
            os.unlink(handoff_path)
        env = dict(extra_env or {})
        env.update({HANDOFF_ENV: handoff_path, UNIT_ENV: unit_id})
        plan_unit = self.manager.plan_unit(run, unit_id)
        settings = self.health_settings(plan_unit, run.get("policy") or self.policy)
        health = H.HealthConfig(settings=settings, worktree=paths["path"], on_check=self._health_listener(
            run_id, unit_id, unit["attempts"], settings, budget=self._minutes_budget(unit, plan_unit)))
        runner_argv = fill_handoff_tokens(runner_argv, handoff_path)
        handle = self.supervisor.start(list(runner_argv), cwd=paths["path"], env_allow=self.runner_env_allow,
                                       extra_env=env, on_line=progress, log_path=log_path,
                                       status_path=status_path, timeout=self.runner_timeout, health=health)
        if handle.pid is None:
            result = handle.wait()
            self.manager.transition(run_id, self.id, unit_id, S.FAILED, reason="spawn_failed")
            return envelope(op, ok=False, checks=[check("runner_started", False, result["error"])])
        rec = self.manager.record_process(run_id, self.id, unit_id,
                                          {"pid": handle.pid, "log_path": log_path, "conductor_id": self.id})
        key = (run_id, unit_id)
        with self._lock:
            self._handles[key] = handle
            self._handoff_paths[key] = handoff_path
            self._log_paths[key] = log_path
            self._repair_argv[key] = list(repair_argv) if repair_argv else None
            self._repairs_used[key] = 0
            self._git_before[key] = before
            self._stages[key] = {}
        return envelope(op, changed=True, checks=[check("runner_started", True)] + rec["checks"],
                        data={"pid": handle.pid, "log_path": log_path, "worktree": paths["path"],
                              "handoff_path": handoff_path, "health": settings.to_dict()})

    def cancel_unit(self, run_id, unit_id, reason="operator", terminal=True):
        """Cooperative cancel of the unit's runner; the unit ends cancelled
        (terminal) or failed (retryable)."""
        with self._lock:
            handle = self._handles.get((run_id, unit_id))
            self._cancel_terminal[(run_id, unit_id)] = (terminal, reason)
        if handle is None:
            target = S.CANCELLED if terminal else S.FAILED
            return self.manager.transition(run_id, self.id, unit_id, target, reason=f"cancelled:{reason}")
        handle.cancel(reason)
        return self.wait_unit(run_id, unit_id)

    # == lease keeping ==
    def _keep_lease(self, run_id, force=False):
        """Renew the run lease when due; re-acquire it when it merely expired.
        Returns (held, detail)."""
        now = self.clock()
        if not force and now < self._lease_due.get(run_id, 0.0):
            return True, ""
        res = self.manager.renew_lease(run_id, self.id)
        if not res["ok"]:
            # Expired but unclaimed: taking it back is safe, and completion
            # re-checks that the unit is still ours before touching it.
            res = self.manager.acquire_lease(run_id, self.id)
        if not res["ok"]:
            return False, "; ".join(c["detail"] for c in res["checks"] if not c["ok"])
        lease = (res.get("data") or {}).get("lease") or {}
        expires = float(lease.get("expires_at", now))
        self._lease_due[run_id] = now + max(0.0, (expires - now) / 2.0)
        return True, ""

    def _forget(self, key):
        with self._lock:
            self._handles.pop(key, None)
            self._cancel_terminal.pop(key, None)
            self._handoff_paths.pop(key, None)
            self._git_before.pop(key, None)
            self._stages.pop(key, None)

    def _surrender(self, run_id, unit_id, detail, data):
        op = "unit.complete"
        res = self.manager.surrender_unit(run_id, self.id, unit_id, reason="lease_lost", detail=detail)
        if res["ok"]:
            self._forget((run_id, unit_id))
        return envelope(op, ok=False, changed=res["changed"], checks=[check("lease_held", False, detail)] + res["checks"],
                        required_user_actions=res["required_user_actions"], data=data)

    def wait_unit(self, run_id, unit_id, timeout=None, handoff=None):
        """Wait for the unit's runner, renewing the lease every
        `lease_renew_interval` seconds (and whenever it is half spent on the
        injected clock). On a timeout or a transient failure the process
        handle is kept, so the call can simply be retried. If another
        conductor took the lease, the unit is surrendered to `needs_user`
        with its pid recorded. `handoff` overrides the handoff file."""
        op = "unit.complete"
        key = (run_id, unit_id)
        with self._lock:
            handle = self._handles.get(key)
        if handle is None:
            return envelope(op, ok=False, checks=[check("unit_has_process", False)])
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        while True:
            held, detail = self._keep_lease(run_id)
            if not held:
                return self._surrender(run_id, unit_id, detail, {"pid": handle.pid})
            step = self.lease_renew_interval
            if deadline is not None:
                step = min(step, max(0.0, deadline - time.monotonic()))
            try:
                result = handle.wait(step)
                break
            except TimeoutError:
                if deadline is not None and time.monotonic() >= deadline:
                    return envelope(op, ok=False, checks=[check("runner_finished", False, "still running")],
                                    data={"pid": handle.pid})
        data = {"process": result}
        held, detail = self._keep_lease(run_id, force=True)
        if not held:
            return self._surrender(run_id, unit_id, detail, data)
        try:
            res, finished = self._complete(run_id, unit_id, handle, result, handoff, data)
        except TRANSIENT_ERRORS as exc:
            return envelope(op, ok=False, checks=[check("transient_failure", False, f"{type(exc).__name__}: {exc}")],
                            data=data)
        if finished:
            self._forget(key)
        return res

    # == completion ==
    def _allowed_refs(self, run, unit_id):
        """Refs a runner's time window may legitimately see change: its own
        branch, plus the branches of the run's other units (the conductor
        creates them, and their runners commit to them in parallel). Tampering
        with another unit's branch still fails closed downstream: the merge
        queue requires that branch's tip to equal its verified head."""
        own = (run["units"][unit_id].get("workspace") or {}).get("branch") \
            or self.worktrees.paths(run["run_id"], unit_id)["branch"]
        refs = [f"refs/heads/{own}"]
        for uid in sorted(run["units"]):
            if uid != unit_id:
                refs.append(f"refs/heads/{self.worktrees.paths(run['run_id'], uid)['branch']}")
        return refs

    def _note_own_ref_move(self, ref, old, new):
        """The conductor itself moved `ref` (a local merge): advance every
        in-flight unit's baseline for that ref, but only when the baseline
        still shows `old`, so a runner's own move of the ref is still caught."""
        with self._lock:
            for before in self._git_before.values():
                refs = before.get("refs") if isinstance(before, dict) else None
                if isinstance(refs, dict) and refs.get(ref) == old:
                    refs[ref] = new

    def _read_handoff(self, key, handoff):
        if handoff is not None:
            return handoff, None
        path = self._handoff_paths.get(key)
        if not path or not os.path.isfile(path) or os.path.islink(path):
            return None, "handoff_missing"
        if os.path.getsize(path) > MAX_HANDOFF_BYTES:
            return None, "handoff_too_large"
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh), None
        except (ValueError, UnicodeDecodeError):
            return None, "handoff_unreadable"

    def _complete(self, run_id, unit_id, handle, result, handoff, data):
        """Process a finished runner. Returns (envelope, finished); when
        `finished` is False the handle is kept for a retry. Every step is
        recorded in `self._stages` so a retry does not repeat it."""
        op = "unit.complete"
        key = (run_id, unit_id)
        stage = self._stages.setdefault(key, {})
        run = self.manager.load(run_id)
        unit = run["units"][unit_id]
        proc = unit.get("process") or {}
        if unit["state"] != S.RUNNING or proc.get("pid") != handle.pid:
            return envelope(op, ok=False, checks=[check("unit_still_ours", False,
                                                        f"state {unit['state']}, pid {proc.get('pid')}")],
                            data=data), True
        if not stage.get("exit"):
            res = self.manager.record_process_exit(run_id, self.id, unit_id, result)
            if not res["ok"]:
                return envelope(op, ok=False, checks=res["checks"], data=data), False
            stage["exit"] = True
        if not stage.get("completed_event"):
            routing = unit.get("routing") or {}
            self._emit("unit.completed", run_id, unit_id, {
                "exit_code": result.get("exit_code"), "signal": result.get("signal"),
                "duration_seconds": result.get("duration_s"), "attempts": unit.get("attempts"),
                "tier": routing.get("tier"), "runner": routing.get("runner"), "model": routing.get("model"),
                "cancelled": bool(result.get("cancelled")), "cancel_reason": result.get("cancel_reason"),
                "hung": bool(result.get("hung")), "timed_out": bool(result.get("timed_out")),
                "error": bool(result.get("error"))})
            stage["completed_event"] = True
        if not stage.get("guard"):
            before = self._git_before.get(key)
            if self.git_state_guard is not None and before is not None:
                after = self.git_state_guard[0](self.worktrees.repo)
                changes = list(self.git_state_guard[1](before, after,
                                                       allow_refs=self._allowed_refs(run, unit_id)))
                if changes:
                    data["shared_git_state_changes"] = changes
                    res = self.manager.transition(run_id, self.id, unit_id, S.NEEDS_USER,
                                                  reason="shared_git_state_changed")
                    if not res["ok"]:
                        return envelope(op, ok=False, checks=res["checks"], data=data), False
                    self.manager.annotate_unit(run_id, self.id, unit_id, "notes",
                                               {"shared_git_state_changes": changes})
                    return envelope(op, ok=False, changed=True,
                                    checks=[check("shared_git_state_unchanged", False, ", ".join(changes))],
                                    required_user_actions=[{"kind": "resolve_needs_user", "run_id": run_id,
                                                            "unit_id": unit_id,
                                                            "detail": "runner changed shared git state: "
                                                                      + ", ".join(changes)}],
                                    data=data), True
            stage["guard"] = True
        doc, missing = self._read_handoff(key, handoff)
        cost = 0.0
        if isinstance(doc, dict) and isinstance(doc.get("usage"), dict):
            value = doc["usage"].get("cost_usd")
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                cost = float(value)
        cancel = self._cancel_terminal.get(key)
        if result["cancelled"] and cancel is not None:
            terminal, reason = cancel
            target = S.CANCELLED if terminal else S.FAILED
            res = self.manager.transition(run_id, self.id, unit_id, target, reason=f"cancelled:{reason}")
            if not res["ok"]:
                return envelope(op, ok=False, checks=res["checks"], data=data), False
            self.manager.record_usage(run_id, self.id, unit_id, minutes=result["duration_s"] / 60.0, cost_usd=cost)
            return envelope(op, ok=True, changed=True, checks=res["checks"], data=data), True
        if not stage.get("usage"):
            usage = self.manager.record_usage(run_id, self.id, unit_id, minutes=result["duration_s"] / 60.0,
                                              cost_usd=cost)
            stage["usage"] = True
            if not usage["ok"]:
                return envelope(op, ok=False, changed=True, checks=usage["checks"],
                                required_user_actions=usage["required_user_actions"], data=data), True
        if result["error"] or result["timed_out"] or result.get("hung") or result["exit_code"] != 0:
            if result.get("hung"):
                reason = "hung"
            elif result["timed_out"]:
                reason = "runner_timeout"
            else:
                reason = f"runner_exit_{result['exit_code']}"
            log_text = R.read_tail(self._log_paths.get(key))
            stop = None
            if not (result["timed_out"] or result.get("hung") or result["error"]):
                stop = R.classify_runner_failure(log_text, result["exit_code"])
            if stop:
                return self._stop_runner(run_id, unit_id, unit, stop, log_text, data), True
            res = self.manager.transition(run_id, self.id, unit_id, S.FAILED, reason=result["error"] or reason)
            if not res["ok"]:
                return envelope(op, ok=False, checks=res["checks"], data=data), False
            self._note_failure(run_id, unit_id, reason, R.failure_evidence(reason, log_text=log_text))
            self._record_outcome(run_id, unit_id, False, False, reason, cost)
            return envelope(op, ok=False, changed=True, checks=[check("runner_succeeded", False, reason)],
                            data=data), True
        return self._accept_handoff(run_id, unit_id, doc, missing, cost, data)

    def _accept_handoff(self, run_id, unit_id, doc, missing, cost, data):
        op = "unit.complete"
        run = self.manager.load(run_id)
        workspace = run["units"][unit_id]["workspace"]
        problems, repairable = self._handoff_problems(run, unit_id, doc, missing)
        if problems and repairable and self._can_repair_handoff((run_id, unit_id)):
            doc, missing = self._repair_handoff(run_id, unit_id, workspace, problems, data)
        head = git_out(["rev-parse", "HEAD"], workspace["path"])
        if missing:
            self._note_failure(run_id, unit_id, missing, R.failure_evidence(
                missing, handoff_problems=[missing], log_text=R.read_tail(self._log_paths.get((run_id, unit_id)))))
            self.manager.transition(run_id, self.id, unit_id, S.FAILED, reason=missing)
            self._record_outcome(run_id, unit_id, False, False, missing, cost)
            return envelope(op, ok=False, changed=True, checks=[check("handoff_valid", False, missing)],
                            data=data), True
        res = self.manager.record_handoff(run_id, self.id, unit_id, doc, head=head)
        data["handoff"] = (res.get("data") or {}).get("handoff")
        if not res["ok"]:
            summary = data["handoff"] or {}
            if not summary:  # the record itself failed (lease, store): retry later
                return envelope(op, ok=False, checks=res["checks"], data=data), False
            wants_user = summary.get("user_input_required") or summary.get("status") == "blocked"
            if wants_user and not summary.get("violations"):
                self.manager.transition(run_id, self.id, unit_id, S.NEEDS_USER, reason="runner_needs_user")
            else:
                reported = doc.get("unresolved_failures") if isinstance(doc, dict) else None
                self._note_failure(run_id, unit_id, "handoff_invalid", R.failure_evidence(
                    "handoff_invalid", handoff_problems=list(summary.get("problems") or [])
                    + [f"unresolved: {f}" for f in (reported or []) if isinstance(f, str)][:10]))
                self.manager.transition(run_id, self.id, unit_id, S.FAILED, reason="handoff_invalid")
                self._record_outcome(run_id, unit_id, False, False, "handoff_invalid", cost)
            return envelope(op, ok=False, changed=True, checks=res["checks"], data=data), True
        return self._verify(run_id, unit_id, data, cost), True

    def _verify(self, run_id, unit_id, data, cost=0.0):
        op = "unit.complete"
        res = self.manager.transition(run_id, self.id, unit_id, S.VERIFYING)
        if not res["ok"]:
            return res
        run = self.manager.load(run_id)
        unit = run["units"][unit_id]
        workspace = unit["workspace"]
        plan_unit = self.manager.plan_unit(run, unit_id)
        dirty = git_out(["status", "--porcelain", "--untracked-files=all"], workspace["path"])
        if dirty:
            self._note_failure(run_id, unit_id, "uncommitted_changes", R.failure_evidence(
                "uncommitted_changes", files=dirty.splitlines()))
            self.manager.transition(run_id, self.id, unit_id, S.FAILED, reason="uncommitted_changes")
            self._record_outcome(run_id, unit_id, False, False, "uncommitted_changes", cost)
            return envelope(op, ok=False, checks=[check("work_committed", False, dirty.splitlines()[0])], data=data)
        verification = self.gate(plan_unit, workspace["path"], run_id=run_id)
        gates_passed = bool(verification["passed"])
        stats = diff_stats(self.worktrees.repo, workspace["branch"], workspace["base_commit"])
        verification["head"] = git_out(["rev-parse", "HEAD"], workspace["path"])
        verification["changed_files"] = stats["files"]
        verification["diff_stats"] = {k: stats[k] for k in ("files_changed", "lines_added", "lines_removed")}
        verification["handoff_valid"] = True
        verification["out_of_scope"] = out_of_scope_changes(stats["files"], plan_unit["scope"]["files"])
        classification = classify(plan_unit, self.policy, stats)
        verification["classification"] = {
            "ok": classification.ok, "risk": classification.risk, "minimum_tier": classification.minimum_tier,
            "review_tier": classification.review_tier, "errors": list(classification.errors),
            "signals": [s["name"] for s in classification.signals],
        }
        checks = [check("gates_passed", gates_passed),
                  check("changes_within_scope", not verification["out_of_scope"],
                        ", ".join(verification["out_of_scope"])),
                  check("classification_valid", classification.ok, "; ".join(classification.errors))]
        warnings = list(verification.get("warnings") or [])
        verification["passed"] = all(c["ok"] for c in checks)
        self.manager.annotate_unit(run_id, self.id, unit_id, "verification", verification)
        data["verification"] = verification
        if verification["out_of_scope"]:
            warnings += self._attention(run_id, unit_id, "out_of_scope_changes", files=verification["out_of_scope"])
            self._note_failure(run_id, unit_id, "out_of_scope_changes", R.failure_evidence(
                "out_of_scope_changes", files=verification["out_of_scope"]))
            self.manager.transition(run_id, self.id, unit_id, S.FAILED, reason="out_of_scope_changes")
            self._record_outcome(run_id, unit_id, False, False, "out_of_scope_changes", cost)
            return envelope(op, ok=False, checks=checks, warnings=warnings, data=data)
        if not classification.ok:
            self.manager.transition(run_id, self.id, unit_id, S.NEEDS_USER, reason="classification_invalid")
            return envelope(op, ok=False, checks=checks, warnings=warnings, data=data)
        if not gates_passed:
            self._note_failure(run_id, unit_id, "gates_failed", R.failure_evidence(
                "gates_failed", gates=verification.get("gates") or []))
            self.manager.transition(run_id, self.id, unit_id, S.FAILED, reason="gates_failed")
            self._record_outcome(run_id, unit_id, False, True, "gates_failed", cost)
            return envelope(op, ok=False, checks=checks, warnings=warnings, data=data)
        self.manager.transition(run_id, self.id, unit_id, S.REVIEWING, reason="gates_passed")
        self._record_outcome(run_id, unit_id, True, False, "gates_passed", cost)
        return envelope(op, changed=True, checks=checks, warnings=warnings, data=data)

    # == review / approval / merge ==
    def select_reviewer(self, run_id, unit_id):
        """Route the review of a unit in `reviewing` with
        `routing.select_reviewer`: the unit is re-classified with its actual
        diff (verified head against the base commit) and the author's routing
        record decides tier and model family independence. When no qualified
        reviewer exists for Tier 2 or high risk work the unit moves to
        `needs_user` (reason `no_qualified_reviewer`) with an attention event."""
        op = "unit.select_reviewer"
        run = self.manager.load(run_id)
        unit = run["units"].get(unit_id)
        if unit is None or unit["state"] != S.REVIEWING:
            return envelope(op, ok=False, checks=[check("unit_reviewing", False, unit["state"] if unit else "unknown")])
        head = (unit["annotations"].get("verification") or {}).get("head")
        if not head:
            return envelope(op, ok=False, checks=[check("verified_head_recorded", False)])
        stats = diff_stats(self.worktrees.repo, head, unit["workspace"]["base_commit"])
        classification = self._classify(run, unit_id, stats)
        chosen = select_reviewer(unit.get("routing") or {}, classification, self.certified_runners or [],
                                 self.catalog, self.policy, breaker=self.breaker, now=self.clock())
        data = {"reviewer": {k: getattr(chosen, k) for k in (
            "runner", "provider", "family", "model", "tier", "capability_class", "independent_family",
            "blocking", "reasons", "warnings")}}
        if chosen.ok:
            return envelope(op, checks=[check("reviewer_selected", True, chosen.runner)],
                            warnings=list(chosen.warnings), data=data)
        detail = "; ".join(chosen.reasons)
        if not chosen.blocking:
            return envelope(op, ok=False, checks=[check("reviewer_selected", False, detail)], data=data)
        warnings = self._attention(run_id, unit_id, "no_qualified_reviewer", detail=detail)
        res = self._needs_user(run_id, unit_id, "no_qualified_reviewer", detail, op)
        res["warnings"] = list(res["warnings"]) + warnings
        res["data"] = data
        return res

    def record_review(self, run_id, unit_id, review, session_id, ttl_s=3600):
        """Record an independent review. An approving review must name the
        verified `head`, the `runner` and `model` that produced it and the
        `tier` it ran at; it must satisfy the review tier and model family
        independence derived from the actual diff. Only then is a merge
        approval requested, bound to that head."""
        op = "unit.review"
        run = self.manager.load(run_id)
        unit = run["units"].get(unit_id)
        if unit is None or unit["state"] != S.REVIEWING:
            return envelope(op, ok=False, checks=[check("unit_reviewing", False, unit["state"] if unit else "unknown")])
        review = review if isinstance(review, dict) else {}
        authors = {unit.get("owner")}
        for h in run.get("handoffs", []):
            if h.get("kind") == "unit" and h.get("unit_id") == unit_id:
                authors.update({h.get("from"), h.get("to")})
        authors.discard(None)
        reviewer = review.get("reviewer")
        independent = bool(reviewer) and reviewer not in authors and reviewer != self.id
        if not independent:
            return envelope(op, ok=False, checks=[check("review_independent", False, f"reviewer {reviewer!r}")])
        verification = unit["annotations"].get("verification") or {}
        head = verification.get("head")
        verdict = review.get("verdict")
        routing = unit.get("routing") or {}
        record = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "unit_id": unit_id, "reviewer": reviewer,
                  "author": unit.get("owner"), "authors": sorted(authors), "verdict": verdict,
                  "independent": True, "notes": review.get("notes", ""), "runner": review.get("runner"),
                  "model": review.get("model"), "tier": review.get("tier"), "head": review.get("head"),
                  "session_id": session_id, "author_runner": routing.get("runner"),
                  "author_family": routing.get("family")}
        if review.get("runner"):
            try:
                record["family"] = model_family(review["runner"], review.get("model"))
            except (KeyError, ValueError):
                record["family"] = None
        if verdict == "approved":
            if self.broker is None:
                return envelope(op, ok=False, checks=[check("approval_broker", False)])
            tip = git_out(["rev-parse", f"refs/heads/{unit['workspace']['branch']}"], self.worktrees.repo)
            checks = [check("review_head_matches", bool(head) and review.get("head") == head,
                            f"review={review.get('head')} verified={head}"),
                      check("branch_matches_verified_head", bool(head) and tip == head, f"tip={tip}")]
            if not all(c["ok"] for c in checks):
                return envelope(op, ok=False, checks=checks)
            stats = diff_stats(self.worktrees.repo, head, unit["workspace"]["base_commit"])
            classification = self._classify(run, unit_id, stats)
            sufficient, reasons = review_satisfies(review, routing, classification, self.certified_runners,
                                                   self.catalog, self.policy, breaker=self.breaker, now=self.clock())
            if not sufficient:
                warnings = self._attention(run_id, unit_id, "review_insufficient", detail="; ".join(reasons))
                return envelope(op, ok=False, warnings=warnings,
                                checks=checks + [check("review_sufficient", False, "; ".join(reasons))])
            req = self.broker.request(run_id=run_id, unit_id=unit_id, action="merge", plan_sha256=run["plan_sha256"],
                                      session_id=session_id, ttl_s=ttl_s, head_sha=head,
                                      summary=f"merge {unit_id} at {head[:12]} into {self.target}")
            if not req["ok"]:
                return req
            record["approval_request_id"] = req["data"]["request_id"]
            res = self.manager.annotate_unit(run_id, self.id, unit_id, "review", record)
            if not res["ok"]:
                return res
            return envelope(op, changed=True, checks=checks + [check("review_independent", True),
                                                                check("review_sufficient", True)],
                            required_user_actions=req["required_user_actions"],
                            data={"review": record, "approval_request": req["data"]})
        res = self.manager.annotate_unit(run_id, self.id, unit_id, "review", record)
        if not res["ok"]:
            return res
        target = S.READY if verdict == "changes_requested" else S.FAILED
        self.manager.transition(run_id, self.id, unit_id, target, reason=f"review:{verdict}")
        return envelope(op, ok=False, changed=True, checks=[check("review_approved", False, str(verdict))],
                        data={"review": record})

    def enqueue_merge(self, run_id, unit_id, approval=None):
        """Queue a reviewed unit. Only the approval's request id is used; the
        queue resolves and consumes it through the broker."""
        op = "merge.enqueue"
        run = self.manager.load(run_id)
        unit = run["units"][unit_id]
        if unit["state"] != S.REVIEWING:
            return envelope(op, ok=False, checks=[check("unit_reviewing", False, unit["state"])])
        review = unit["annotations"].get("review") or {}
        rid = approval_request_id(approval) if approval is not None else review.get("approval_request_id")
        expected = review.get("approval_request_id")
        if expected and rid != expected:
            return envelope(op, ok=False, checks=[check("approval_usable", False,
                                                        f"approval {rid!r} is not the one requested for this review")])
        res = self.queue.enqueue(run_id=run_id, plan_sha256=run["plan_sha256"], unit=self.manager.plan_unit(run, unit_id),
                                 branch=unit["workspace"]["branch"],
                                 verification=unit["annotations"].get("verification"), review=review,
                                 approval=rid, author=unit.get("owner"), routing=unit.get("routing"))
        if res["ok"]:
            moved = self.manager.transition(run_id, self.id, unit_id, S.QUEUED_FOR_MERGE, reason="approved")
            if not moved["ok"]:
                return moved
        return res

    def process_merge_queue(self):
        res = self.queue.process_next()
        data = res.get("data") or {}
        status = data.get("status")
        if status in (None, "empty", "would_merge"):
            return res
        run_id, unit_id = data["run_id"], data["unit_id"]
        summary = {k: data.get(k) for k in ("status", "merged_commit", "previous_commit", "conflicts", "detail")}
        self.manager.annotate_unit(run_id, self.id, unit_id, "merge", copy.deepcopy(summary))
        if status == "merged" and data.get("previous_commit") and data.get("merged_commit"):
            self._note_own_ref_move(f"refs/heads/{self.target}", data["previous_commit"], data["merged_commit"])
        target = {"merged": S.MERGED, "conflict": S.BLOCKED, "stale": S.BLOCKED, "gate_failed": S.FAILED}.get(status)
        if target:
            moved = self.manager.transition(run_id, self.id, unit_id, target, reason=f"merge:{status}")
            if not moved["ok"]:
                res["warnings"] = list(res["warnings"]) + [f"state update failed: {moved['checks']}"]
        return res
