"""Sequential, local-only merge queue.

A unit is accepted only when all of the following hold:

* its verification passed for this unit, recorded a valid handoff, found no
  out of scope changes, and recorded the verified head commit;
* the branch tip still equals that verified head;
* the actual diff (merge base to tip) stays inside the declared scope
  (`scope.files` globs); anything outside blocks the merge;
* an independent review approved that same head, and it satisfies the
  review tier and model family independence that `routing.classify` and
  `select_reviewer` require for the actual changed files and diff size;
* a merge approval resolved through the `ApprovalBroker` is approved,
  unexpired, bound to this run, unit, action, session, plan hash and head,
  and not used before. It is consumed on enqueue. Approval records handed in
  by callers are never trusted on their own; only the request id is used.

Processing takes one item at a time under a file lock: the branch tip must
still equal the verified head, a dry-run merge check, a real merge in a
throwaway worktree, gates re-run on the merged result, then a local
fast-forward of the target branch with compare-and-swap. Nothing is ever
pushed.
"""

import copy
import os
import shutil
import tempfile
import time

from ..routing.classify import classify
from ..routing.selection import review_satisfies
from ..runs.envelope import SCHEMA_VERSION, check, envelope
from ..runs.fsutil import FileLock, atomic_write_json, read_json
from ..runs.store import iso
from ..worktrees.git import GitError, git_out, run_git, toplevel
from ..worktrees.manager import validate_root
from .conflicts import diff_stats, out_of_scope_changes

_HEX = set("0123456789abcdef")


def _is_sha(value):
    return isinstance(value, str) and len(value) >= 40 and set(value) <= _HEX


def _inside(child, parent):
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def _remove_throwaway(repo, path):
    run_git(["worktree", "remove", "--force", path], repo, check=False)
    shutil.rmtree(path, ignore_errors=True)
    run_git(["worktree", "prune"], repo, check=False)


def merge_check(repo, target, branch, scratch_root, force_fallback=False, config=None):
    """Predict whether `branch` merges cleanly into `target` without touching refs.

    Uses git merge-tree in write-tree mode when available (writes objects only),
    otherwise a throwaway detached worktree that is removed afterwards.
    """
    if not force_fallback:
        proc = run_git(["merge-tree", "--write-tree", "--name-only", "--no-messages", target, branch], repo,
                       check=False, config=config)
        if proc.returncode in (0, 1):
            lines = [line for line in proc.stdout.splitlines() if line]
            conflicts = sorted(set(lines[1:])) if proc.returncode == 1 else []
            return {"clean": proc.returncode == 0, "conflicts": conflicts, "strategy": "merge-tree",
                    "tree": lines[0] if lines else None}
    os.makedirs(scratch_root, exist_ok=True)
    path = tempfile.mkdtemp(prefix="check-", dir=scratch_root)
    try:
        run_git(["worktree", "add", "--detach", path, target], repo)
        proc = run_git(["merge", "--no-commit", "--no-ff", branch], path, check=False, config=config)
        conflicts = []
        if proc.returncode != 0:
            out = git_out(["diff", "--name-only", "--diff-filter=U"], path)
            conflicts = sorted(set(line for line in out.splitlines() if line)) or ["<unknown>"]
        run_git(["merge", "--abort"], path, check=False)
        return {"clean": proc.returncode == 0, "conflicts": conflicts, "strategy": "worktree", "tree": None}
    finally:
        _remove_throwaway(repo, path)


def check_review(review, run_id, unit_id, author=None):
    checks = []
    if not isinstance(review, dict):
        return [check("review_present", False, "no review record")]
    checks.append(check("review_present", True))
    checks.append(check("review_bound", review.get("run_id") == run_id and review.get("unit_id") == unit_id,
                        f"{review.get('run_id')}/{review.get('unit_id')}"))
    checks.append(check("review_approved", review.get("verdict") == "approved", str(review.get("verdict"))))
    reviewer = review.get("reviewer")
    listed = review.get("authors") if isinstance(review.get("authors"), list) else []
    authors = {a for a in [review.get("author"), author, *listed] if isinstance(a, str) and a}
    independent = bool(reviewer) and review.get("independent") is True and reviewer not in authors
    checks.append(check("review_independent", independent, f"reviewer={reviewer} author={sorted(authors)}"))
    return checks


def approval_request_id(approval):
    """Only the request id of a caller supplied approval is ever used."""
    if isinstance(approval, str):
        return approval
    if isinstance(approval, dict) and isinstance(approval.get("request_id"), str):
        return approval["request_id"]
    return None


