"""The learning loop (TEAM-DELIVERY v7 sections 4.6 and 7.3).

Every persona records lessons in its own area while a milestone is built:
reviewers, the security reviewer, the architect and the designer through their
verdicts, the verifier through its verification, the gates through failures,
coders through `Lesson:` lines in their handoff notes, and the drive through its
heals. The delivery lead, acting as practices steward, turns the lessons that
generalize into the repository's practice files (agent instructions, skills and
a lessons ledger) on its own branch. The personas whose lessons it applied
review the change, the gates run on the merged result, and the target branch
is fast-forwarded exactly as the merge queue does. Units that start later
branch from the updated target; running units receive it through the branch
sync before their next attempt.

Nothing here is specific to one repository: the defaults below suit any repo
that keeps its instructions in AGENTS.md and its skills in .agents/skills, and
team.json's "learning" block overrides them.

Shared skills (skills-catalog SUB-11, SUB-12): the steward never owns the
`.agents/skills/shared` submodule or the `.claude/skills/<name>` links into it,
whatever the configured ownership says. A lesson about a shared skill is
written under "Proposed for the catalog" in the ledger, and when the milestone
ends one `lesson` issue per skill is opened on the catalog (catalog_lessons).
"""

import copy
import fnmatch
import json
import os
import shutil

from ..runs.store import iso
from ..worktrees.git import git_out, run_git
from . import catalog_lessons as CL
from . import config as TC
from . import evidence as E
from . import personas as P

DEFAULT_LEARNING = {
    "enabled": True,
    "steward_role": "delivery_lead",
    # Run the steward after this many more units merged, when enough new lessons wait.
    "after_merged_units": 3,
    "min_new_lessons": 2,
    "ledger": ".agents/LESSONS.md",
    "spec_paths": ["docs/spec/**"],
    # Which practice files each persona's lessons may change. Lessons from roles with an
    # empty list become proposals for the owner.
    "ownership": {
        "pr_reviewer": ["AGENTS.md", "CLAUDE.md", ".agents/skills/review-*/**", ".agents/skills/simplify-*/**"],
        "security_reviewer": [".agents/skills/security-*/**", "AGENTS.md"],
        "verifier": [".agents/skills/verify-*/**", ".agents/skills/run-*/**"],
        "architect": ["AGENTS.md"],
        "designer": [".agents/skills/design-*/**"],
        "delivery_lead": ["AGENTS.md", "CLAUDE.md"],
        "coder": ["AGENTS.md", ".agents/skills/**"],
        "gates": ["AGENTS.md", ".agents/skills/**"],
        "product_owner": [],
    },
    # Commands run on the merged result before the target moves ([] runs none).
    "gate_commands": [],
    "timeout_minutes": 30,
    # Lessons about shared skills become issues on this repository at milestone end
    # (false keeps them in the ledger only). The identity that opens them is gh's
    # login, or the token in the environment variable named by catalog_token_env.
    "catalog_issues": True,
    "catalog_repo": CL.CATALOG_REPO,
    "catalog_token_env": None,
}

# Which reviewing persona approves practice changes from each source role.
REVIEWER_FOR = {"pr_reviewer": "pr_reviewer", "security_reviewer": "security_reviewer",
                "architect": "architect", "designer": "designer"}


def settings(team_cfg):
    cfg = copy.deepcopy(DEFAULT_LEARNING)
    own = (team_cfg or {}).get("learning") or {}
    for key, value in own.items():
        if key == "ownership" and isinstance(value, dict):
            cfg["ownership"].update(copy.deepcopy(value))
        else:
            cfg[key] = copy.deepcopy(value)
    return cfg


MAX_LESSON_CHARS = 4000


