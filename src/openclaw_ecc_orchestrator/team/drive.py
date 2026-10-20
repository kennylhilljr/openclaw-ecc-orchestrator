"""`ecc drive`: one command per milestone (TEAM-DELIVERY-v2 sections 4.2 and 7).

Milestone start: the architect checks the plan. Then, until every unit is
merged: dispatch ready units (the repair loop returns failed ones to ready),
run the team step on units in review, process the merge queue. Milestone end:
the architect's drift check, the delivery lead's retrospective, then the
product owner pair. The drive stops only for section 7's conditions.
"""

import json
import os
import shlex

from ..merge_queue.conflicts import diff_stats
from ..runs import state as S
from ..runs.envelope import check, envelope
from ..runs.fsutil import atomic_write_json
from ..runs.store import iso
from ..worktrees.git import git_out, run_git
from . import config as TC
from . import evidence as E
from . import personas as P

PRODUCT_MARK = "ECC_PRODUCT"
MAX_STEPS = 500


def product_prompt(pack, head):
    return "\n\n".join([
        "You are a product owner on an agent delivery team, accepting a milestone (program increment). Agents "
        "built it; you did not. Judge the milestone as a whole against its exit criteria and the goals it "
        "owns, from the evidence below: the requirement and test plan sections, the milestone gate output, the "
        "architect's drift report and the delivery lead's retrospective.",
        "For every exit criterion say whether it is met and cite the evidence (a command and its output, or a "
        "file and line). A criterion with no evidence is not met. Accept only when every criterion is met.",
        "End your reply with exactly one line of single-line JSON, nothing after it:\n"
        f'{PRODUCT_MARK} {{"verdict": "accept|reject", "head": "{head}", "criteria": [{{"criterion": "<text>", '
        '"met": true, "evidence": "<text>"}], "findings": [{"severity": "blocker|major|minor", "what": "<text>"}]}',
        "# Evidence\n\n" + pack])


def plan_check_prompt(pack, head):
    return "\n\n".join([
        "You are the architect on an agent delivery team. Before any unit of this milestone runs, check the "
        "plan against the design: every unit matches its design section, scopes do not overlap, dependencies "
        "are complete and in the right order, and each shared contract has exactly one owning unit that runs "
        "before its users. Report only problems that would make a unit fail or force rework.",
        P._SEVERITY_RULES,
        "End your reply with exactly one line of single-line JSON, nothing after it:\n"
        f'{P.REVIEW_MARK} {{"verdict": "approved|changes_requested|rejected", "head": "{head}", '
        '"findings": [{"severity": "blocker|major|minor", "where": "<unit or section>", "what": "<text>"}]}',
        "# Evidence\n\n" + pack])


def drift_prompt(pack, head):
    return "\n\n".join([
        "You are the architect on an agent delivery team. The milestone's units are merged. Compare the code "
        "on main with the design: module boundaries, the names the design owns, layering rules and recorded "
        "decisions. List every drift with where it is and what the design says. Drift is a finding, not a "
        "verdict on the milestone; the product owner reads your report.",
        "End your reply with exactly one line of single-line JSON, nothing after it:\n"
        f'{P.REVIEW_MARK} {{"verdict": "approved|changes_requested", "head": "{head}", '
        '"findings": [{"severity": "blocker|major|minor", "where": "<path>", "what": "<text>"}]}',
        "# Evidence\n\n" + pack])


def retro_prompt(table, head):
    return "\n\n".join([
        "You are the delivery lead on an agent delivery team. Write the milestone retrospective from the "
        "numbers below: where time, cost and attempts went, which failures repeated, which roles and providers "
        "agreed or disagreed with later stages, and the two or three platform or plan changes that would help "
        "most next milestone. Be specific and brief.",
        "End your reply with exactly one line of single-line JSON, nothing after it:\n"
        f'{P.REVIEW_MARK} {{"verdict": "approved", "head": "{head}", "findings": [{{"severity": "minor", '
        '"what": "<recommendation>"}]}',
        "# Numbers\n\n```json\n" + json.dumps(table, indent=1, default=str) + "\n```"])