class MergeQueue:
    def __init__(self, repo, target_branch, state_path, scratch_root, *, gate_runner, clock=time.time,
                 emitter=None, merge_config=None, force_fallback=False, lock_timeout=30.0, broker=None,
                 policy=None, certified_runners=None, catalog=None, breaker=None):
        self.repo = toplevel(repo)
        self.target = target_branch
        self.state_path = os.path.realpath(os.path.abspath(state_path))
        if _inside(self.state_path, self.repo):
            raise ValueError("merge queue state must live outside the repository")
        res = validate_root(scratch_root, self.repo)
        if not res["ok"]:
            raise ValueError("invalid scratch root: " + "; ".join(c["name"] for c in res["checks"] if not c["ok"]))
        self.scratch = res["data"]["root"]
        self.gate_runner = gate_runner
        self.clock = clock
        self.emitter = emitter
        self.config = dict(merge_config or {})
        self.force_fallback = force_fallback
        self.lock_timeout = lock_timeout
        # Approvals are resolved only through the broker; without one the
        # queue accepts nothing.
        self.broker = broker
        self.policy = policy
        self.certified_runners = certified_runners
        self.catalog = catalog
        self.breaker = breaker

    # == state ==
    def _lock(self):
        return FileLock(self.state_path + ".lock", timeout=self.lock_timeout)

    def state(self):
        if os.path.exists(self.state_path):
            return read_json(self.state_path)
        return {"schema_version": SCHEMA_VERSION, "repo": self.repo, "target_branch": self.target,
                "items": [], "consumed_approvals": [], "history": []}

    def _save(self, st):
        st["schema_version"] = SCHEMA_VERSION
        atomic_write_json(self.state_path, st)

    def _emit(self, event_type, run_id, unit_id, data):
        if self.emitter is None:
            return []
        try:
            self.emitter.emit(event_type, run_id, unit_id=unit_id, data=data)
        except Exception as exc:
            return [f"event emission failed: {exc}"]
        return []

    def _rev(self, ref):
        proc = run_git(["rev-parse", "--verify", "--quiet", ref + "^{commit}"], self.repo, check=False)
        return proc.stdout.strip() or None

    # == enqueue ==
    def _scope_and_review_checks(self, unit, tip, review, routing):
        """Actual diff against declared scope, then re-classification of the
        unit with the actual files and line counts; the recorded review must
        satisfy the resulting review tier and family independence."""
        target_oid = self._rev(f"refs/heads/{self.target}")
        if target_oid is None:
            return [check("target_branch_exists", False, self.target)], {}
        stats = diff_stats(self.repo, tip, target_oid)
        scope = list((unit.get("scope") or {}).get("files") or [])
        outside = out_of_scope_changes(stats["files"], scope)
        checks = [check("changes_within_scope", not outside, ", ".join(outside))]
        classification = classify(unit, self.policy, stats)
        checks.append(check("classification_valid", classification.ok, "; ".join(classification.errors)))
        sufficient, reasons = review_satisfies(review, routing, classification, self.certified_runners,
                                               self.catalog, self.policy, breaker=self.breaker, now=self.clock())
        checks.append(check("review_sufficient", sufficient, "; ".join(reasons)))
        info = {"changed_files": stats["files"], "outside_scope": outside,
                "review_tier": classification.review_tier, "minimum_tier": classification.minimum_tier,
                "risk": classification.risk, "review_reasons": reasons}
        return checks, info

    def enqueue(self, *, run_id, plan_sha256, unit, branch, verification, review, approval, author=None,
                routing=None, dry_run=False):
        """`routing` is the author's routing record (runner, model, tier,
        family) captured at assignment; it decides review independence.
        `approval` is a request id or a record carrying one."""
        op = "merge_queue.enqueue"
        unit = unit if isinstance(unit, dict) else {}
        unit_id = unit.get("id")
        ver = verification if isinstance(verification, dict) else {}
        rev = review if isinstance(review, dict) else {}
        head = ver.get("head")
        info = {}
        with self._lock():
            st = self.state()
            checks = []
            ver_ok = (ver.get("passed") is True and ver.get("unit_id") == unit_id
                      and ver.get("handoff_valid") is True and not ver.get("out_of_scope"))
            checks.append(check("verification_passed", ver_ok))
            checks.append(check("verified_head_recorded", _is_sha(head), str(head)))
            tip = self._rev(f"refs/heads/{branch}")
            checks.append(check("branch_exists", tip is not None, branch))
            checks.append(check("branch_matches_verified_head", tip is not None and tip == head,
                                f"tip={tip} verified={head}"))
            checks.extend(check_review(review, run_id, unit_id, author))
            checks.append(check("review_head_matches", _is_sha(head) and rev.get("head") == head,
                                f"review={rev.get('head')} verified={head}"))
            if tip is not None:
                more, info = self._scope_and_review_checks(unit, tip, review, routing)
                checks.extend(more)
            rid = approval_request_id(approval)
            binding = {"run_id": run_id, "unit_id": unit_id, "action": "merge", "plan_sha256": plan_sha256,
                       "session_id": rev.get("session_id"), "head_sha": head}
            if self.broker is None:
                checks.append(check("approval_broker", False, "no approval broker configured"))
            else:
                usable = self.broker.usable(rid, **binding)
                failed = [f"{c['name']}:{c['detail']}" for c in usable if not c["ok"]]
                checks.append(check("approval_usable", not failed, "; ".join(failed)))
            queued_ids = [i["approval_request_id"] for i in st["items"]]
            checks.append(check("approval_not_replayed", bool(rid) and rid not in set(st["consumed_approvals"] + queued_ids),
                                str(rid)))
            already = any(i["run_id"] == run_id and i["unit_id"] == unit_id for i in st["items"])
            checks.append(check("not_already_queued", not already))
            ok = all(c["ok"] for c in checks)
            item = {
                "run_id": run_id, "unit_id": unit_id, "unit": copy.deepcopy(unit), "branch": branch,
                "branch_tip": tip, "verified_head": head, "plan_sha256": plan_sha256,
                "approval_request_id": rid, "reviewer": rev.get("reviewer"), "enqueued_at": iso(self.clock()),
                "classification": {k: info.get(k) for k in ("review_tier", "minimum_tier", "risk")},
            }
            if ok and not dry_run:
                consumed = self.broker.consume(rid, **binding)
                if not consumed["ok"]:
                    ok = False
                    checks.append(check("approval_consumed", False,
                                        "; ".join(c["name"] for c in consumed["checks"] if not c["ok"])))
            if not ok or dry_run:
                warnings = [] if dry_run else self._blocked_attention(run_id, unit_id, checks, info)
                return envelope(op, ok=ok, changed=False, checks=checks, warnings=warnings,
                                data={"item": item, "position": len(st["items"])})
            st["items"].append(item)
            st["consumed_approvals"].append(rid)
            self._save(st)
        return envelope(op, changed=True, checks=checks, data={"item": item, "position": len(st["items"]) - 1})

    def _blocked_attention(self, run_id, unit_id, checks, info):
        failed = {c["name"] for c in checks if not c["ok"]}
        warnings = []
        if "changes_within_scope" in failed:
            warnings += self._emit("attention.required", run_id, unit_id,
                                   {"reason": "out_of_scope_changes", "files": info.get("outside_scope") or []})
        if "review_sufficient" in failed:
            warnings += self._emit("attention.required", run_id, unit_id,
                                   {"reason": "review_insufficient",
                                    "detail": "; ".join(info.get("review_reasons") or [])})
        return warnings

    # == process ==
    def _checked_out_at(self):
        out = git_out(["worktree", "list", "--porcelain"], self.repo)
        path = None
        for line in out.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree "):]
            elif line == f"branch refs/heads/{self.target}":
                return path
        return None

    def _fast_forward(self, old, new):
        wt = self._checked_out_at()
        if wt:
            dirty = git_out(["status", "--porcelain", "--untracked-files=no"], wt)
            if dirty:
                return "target_dirty", f"target checkout {wt} has local changes"
            if git_out(["rev-parse", "HEAD"], wt) != old:
                return "stale", "target moved during merge"
            run_git(["merge", "--ff-only", "--no-edit", new], wt, config=self.config)
            return "merged", wt
        proc = run_git(["update-ref", f"refs/heads/{self.target}", new, old], self.repo, check=False)
        if proc.returncode != 0:
            return "stale", proc.stderr.strip()
        return "merged", None

    def _finish(self, st, item, status, dry_run, data, warnings=(), keep=False):
        record = dict(item, status=status, processed_at=iso(self.clock()))
        record.pop("unit", None)
        record.update({k: v for k, v in data.items() if k in ("merged_commit", "previous_commit", "conflicts", "detail")})
        if not dry_run:
            if not keep:
                st["items"].pop(0)
            st["history"].append(record)
            self._save(st)
        return record

    def process_next(self, dry_run=False):
        op = "merge_queue.process"
        with self._lock():
            st = self.state()
            if not st["items"]:
                return envelope(op, changed=False, data={"status": "empty"})
            item = st["items"][0]
            run_id, unit_id = item["run_id"], item["unit_id"]
            base = {"unit_id": unit_id, "run_id": run_id, "branch": item["branch"]}
            target_oid = self._rev(f"refs/heads/{self.target}")
            tip = self._rev(f"refs/heads/{item['branch']}")
            verified = item.get("verified_head", item["branch_tip"])
            if target_oid is None or tip != item["branch_tip"] or tip != verified:
                detail = "target branch missing" if target_oid is None else "branch moved after verification and approval"
                data = dict(base, status="stale", detail=detail)
                self._finish(st, item, "stale", dry_run, data)
                warnings = [] if dry_run else self._emit("attention.required", run_id, unit_id,
                                                         {"reason": "merge_stale", "detail": detail})
                return envelope(op, ok=False, changed=not dry_run, checks=[check("branch_unchanged", False, detail)],
                                warnings=warnings, data=data,
                                required_user_actions=[{"kind": "reverify_and_reapprove", "run_id": run_id,
                                                        "unit_id": unit_id, "detail": detail}])
            mc = merge_check(self.repo, target_oid, tip, self.scratch, self.force_fallback, self.config)
            if not mc["clean"]:
                data = dict(base, status="conflict", conflicts=mc["conflicts"], merge_check=mc)
                self._finish(st, item, "conflict", dry_run, data)
                warnings = [] if dry_run else self._emit("attention.required", run_id, unit_id,
                                                         {"reason": "merge_conflict", "files": mc["conflicts"]})
                return envelope(op, ok=False, changed=not dry_run, checks=[check("merge_clean", False, ",".join(mc["conflicts"]))],
                                warnings=warnings, data=data,
                                required_user_actions=[{"kind": "resolve_conflict", "run_id": run_id, "unit_id": unit_id,
                                                        "detail": ", ".join(mc["conflicts"])}])
            commands = [["git", "worktree", "add", "--detach", "<scratch>", target_oid],
                        ["git", "merge", "--no-edit", tip], ["<gates>"],
                        ["git", "merge", "--ff-only", "<merged>"] if self._checked_out_at()
                        else ["git", "update-ref", f"refs/heads/{self.target}", "<merged>", target_oid]]
            if dry_run:
                return envelope(op, changed=False, checks=[check("merge_clean", True)],
                                data=dict(base, status="would_merge", merge_check=mc, commands=commands))
            os.makedirs(self.scratch, exist_ok=True)
            path = tempfile.mkdtemp(prefix=f"merge-{unit_id}-", dir=self.scratch)
            try:
                run_git(["worktree", "add", "--detach", path, target_oid], self.repo)
                proc = run_git(["merge", "--no-edit", tip], path, check=False, config=self.config)
                if proc.returncode != 0:
                    data = dict(base, status="conflict", conflicts=["<merge failed>"], detail=proc.stderr.strip())
                    self._finish(st, item, "conflict", False, data)
                    return envelope(op, ok=False, changed=True, checks=[check("merge_clean", False, proc.stderr.strip())],
                                    data=data)
                merged = git_out(["rev-parse", "HEAD"], path)
                verification = self.gate_runner(item["unit"], path)
                if not verification.get("passed"):
                    data = dict(base, status="gate_failed", verification=verification, merged_commit=None)
                    self._finish(st, item, "gate_failed", False, data)
                    warnings = self._emit("attention.required", run_id, unit_id, {"reason": "merged_result_failed_gates"})
                    return envelope(op, ok=False, changed=True, checks=[check("gates_on_merged_result", False)],
                                    warnings=warnings, data=data,
                                    required_user_actions=[{"kind": "fix_failing_gates", "run_id": run_id,
                                                            "unit_id": unit_id, "detail": "gates failed on merged result"}])
                status, detail = self._fast_forward(target_oid, merged)
            finally:
                _remove_throwaway(self.repo, path)
            if status != "merged":
                keep = status == "target_dirty"
                data = dict(base, status=status, detail=detail, verification=verification)
                self._finish(st, item, status, False, data, keep=keep)
                return envelope(op, ok=False, changed=True, checks=[check("fast_forward", False, detail)], data=data,
                                required_user_actions=[{"kind": status, "run_id": run_id, "unit_id": unit_id,
                                                        "detail": detail}])
            data = dict(base, status="merged", merged_commit=merged, previous_commit=target_oid,
                        verification=verification)
            self._finish(st, item, "merged", False, data)
        warnings = self._emit("merge.completed", run_id, unit_id, {
            "target_branch": self.target, "merged_commit": merged, "previous_commit": target_oid,
            "branch": item["branch"], "pushed": False,
        })
        return envelope(op, changed=True, checks=[check("merge_clean", True), check("gates_on_merged_result", True),
                                                  check("fast_forward", True)],
                        warnings=warnings, data=data,
                        rollback_checkpoint={"target_branch": self.target, "previous_commit": target_oid,
                                             "restore": ["git", "update-ref", f"refs/heads/{self.target}", target_oid, merged]})