class Lessons:
    """Append-only lessons log for one run: <state>/lessons/<run>.jsonl."""

    def __init__(self, state_dir, run_id):
        self.path = os.path.join(state_dir, "lessons", f"{run_id}.jsonl")
        self.clock_iso = None

    def all(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                return [json.loads(line) for line in fh if line.strip()]
        except (OSError, ValueError):
            return []

    def add(self, *, source, role, what, unit=None, severity=None, where=None, practice=None, provider=None,
            at=None):
        raw = str(what or "").strip()[:MAX_LESSON_CHARS]
        what = " ".join(raw.split())[:1000]
        if not what:
            return None
        existing = self.all()
        key = (role, unit, what)
        if any((x.get("role"), x.get("unit"), x.get("what")) == key for x in existing):
            return None  # the same lesson from the same persona on the same unit is recorded once
        entry = {"id": "L%04d" % (len(existing) + 1), "source": source, "role": role, "unit": unit,
                 "severity": severity, "where": where, "practice": practice, "provider": provider,
                 "what": what, "at": at,
                 # The text as the persona wrote it, kept when `what` normalized it (catalog issues quote it).
                 "text": raw if raw != what else None}
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({k: v for k, v in entry.items() if v is not None}) + "\n")
        return entry["id"]


def capture_verdict(lessons, unit_id, record, at=None):
    """Blocker and major findings, and explicit lessons, from one persona verdict."""
    role = record.get("role")
    if not role or record.get("verdict") is None:
        return
    for f in record.get("findings") or []:
        if isinstance(f, dict) and f.get("severity") in ("blocker", "major"):
            lessons.add(source="finding", role=role, unit=unit_id, severity=f.get("severity"),
                        where=f.get("where"), what=f.get("what"), provider=record.get("provider"), at=at)
    for x in record.get("lessons") or []:
        if isinstance(x, dict):
            lessons.add(source="lesson", role=role, unit=unit_id, practice=x.get("practice"),
                        what=x.get("lesson"), provider=record.get("provider"), at=at)


def capture_handoff(lessons, unit_id, doc, provider=None, at=None):
    """`Lesson: ...` lines a coder wrote in its handoff notes."""
    notes = doc.get("notes") if isinstance(doc, dict) else None
    for line in str(notes or "").splitlines():
        line = line.strip().lstrip("-* ").strip()
        if line.lower().startswith("lesson:"):
            lessons.add(source="coder", role="coder", unit=unit_id, what=line[7:], provider=provider, at=at)


def capture_failure(lessons, unit_id, reason, evidence, at=None):
    """A gate, scope or handoff failure: what failed, so recurring failures become rules."""
    ev = evidence or {}
    failed = [f"{g.get('name')} (`{g.get('command')}`)" for g in ev.get("gates") or []
              if isinstance(g, dict) and g.get("status") not in ("passed", None)]
    detail = "; ".join(failed) or ", ".join(ev.get("files") or []) or "; ".join(ev.get("handoff_problems") or [])
    if reason in ("runner_auth", "runner_missing"):
        return
    lessons.add(source="gates", role="gates", unit=unit_id, severity="major",
                what=f"{reason}: {detail}" if detail else reason, at=at)