def parse_product(text, head):
    lines = [ln for ln in (text or "").splitlines() if ln.strip().startswith(PRODUCT_MARK)]
    if not lines:
        return None, f"no {PRODUCT_MARK} line"
    try:
        doc = json.loads(lines[-1].strip()[len(PRODUCT_MARK):].strip())
    except ValueError:
        return None, f"{PRODUCT_MARK} line is not JSON"
    if not isinstance(doc, dict) or doc.get("verdict") not in ("accept", "reject"):
        return None, "verdict must be accept or reject"
    if not isinstance(doc.get("head"), str) or len(doc["head"]) < 7 or not head.startswith(doc["head"]):
        return None, "verdict names a different head"
    criteria = [c for c in doc.get("criteria") or [] if isinstance(c, dict)]
    doc["criteria"] = criteria
    if doc["verdict"] == "accept" and (not criteria or any(c.get("met") is not True for c in criteria)):
        doc["verdict"] = "reject"
        doc["downgraded"] = "accept with an unmet or missing criterion"
    return doc, None


class Drive:
    """`dispatch(unit_id) -> envelope` runs one unit through dispatch (the CLI
    path, or a test seam); `step` is a team.review.TeamStep on conductor `c`."""

    def __init__(self, c, step, team_cfg, run_id, *, dispatch, clock, max_cost_usd=None, max_hours=None,
                 progress=None, reports_dir, gate_runner=None):
        self.c = c
        self.step = step
        self.cfg = team_cfg
        self.run_id = run_id
        self.dispatch = dispatch
        self.clock = clock
        self.max_cost = max_cost_usd
        self.max_seconds = None if max_hours is None else float(max_hours) * 3600
        self.progress = progress or (lambda line: None)
        self.reports_dir = reports_dir
        self.gate_runner = gate_runner
        self.state_path = os.path.join(step.state_dir, "team", f"{run_id}-drive.json")
        self.state = self._load()

    # == persistence ==
    def _load(self):
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                doc = json.load(fh)
            return doc if isinstance(doc, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        atomic_write_json(self.state_path, self.state)

    # == helpers ==
    def run(self):
        return self.c.manager.load(self.run_id)

    def spent(self, run):
        return round(sum(float(u.get("cost_usd") or 0.0) for u in run["units"].values()), 6)

    def _consult(self, role, mark, prompt_fn, head, *, exclude=(), require_kind=None):
        cls = TC.role_class(self.cfg, role)
        tier = TC.CLASS_TIER[cls]
        candidates = self.step.candidates(tier, "low", None, None, False)
        rotation = self.step.rotation(self.run_id)
        repo = self.c.worktrees.repo
        return self.step._play(role, mark, prompt_fn, run_id=self.run_id, unit_id="_milestone", rotation=rotation,
                               panel=TC.panel_for(self.cfg, role, cls), candidates=candidates,
                               exclude=set(exclude), pack_tokens=0, head=head,
                               cwd_fn=lambda: (repo, lambda: None), writable=False, timeout=1800.0,
                               require_kind=require_kind)

    def _stop(self, reason, detail, extra=None):
        data = {"stop": reason, "detail": detail, "summary": self.summary()}
        data.update(extra or {})
        self.state["last_stop"] = {"reason": reason, "detail": detail, "at": iso(self.clock())}
        self._save()
        return envelope("drive", ok=False, changed=True, checks=[check("milestone_complete", False, reason)],
                        required_user_actions=[{"kind": "resolve_needs_user", "run_id": self.run_id,
                                                "detail": f"{reason}: {detail}"}], data=data)

    def summary(self):
        run = self.run()
        return {uid: {"state": u["state"], "reason": u.get("last_reason"), "attempts": u.get("attempts"),
                      "cost_usd": u.get("cost_usd")} for uid, u in run["units"].items()}

    # == milestone start ==
    def milestone_start(self):
        if self.state.get("plan_check") or "milestone_start" not in (self.cfg["roles"]["architect"].get("when") or []):
            return None
        run = self.run()
        base = self.base_commit(run)
        units = [{k: u.get(k) for k in ("id", "title", "depends_on", "scope", "acceptance", "risk")}
                 for u in run["plan"]["units"]]
        refs = E.design_references(self.c.worktrees.repo, " ".join(str(u.get("description") or "")
                                                                   for u in run["plan"]["units"]))
        pack = ("## Plan\n\n```json\n" + json.dumps(units, indent=1) + "\n```\n\n## Design references\n\n" + refs)
        self.progress("milestone start: architect plan check")
        res = self._consult("architect", P.REVIEW_MARK, lambda: plan_check_prompt(pack, base), base)
        self.state["plan_check"] = res
        self._save()
        if res.get("verdict") in ("changes_requested", "rejected"):
            return self._stop("plan_check_failed", "; ".join(str(f.get("what")) for f in res.get("findings") or []),
                              {"plan_check": res})
        if res.get("verdict") is None:
            return self._stop("no_architect", str(res.get("skipped")))
        return None

    # == loop ==
    def loop(self):
        started = self.clock()
        baseline = self.spent(self.run())
        for _ in range(MAX_STEPS):
            run = self.run()
            units = run["units"]
            order = [u["id"] for u in run["plan"]["units"]]
            if all(units[uid]["state"] == S.MERGED for uid in order):
                return None
            if self.max_cost is not None and self.spent(run) - baseline > float(self.max_cost):
                return self._stop("drive_cost_ceiling", f"spent {self.spent(run) - baseline} USD")
            if self.max_seconds is not None and self.clock() - started > self.max_seconds:
                return self._stop("drive_time_ceiling", f"ran {int(self.clock() - started)} s")
            acted = False
            for uid in order:
                state = units[uid]["state"]
                before = self._fingerprint(uid)
                if state == S.REVIEWING:
                    self.progress(f"{uid}: team review")
                    res = self.step.run(self.run_id, uid)
                elif state == S.QUEUED_FOR_MERGE:
                    self.progress(f"{uid}: merge queue")
                    res = self.c.process_merge_queue()
                elif state == S.READY:
                    self.progress(f"{uid}: dispatch (attempt {int(units[uid].get('attempts') or 0) + 1})")
                    res = self.dispatch(uid)
                else:
                    continue
                if self._fingerprint(uid) == before:
                    # Nothing moved: a plumbing refusal (plan check, lease, routing). Retrying
                    # the same call would loop, so the owner gets the refusal instead.
                    failed = [f"{c.get('name')}: {c.get('detail')}" for c in (res or {}).get("checks") or []
                              if not c.get("ok")]
                    return self._stop("no_progress", f"{uid} {state}: " + "; ".join(failed)[:2000])
                acted = True
                break
            if not acted:
                stuck = {uid: units[uid].get("last_reason") for uid in order
                         if units[uid]["state"] in (S.NEEDS_USER, S.BLOCKED, S.FAILED, S.CANCELLED)}
                return self._stop("needs_owner", json.dumps(stuck), {"diagnosis": self.triage(stuck)})
        return self._stop("drive_step_limit", f"{MAX_STEPS} steps without finishing")

    def base_commit(self, run):
        """The commit the run started from: recorded by create-run, else the
        first unit worktree's base, else the target branch tip."""
        base = (run.get("metadata") or {}).get("base_commit")
        if not base:
            base = next(((u.get("workspace") or {}).get("base_commit") for u in run["units"].values()
                         if (u.get("workspace") or {}).get("base_commit")), None)
        return base or git_out(["rev-parse", f"refs/heads/{self.c.target}"], self.c.worktrees.repo)

    def _fingerprint(self, uid):
        u = self.run()["units"][uid]
        return (u["state"], u.get("attempts"), u.get("last_reason"), len(u.get("history") or []))

    def triage(self, stuck):
        """The delivery lead's one look before the owner is interrupted
        (section 3). v2 of this platform reports the diagnosis; it does not
        re-queue a unit past its budget."""
        if not stuck or "budget_stop" not in (self.cfg["roles"]["delivery_lead"].get("when") or []):
            return None
        run = self.run()
        table = {uid: {"reason": reason, "last_failure": (run["units"][uid].get("annotations") or {}).get(
            "last_failure"), "escalation": ((run["units"][uid].get("annotations") or {}).get("escalation") or {})
            .get("attempts")} for uid, reason in stuck.items()}
        head = self.base_commit(run)
        res = self._consult("delivery_lead", P.REVIEW_MARK, lambda: retro_prompt(table, head).replace(
            "Write the milestone retrospective", "Diagnose why these units stopped and what the owner should "
            "decide; then write the retrospective"), head)
        return {"provider": res.get("provider"), "findings": res.get("findings")}

    # == milestone end ==
    def milestone_end(self):
        repo = self.c.worktrees.repo
        run = self.run()
        base = self.base_commit(run)
        head = git_out(["rev-parse", f"refs/heads/{self.c.target}"], repo)
        milestone = self.cfg.get("milestone") or {}
        report = {"run_id": self.run_id, "milestone": milestone.get("name"), "head": head, "base": base,
                  "at": iso(self.clock())}
        if "milestone_end" in (self.cfg["roles"]["architect"].get("when") or []) and not self.state.get("drift"):
            stat, diff = E.diff_text(repo, base, head)
            refs = E.design_references(repo, " ".join(str(u.get("description") or "") for u in run["plan"]["units"]))
            pack = f"## Diff stat {base[:12]}..{head[:12]}\n\n```\n{stat}\n```\n\n## Diff\n\n```diff\n{diff}\n```" \
                   f"\n\n## Design references\n\n{refs}"
            self.progress("milestone end: architect drift check")
            self.state["drift"] = self._consult("architect", P.REVIEW_MARK, lambda: drift_prompt(pack, head), head)
            self._save()
        report["drift"] = self.state.get("drift")
        table = self.retro_table(run)
        if "milestone_end" in (self.cfg["roles"]["delivery_lead"].get("when") or []) and not self.state.get("retro"):
            self.progress("milestone end: delivery lead retrospective")
            self.state["retro"] = {"table": table, "narrative": self._consult(
                "delivery_lead", P.REVIEW_MARK, lambda: retro_prompt(table, head), head)}
            self._save()
        report["retrospective"] = self.state.get("retro") or {"table": table}
        report["gates"] = self.milestone_gates(head)
        pack = self.product_pack(report)
        verdicts, used = [], set()
        for n in range(2 if self.cfg["roles"]["product_owner"].get("pair") else 1):
            self.progress(f"milestone end: product owner {n + 1}")
            res = self._consult_product(pack, head, exclude=used)
            verdicts.append(res)
            if res.get("provider"):
                used.add(res["provider"])
        report["product"] = verdicts
        # Two product owner personas in separate sessions; different providers are
        # preferred, not required (independence is the persona, owner decision 2026-10-19).
        accepted = len(verdicts) >= 1 and all(v.get("verdict") == "accept" for v in verdicts)
        report["verdict"] = "accept" if accepted else "reject"
        path = self.write_report(report)
        if not accepted:
            return self._stop("product_rejected", f"report {path}", {"report": path})
        return envelope("drive", changed=True, checks=[check("milestone_complete", True, path)],
                        data={"report": path, "summary": self.summary(), "verdict": "accept"})

    def _consult_product(self, pack, head, exclude):
        """One product owner verdict. Providers not yet used are tried first; a
        provider that errors is skipped, never counted as a reject; if no other
        provider answers, a used one plays the persona again in a new session."""
        role = "product_owner"
        cls = TC.role_class(self.cfg, role)
        candidates = self.step.candidates(TC.CLASS_TIER[cls], "low", None, None, False)
        rotation = self.step.rotation(self.run_id)
        panel = TC.panel_for(self.cfg, role, cls)
        skipped_all = []
        for avoid in (set(exclude), set()):
            tried = set()
            for _ in range(max(1, len(panel))):
                pool = [c for c in candidates if c["runner"] not in tried]
                choice, skipped = rotation.choose(role, panel, pool, exclude=avoid)
                skipped_all += [{"provider": p, "reason": r} for p, r in skipped]
                if choice is None:
                    break
                tried.add(choice["runner"])
                turn = self.step._turn(role, choice, product_prompt(pack, head), self.c.worktrees.repo, False,
                                       self.run_id, "_milestone", 1800.0)
                if turn.get("error"):
                    skipped_all.append({"provider": choice["runner"], "reason": f"runner error: {turn['error']}"})
                    continue
                doc, problem = parse_product(turn.get("text"), head)
                if doc is None:
                    again = (f"Your previous reply could not be used: {problem}. Reply again.\n\n"
                             + product_prompt(pack, head))
                    turn = self.step._turn(role, choice, again, self.c.worktrees.repo, False, self.run_id,
                                           "_milestone", 1800.0)
                    if turn.get("error"):
                        skipped_all.append({"provider": choice["runner"],
                                            "reason": f"runner error: {turn['error']}"})
                        continue
                    doc, problem = parse_product(turn.get("text"), head)
                out = {"role": role, "provider": choice["runner"], "model": turn.get("model") or choice["model"],
                       "skipped": skipped_all, "independence": "provider" if avoid or not exclude else "persona"}
                if doc is None:
                    out.update(verdict="reject", problem=problem)
                else:
                    out.update(verdict=doc["verdict"], criteria=doc["criteria"], findings=doc.get("findings"),
                               downgraded=doc.get("downgraded"))
                return out
        return {"role": role, "provider": None, "verdict": None, "skipped": skipped_all}

    def retro_table(self, run):
        units = {}
        for uid, u in run["units"].items():
            esc = (u.get("annotations") or {}).get("escalation") or {}
            team = (u.get("annotations") or {}).get("team") or {}
            units[uid] = {"attempts": u.get("attempts"), "minutes": round(float(u.get("minutes_used") or 0), 1),
                          "cost_usd": u.get("cost_usd"), "tier": (u.get("routing") or {}).get("tier"),
                          "author": (u.get("routing") or {}).get("runner"),
                          "failures": [a.get("reason") for a in esc.get("attempts") or [] if not a.get("passed")],
                          "reviews": [{"role": r.get("role"), "provider": r.get("provider"),
                                       "verdict": r.get("verdict")} for r in team.get("reviews") or []],
                          "verifier": {k: (team.get("verification") or {}).get(k) for k in ("provider", "verdict")}}
        turns = self.step.rotation(self.run_id).state.get("turns")
        return {"units": units, "role_turns_by_provider": turns}

    def milestone_gates(self, head):
        commands = (self.cfg.get("milestone") or {}).get("gate_commands") or []
        if not commands or self.gate_runner is None:
            return []
        repo = self.c.worktrees.repo
        path = os.path.join(self.step.worktree_root, "_milestone", f"{self.run_id}-{int(self.clock() * 1000)}")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        run_git(["worktree", "add", "-q", "--detach", path, head], repo)
        try:
            return [self.gate_runner(cmd, path) for cmd in commands]
        finally:
            run_git(["worktree", "remove", "--force", path], repo, check=False)

    def product_pack(self, report):
        repo = self.c.worktrees.repo
        parts = []
        for doc in (self.cfg.get("milestone") or {}).get("docs") or []:
            p = os.path.join(repo, doc.get("path", ""))
            if os.path.isfile(p):
                with open(p, encoding="utf-8", errors="replace") as fh:
                    parts.append(f"### {doc['path']}\n\n" + E.extract_sections(fh.read(), doc.get("sections") or []))
        gates = "\n".join(f"- `{g.get('command')}` exit {g.get('exit_code')}\n```\n{g.get('output_tail', '')}\n```"
                          for g in report.get("gates") or [])
        return "\n\n".join([
            f"## Milestone {report.get('milestone')} on main at {report['head']}",
            "## Exit criteria and goals\n\n" + ("\n\n".join(parts) or "(none configured)"),
            "## Milestone gate commands (run by the orchestrator on main)\n\n" + (gates or "(none configured)"),
            "## Architect drift report\n\n```json\n" + json.dumps(report.get("drift"), indent=1, default=str)[:20000]
            + "\n```",
            "## Delivery lead retrospective\n\n```json\n" + json.dumps(report.get("retrospective"), indent=1,
                                                                       default=str)[:20000] + "\n```"])

    def write_report(self, report):
        os.makedirs(self.reports_dir, exist_ok=True)
        name = f"{self.run_id}-{report.get('milestone') or 'milestone'}-product-v1"
        path = os.path.join(self.reports_dir, name + ".md")
        lines = [f"# Product report: {report.get('milestone')} ({self.run_id})", "",
                 f"- Verdict: **{report['verdict']}**", f"- Main at `{report['head']}`, base `{report['base']}`", "",
                 "## Product owners", ""]
        for v in report["product"]:
            lines.append(f"### {v.get('provider')} ({v.get('model')}): {v.get('verdict')}")
            for c in v.get("criteria") or []:
                lines.append(f"- {'met' if c.get('met') else 'NOT met'}: {c.get('criterion')} ({c.get('evidence')})")
            for f in v.get("findings") or []:
                lines.append(f"- finding ({f.get('severity')}): {f.get('what')}")
            lines.append("")
        lines += ["## Gates", ""] + [f"- `{g.get('command')}`: exit {g.get('exit_code')}" for g in report["gates"]]
        drift = report.get("drift") or {}
        lines += ["", "## Architect drift", ""] + [f"- ({f.get('severity')}) {f.get('where')}: {f.get('what')}"
                                                    for f in drift.get("findings") or []]
        retro = report.get("retrospective") or {}
        lines += ["", "## Retrospective", ""] + [f"- {f.get('what')}" for f in
                                                  ((retro.get("narrative") or {}).get("findings") or [])]
        lines += ["", "```json", json.dumps(retro.get("table"), indent=1, default=str), "```", ""]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        atomic_write_json(path[:-3] + ".json", report)
        return path

    # == entry ==
    def go(self):
        stop = self.milestone_start()
        if stop is not None:
            return stop
        stop = self.loop()
        if stop is not None:
            return stop
        return self.milestone_end()


def shell_gate_runner(supervisor, timeout=3600.0, tail_chars=4000):
    """Run a milestone gate command (shell-free argv, as the unit gates are) in `path`."""
    import collections

    def run(command, path):
        tail = collections.deque(maxlen=200)
        res = supervisor.run(shlex.split(command), cwd=path, env_allow=("HOME",), timeout=timeout,
                             on_line=lambda stream, line: tail.append(line))
        text = "\n".join(tail)
        return {"command": command, "exit_code": res.get("exit_code"), "output_tail": text[-tail_chars:]}
    return run