class Steward:
    """The delivery lead's practices pass for one drive."""

    def __init__(self, drive):
        self.d = drive
        self.cfg = settings(drive.cfg)
        self.state = drive.state.setdefault("learning", {"runs": [], "seen": 0, "merged_at_last": 0})
        self.state.setdefault("catalog", {"entries": [], "filed": {}, "issues": []})

    def lessons(self):
        return Lessons(self.d.step.state_dir, self.d.run_id)

    def pending(self):
        return self.lessons().all()[int(self.state.get("seen", 0)):]

    def due(self, final=False):
        if not self.cfg.get("enabled"):
            return False
        new = self.pending()
        if final:
            return bool(new) or bool(self._unfiled()) or bool(self._unledgered())
        merged = sum(1 for u in self.d.run()["units"].values() if u["state"] == "merged")
        return (merged - int(self.state.get("merged_at_last", 0)) >= int(self.cfg["after_merged_units"])
                and len(new) >= int(self.cfg["min_new_lessons"]))

    # == helpers ==
    def _choice(self, role):
        cls = TC.role_class(self.d.cfg, role)
        tier = TC.CLASS_TIER[cls]
        from ..runners.registry import PROFILES
        cands = {c["runner"]: c for c in self.d.step.candidates(tier, "low", None, None, False)
                 if PROFILES[c["runner"]].kind == "coding_cli"}
        for name in TC.panel_for(self.d.cfg, role, cls) + sorted(cands):
            if name in cands:
                return cands[name]
        return None

    def _ownership(self, roles, tree):
        """Each role's practice globs, minus the shared submodule and links (SUB-11)."""
        return CL.filter_ownership({r: self.cfg["ownership"].get(r, []) for r in roles},
                                   [tree, self.d.c.worktrees.repo])

    def _allowed(self, roles, tree):
        globs = [self.cfg["ledger"]]
        for globs_of_role in self._ownership(roles, tree).values():
            globs += globs_of_role
        return CL.exclude_shared(globs, CL.protected([tree, self.d.c.worktrees.repo]))

    @staticmethod
    def _matches(path, globs):
        return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(path, g.rstrip("/*").rstrip("/") + "/*")
                   or path == g for g in globs)

    def _record(self, record):
        self.state["runs"].append(record)
        self.d._save()
        self.d.progress(f"practices: {record['status']}"
                        + (f" ({', '.join(record.get('applied') or [])})" if record.get("applied") else ""))
        return record

    # == the pass ==
    def run(self, final=False):
        lessons = self.pending()
        record = self._pass(lessons, final) if lessons else None
        if self._unledgered():
            self._catalog_ledger()  # retries a ledger merge that failed earlier
        if final:
            self.file_catalog_lessons()
        return record

    def _pass(self, lessons, final):
        repo = self.d.c.worktrees.repo
        target = self.d.c.target
        n = len(self.state["runs"]) + 1
        self.state["seen"] = int(self.state.get("seen", 0)) + len(lessons)
        self.state["merged_at_last"] = sum(1 for u in self.d.run()["units"].values() if u["state"] == "merged")
        record = {"n": n, "at": iso(self.d.clock()), "lessons": [x["id"] for x in lessons], "final": final}
        # Lessons about shared skills go to the catalog, not to the steward (SUB-11). The
        # harness writes them to the ledger on a branch of their own first, so a rejected
        # steward change cannot drop them.
        catalog = CL.catalog_entries(lessons, [repo], self.d.run_id)
        if catalog:
            known = {e["id"] for e in self.state["catalog"]["entries"]}
            self.state["catalog"]["entries"] += [e for e in catalog if e["id"] not in known]
            record["catalog"] = [e["id"] for e in catalog]
            self.d._save()
            record["catalog_ledger"] = self._catalog_ledger().get("status")
        rest = [x for x in lessons if x["id"] not in {e["id"] for e in catalog}]
        if not rest:
            return self._record(dict(record, status="catalog only"))
        role = self.cfg["steward_role"]
        choice = self._choice(role)
        if choice is None:
            return self._record(dict(record, status="skipped: no coding runner for the steward"))
        roles = sorted({x.get("role") for x in rest if x.get("role")})
        tip = git_out(["rev-parse", f"refs/heads/{target}"], repo)
        branch = f"ecc/{self.d.run_id}/practices-{n}"
        path = os.path.join(self.d.step.worktree_root, "_practices", f"{self.d.run_id}-{n}")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        run_git(["worktree", "add", "-q", "-b", branch, path, tip], repo)
        try:
            ownership = self._ownership(roles, path)
            before = CL.shared_snapshot(path)
            prompt = P.steward_prompt(rest, ownership, self.cfg["ledger"], ", ".join(self.cfg["spec_paths"]),
                                      catalog=catalog)
            self.d.progress(f"practices {n}: steward ({choice['runner']}) on {len(rest)} lesson(s)")
            turn = self.d.step._turn("steward", choice, prompt, path, True, self.d.run_id, f"_practices-{n}",
                                     float(self.cfg["timeout_minutes"]) * 60)
            head = git_out(["rev-parse", "HEAD"], path)
            report = _steward_line(turn.get("text"))
            record.update(steward=choice["runner"], branch=branch, head=head, applied=report.get("applied"),
                          proposed=report.get("proposed"), skipped=report.get("skipped"))
            if turn.get("error"):
                return self._record(dict(record, status=f"steward error: {turn['error']}"))
            if CL.shared_snapshot(path) != before:
                return self._record(dict(record, status="rejected: wrote inside the shared skills",
                                         files=[CL.SHARED_ROOT]))
            if head == tip:
                return self._record(dict(record, status="no change"))
            changed = [f for f in git_out(["diff", "--name-only", tip, head], path).splitlines() if f]
            shared = CL.refused_paths(changed, [path, repo])
            outside = [f for f in changed if f not in shared and not self._matches(f, self._allowed(roles, path))]
            spec = [f for f in changed if self._matches(f, self.cfg["spec_paths"])]
            if shared or outside or spec:
                return self._record(dict(record, status="rejected: outside the practice files",
                                         files=shared + outside + spec))
            stat, diff = E.diff_text(path, tip, head)
            pack = f"## Files\n\n```\n{stat}\n```\n\n## Diff\n\n```diff\n{diff}\n```"
            reviewers = sorted({REVIEWER_FOR.get(r, "pr_reviewer") for r in roles}) or ["pr_reviewer"]
            verdicts = []
            for rv in reviewers:
                cited = [x for x in rest if REVIEWER_FOR.get(x.get("role"), "pr_reviewer") == rv]
                res = self._review(rv, cited, pack, head, path, choice["runner"])
                verdicts.append({k: res.get(k) for k in ("role", "provider", "verdict", "findings", "independence")})
                if res.get("verdict") != "approved":
                    return self._record(dict(record, status=f"not approved by {rv}", reviews=verdicts))
            record["reviews"] = verdicts
            return self._record(self._merge(record, branch, head))
        finally:
            run_git(["worktree", "remove", "--force", path], repo, check=False)

    def _unledgered(self):
        return [e for e in (self.state.get("catalog") or {}).get("entries") or [] if not e.get("ledger")]

    def _catalog_ledger(self):
        """Write the catalog entries not yet on the target under "Proposed for the
        catalog" in the ledger, commit, run the gates and fast-forward the target.
        Entries are marked once their line is on the target (SUB-11)."""
        entries = self._unledgered()
        if not entries:
            return {"status": "nothing to write"}
        repo = self.d.c.worktrees.repo
        cat = self.state["catalog"]
        k = len(cat.setdefault("ledger_runs", [])) + 1
        record = {"n": f"catalog-{k}", "at": iso(self.d.clock()), "lessons": [e["id"] for e in entries]}
        tip = git_out(["rev-parse", f"refs/heads/{self.d.c.target}"], repo)
        branch = f"ecc/{self.d.run_id}/catalog-ledger-{k}"
        path = os.path.join(self.d.step.worktree_root, "_practices", f"{self.d.run_id}-catalog-{k}")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        run_git(["worktree", "add", "-q", "-b", branch, path, tip], repo)
        try:
            added = CL.write_ledger(os.path.join(path, self.cfg["ledger"]), entries, iso(self.d.clock())[:10])
            if added:
                run_git(["add", "--", self.cfg["ledger"]], path)
                run_git(["commit", "-q", "-m", f"Practices: proposed for the catalog ({', '.join(added)})"], path)
                record = self._merge(record, branch, git_out(["rev-parse", "HEAD"], path))
            else:
                record["status"] = "already in the ledger"
        finally:
            run_git(["worktree", "remove", "--force", path], repo, check=False)
        if record.get("status") in ("merged", "already in the ledger"):
            for e in entries:
                e["ledger"] = True
        cat["ledger_runs"].append(record)
        self.d._save()
        self.d.progress(f"practices: catalog ledger {record['status']} ({', '.join(record['lessons'])})")
        return record

    # == lesson issues on the catalog (SUB-12) ==
    def _unfiled(self):
        if not self.cfg.get("catalog_issues"):
            return []
        cat = self.state.get("catalog") or {}
        repo_name, milestone = self._names()
        filed = cat.get("filed") or {}
        # Only skills whose every entry is on the target in the ledger; the rest wait for a retry.
        waiting = {e["skill"] for e in cat.get("entries") or [] if not e.get("ledger")}
        return [e for e in cat.get("entries") or [] if e["skill"] not in waiting
                and CL.marker(repo_name, milestone, e["skill"]) not in filed]

    def _names(self):
        repo_name = self.state.setdefault("catalog", {}).get("repo")
        if not repo_name:
            repo = self.d.c.worktrees.repo
            proc = run_git(["remote", "get-url", "origin"], repo, check=False)
            repo_name = CL.repo_slug(repo, proc.stdout if proc.returncode == 0 else None)
        self.state["catalog"]["repo"] = repo_name
        milestone = str((self.d.cfg.get("milestone") or {}).get("name") or self.d.run_id)
        return repo_name, milestone

    def _unit_link(self, repo_name):
        units = self.d.run()["units"]

        def link(uid):
            commit = (((units.get(uid) or {}).get("annotations") or {}).get("merge") or {}).get("merged_commit")
            if commit and "/" in repo_name:
                return f"https://github.com/{repo_name}/commit/{commit}"
            return None
        return link

    def file_catalog_lessons(self):
        """At milestone end: one `lesson` issue per shared skill on the catalog.
        The opener is the drive's `catalog_issues` (an object with `open(title,
        body, label)` and `exists(marker)`), else gh. It never writes inside
        the submodule and never opens a pull request on the catalog."""
        entries = self._unfiled()
        if not entries:
            return []
        repo_name, milestone = self._names()
        issues = getattr(self.d, "catalog_issues", None) or CL.GhIssues(
            self.cfg.get("catalog_repo") or CL.CATALOG_REPO, self.cfg.get("catalog_token_env"))
        cat = self.state["catalog"]
        results = CL.file_issues(entries, repo=repo_name, milestone=milestone, opener=issues.open,
                                 exists=issues.exists, unit_link=self._unit_link(repo_name),
                                 filed=cat.setdefault("filed", {}))
        cat.setdefault("issues", []).extend(results)
        self.d._save()
        for r in results:
            self.d.progress(f"catalog lesson {r['skill']}: {r['status']}"
                            + (f" {r['url']}" if r.get("url") else "") + (f" ({r['error']})" if r.get("error") else ""))
        return results

    def _review(self, role, lessons, pack, head, path, steward_runner):
        cls = TC.role_class(self.d.cfg, role, 1)
        tier = TC.CLASS_TIER[cls]
        cands = self.d.step.candidates(tier, "low", None, None, False)
        return self.d.step._play_preferring_other_providers(
            role, P.REVIEW_MARK, lambda: P.practices_review_prompt(role, lessons, pack, head),
            avoid={steward_runner}, strict_candidates=cands, open_candidates=cands, run_id=self.d.run_id,
            unit_id="_practices", rotation=self.d.step.rotation(self.d.run_id),
            panel=TC.panel_for(self.d.cfg, role, cls), pack_tokens=E.estimate_tokens(pack), head=head,
            cwd_fn=lambda: (path, lambda: None), writable=False, timeout=1800.0)

    def _merge(self, record, branch, head):
        """Merge into a scratch checkout of the target's tip, run the gates there, then
        fast-forward the target as the merge queue does."""
        repo = self.d.c.worktrees.repo
        target = self.d.c.target
        old = git_out(["rev-parse", f"refs/heads/{target}"], repo)
        scratch = os.path.join(self.d.step.worktree_root, "_practices", f"merge-{self.d.run_id}-{record['n']}")
        run_git(["worktree", "add", "-q", "--detach", scratch, old], repo)
        try:
            proc = run_git(["merge", "--no-edit", "-m", f"Merge {branch} (practices)", head], scratch, check=False)
            if proc.returncode != 0:
                return dict(record, status="conflict", detail=(proc.stderr or proc.stdout).strip()[:500])
            merged = git_out(["rev-parse", "HEAD"], scratch)
            gates = []
            for command in self.cfg.get("gate_commands") or []:
                res = self.d.gate_runner(command, scratch) if self.d.gate_runner else {"exit_code": 0}
                gates.append({"command": command, "exit_code": res.get("exit_code")})
                if res.get("exit_code") != 0:
                    return dict(record, status="gates failed", gates=gates)
            status, detail = self.d.c.queue._fast_forward(old, merged)
            if status != "merged":
                return dict(record, status=status, detail=detail, gates=gates)
            return dict(record, status="merged", merged_commit=merged, previous_commit=old, gates=gates)
        finally:
            run_git(["worktree", "remove", "--force", scratch], repo, check=False)
            shutil.rmtree(scratch, ignore_errors=True)


def _steward_line(text):
    lines = [ln for ln in (text or "").splitlines() if ln.strip().startswith(P.STEWARD_MARK)]
    if not lines:
        return {}
    try:
        doc = json.loads(lines[-1].strip()[len(P.STEWARD_MARK):].strip())
    except ValueError:
        return {}
    return {k: [str(x) for x in doc.get(k) or []] for k in ("applied", "proposed", "skipped")} \
        if isinstance(doc, dict) else {}
