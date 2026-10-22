"""Per-unit team step (TEAM-DELIVERY-v2 section 4.1, steps 4 to 6): reviewers,
the QA verifier, then the agent merge. Runs on a unit in `reviewing`."""

import contextlib
import fnmatch
import os
import threading

from ..merge_queue.conflicts import diff_stats
from ..routing.selection import required_review, select_runner
from ..runners.registry import PROFILES, model_family
from ..runs import repair as R
from ..runs import state as S
from ..runs.envelope import check, envelope
from ..runs.store import iso
from ..worktrees.git import git_out, run_git
from . import config as TC
from . import evidence as E
from . import invoke as I
from . import learning as L
from . import personas as P
from .rotation import Rotation

SECURITY_SIGNALS = ("security", "auth", "secret", "credential", "crypto", "migration")


def matches(path, patterns):
    return any(fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(path, pat.rstrip("*").rstrip("/") + "/*")
               for pat in patterns or [])


class TeamStep:
    """`adapters` maps runner name to adapter (default: the registry);
    `cli_override(role, runner, prompt) -> argv` and `http_post` are test seams."""

    def __init__(self, conductor, team_cfg, *, state_dir, worktree_root, logs_dir, supervisor, clock,
                 adapters=None, cli_override=None, http_post=None, env=None):
        self.c = conductor
        self.cfg = team_cfg
        self.state_dir = state_dir
        self.worktree_root = worktree_root
        self.logs_dir = logs_dir
        self.supervisor = supervisor
        self.clock = clock
        self._adapters = adapters
        self.cli_override = cli_override
        self.http_post = http_post
        self.env = env

    # == helpers ==
    def adapter(self, name):
        if self._adapters is not None:
            return self._adapters[name]
        from ..runners.probes import get_adapter
        return get_adapter(name)

    def rotation(self, run_id):
        return Rotation(os.path.join(self.state_dir, "team", f"{run_id}.json"))

    def candidates(self, tier, risk, author_runner, author_family, strict):
        sel = select_runner(tier, self.c.certified_runners or [], self.c.catalog, self.c.policy, role="review",
                            risk=risk, breaker=self.c.breaker, now=self.clock(), large_context=True,
                            exclude_runners=[author_runner] if author_runner else [],
                            exclude_families=[author_family] if strict and author_family else [])
        return sel.candidates

    @contextlib.contextmanager
    def _lease_kept(self, run_id):
        """Keep the run lease alive while a persona turn runs. A review turn can
        outlast the lease (kimi-k3 took 5.5 minutes on M0-01's PR review); the
        lease then expired and the verdict could not be saved (live finding,
        rmcp-m0f). Renewed every third of the lease, and once more at the end
        so the caller can record the result."""
        keep = getattr(self.c, "_keep_lease", None)
        if keep is None:
            yield
            return
        period = max(1.0, float(getattr(self.c.manager, "lease_ttl", 300.0)) / 3.0)
        stop = threading.Event()

        def beat():
            while not stop.wait(period):
                try:
                    keep(run_id, force=True)
                except Exception:  # noqa: BLE001 - a missed beat is retried on the next one
                    pass

        thread = threading.Thread(target=beat, name=f"lease-{run_id}", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=10)
            keep(run_id, force=True)

    def _turn(self, role, choice, prompt, cwd, writable, run_id, unit_id, timeout):
        with self._lease_kept(run_id):
            return self._turn_unkept(role, choice, prompt, cwd, writable, run_id, unit_id, timeout)

    def _turn_unkept(self, role, choice, prompt, cwd, writable, run_id, unit_id, timeout):
        name = P.persona_for(self.cfg, role)
        if name:
            prompt = P.with_persona(prompt, name, P.load_persona(self.c.worktrees.repo, name))
        runner, model = choice["runner"], choice["model"]
        adapter = self.adapter(runner)
        stamp = int(self.clock() * 1000)
        if PROFILES[runner].kind == "coding_cli":
            override = (lambda p: self.cli_override(role, runner, p)) if self.cli_override else None
            return I.run_cli(adapter, model, prompt, cwd=cwd, writable=writable, supervisor=self.supervisor,
                             log_path=os.path.join(self.logs_dir, run_id, f"{unit_id}-{role}-{runner}-{stamp}.log"),
                             timeout=timeout, argv_override=override)
        return I.run_api(adapter, model, prompt, timeout=min(timeout, 900.0), env=self.env, http_post=self.http_post,
                         log_path=os.path.join(self.logs_dir, run_id, f"{unit_id}-{role}-{runner}-{stamp}.log"))

    def _ask(self, role, mark, prompt, choice, head, cwd, writable, run_id, unit_id, timeout):
        """One turn plus one re-ask on an unparsable reply. Returns
        (verdict or None, problem, turn)."""
        turn = self._turn(role, choice, prompt, cwd, writable, run_id, unit_id, timeout)
        if turn.get("error"):
            return None, f"runner error: {turn['error']}", turn
        verdict, problem = P.parse_verdict(turn.get("text"), mark, head)
        if verdict is None:
            again = (f"Your previous reply could not be used: {problem}. Reply again; the last line must be the "
                     f"{mark} line exactly as specified.\n\n" + prompt)
            turn = self._turn(role, choice, again, cwd, writable, run_id, unit_id, timeout)
            if turn.get("error"):
                return None, f"runner error: {turn['error']}", turn
            verdict, problem = P.parse_verdict(turn.get("text"), mark, head)
        return verdict, problem, turn

    def _play(self, role, mark, prompt_fn, *, run_id, unit_id, rotation, panel, candidates, exclude, pack_tokens,
              head, cwd_fn, writable, timeout, require_kind=None):
        """Rotate through eligible providers until one returns a usable verdict
        (a provider that errors is skipped; an unparsable verdict after the
        re-ask counts as changes requested, per section 6.3)."""
        spec = self.cfg["roles"][role]
        tried, skipped_all = set(), []
        kinds = {name: PROFILES[name].kind for name in PROFILES}
        for _ in range(max(1, len(panel))):
            pool = [c for c in candidates if c["runner"] not in tried]
            choice, skipped = rotation.choose(role, panel, pool, exclude=exclude, pack_tokens=pack_tokens,
                                              max_pack=self.cfg.get("max_pack_tokens"),
                                              cost_cap=spec.get("cost_cap_usd_per_turn"),
                                              share_cap=self.cfg.get("share_cap"), require_kind=require_kind,
                                              kinds=kinds)
            skipped_all += [{"provider": p, "reason": r} for p, r in skipped]
            if choice is None:
                break
            tried.add(choice["runner"])
            cwd, cleanup = cwd_fn()
            try:
                verdict, problem, turn = self._ask(role, mark, prompt_fn(), choice, head, cwd, writable, run_id,
                                                   unit_id, timeout)
            finally:
                cleanup()
            record = {"role": role, "provider": choice["runner"], "model": turn.get("model") or choice["model"],
                      "cost_usd": turn.get("cost_usd"), "skipped": skipped_all, "at": iso(self.clock())}
            if verdict is not None:
                record.update(verdict=verdict["verdict"], findings=verdict.get("findings"),
                              lessons=verdict.get("lessons"),
                              checks=verdict.get("checks"), downgraded=verdict.get("downgraded"))
                return record
            if problem.startswith("runner error"):
                skipped_all.append({"provider": choice["runner"], "reason": problem})
                continue
            record.update(verdict="changes_requested" if mark == P.REVIEW_MARK else "fail",
                          findings=[{"severity": "blocker", "what": f"reviewer output unusable: {problem}"}])
            return record
        return {"role": role, "provider": None, "verdict": None, "skipped": skipped_all,
                "problem": "no eligible provider produced a verdict"}

    def _play_preferring_other_providers(self, role, mark, prompt_fn, *, avoid, strict_candidates,
                                         open_candidates, **kw):
        """Independence is the persona and the fresh session, not the provider
        (owner decision, 2026-10-19). Other providers are tried first for
        diversity; if none of them produces a verdict, any provider eligible for
        the role's class plays the persona in its own session. The record says
        which kind of independence the verdict has."""
        res = self._play(role, mark, prompt_fn, candidates=strict_candidates, exclude=set(avoid), **kw)
        if res.get("verdict") is not None:
            res["independence"] = "provider"
            return res
        first = res.get("skipped") or []
        res = self._play(role, mark, prompt_fn, candidates=open_candidates, exclude=set(), **kw)
        res["skipped"] = first + (res.get("skipped") or [])
        if res.get("verdict") is not None:
            res["independence"] = "persona"
        return res

    # == the step ==
    def run(self, run_id, unit_id):
        op = "team.review"
        c = self.c
        run = c.manager.load(run_id)
        unit = run["units"][unit_id]
        if unit["state"] != S.REVIEWING:
            return envelope(op, ok=False, checks=[check("unit_reviewing", False, unit["state"])])
        ws = unit["workspace"]
        verification = (unit.get("annotations") or {}).get("verification") or {}
        head = verification.get("head")
        base = verification.get("base_commit") or ws["base_commit"]
        punit = c.manager.plan_unit(run, unit_id)
        repo = c.worktrees.repo
        stats = diff_stats(repo, head, base)
        classification = c._classify(run, unit_id, stats)
        routing = unit.get("routing") or {}
        author = routing.get("runner")
        author_family = routing.get("family") or (model_family(author, routing.get("model")) if author else None)
        tier, strict = required_review(routing, classification)
        pack, tokens = E.unit_pack(repo, ws["path"], base, head, punit, verification)
        rotation = self.rotation(run_id)
        record = {"head": head, "reviews": [], "verification": None, "merge": None}

        def save():
            c.manager.annotate_unit(run_id, c.id, unit_id, "team", record)

        roles = ["pr_reviewer"]
        files = stats.get("files") or []
        high = (classification.risk == "high"
                or any(matches(f, (c.policy or {}).get("high_risk_paths")) for f in files)
                or any(any(sig in s for sig in SECURITY_SIGNALS) for s in
                       [str(x.get("name", x)) for x in getattr(classification, "signals", [])]))
        if high:
            roles.append("security_reviewer")
        if any(matches(f, self.cfg["roles"]["architect"].get("contract_paths")) for f in files):
            roles.append("architect")
        if any(matches(f, self.cfg["roles"]["designer"].get("when_paths")) for f in files):
            roles.append("designer")

        same_dir = lambda: (ws["path"], lambda: None)  # noqa: E731
        pr_provider = None
        for role in roles:
            cls = TC.role_class(self.cfg, role, tier)
            role_tier = max(TC.CLASS_TIER[cls], tier if role == "pr_reviewer" else 0)
            cls = {0: "economical", 1: "standard", 2: "advanced"}[role_tier]
            avoid = {author} if author else set()
            if role == "security_reviewer" and pr_provider:
                avoid.add(pr_provider)
            res = self._play_preferring_other_providers(
                role, P.REVIEW_MARK, lambda r=role: P.review_prompt(r, pack, head), avoid=avoid,
                strict_candidates=self.candidates(role_tier, classification.risk, author, author_family, strict),
                open_candidates=self.candidates(role_tier, classification.risk, None, None, False),
                run_id=run_id, unit_id=unit_id, rotation=rotation, panel=TC.panel_for(self.cfg, role, cls),
                pack_tokens=tokens, head=head, cwd_fn=same_dir, writable=False, timeout=1800.0)
            res["tier"] = role_tier
            record["reviews"].append(res)
            L.capture_verdict(L.Lessons(self.state_dir, run_id), unit_id, res, at=iso(self.clock()))
            save()
            if role == "pr_reviewer":
                pr_provider = res.get("provider")
                pr = res
            outcome = self._handle_review(run_id, unit_id, res, op)
            if outcome is not None:
                return outcome

        vspec = self.cfg["roles"]["verifier"]
        vtier = TC.CLASS_TIER[TC.role_class(self.cfg, "verifier")]
        vres = self._play_preferring_other_providers(
            "verifier", P.VERIFY_MARK, lambda: P.verify_prompt(pack, head), avoid={author} if author else set(),
            strict_candidates=self.candidates(vtier, classification.risk, author, author_family, False),
            open_candidates=self.candidates(vtier, classification.risk, None, None, False),
            run_id=run_id, unit_id=unit_id, rotation=rotation, panel=TC.panel_for(self.cfg, "verifier", None),
            pack_tokens=0, head=head, cwd_fn=lambda: self._scratch(repo, head, run_id, unit_id), writable=True,
            timeout=float(vspec.get("timeout_minutes", 30)) * 60, require_kind="coding_cli")
        record["verification"] = vres
        L.capture_verdict(L.Lessons(self.state_dir, run_id), unit_id, vres, at=iso(self.clock()))
        save()
        if vres.get("verdict") is None:
            return c._needs_user(run_id, unit_id, "no_qualified_verifier",
                                 vres.get("problem", "") + ": " + str(vres.get("skipped")), op)
        if vres["verdict"] == "fail":
            ev = R.failure_evidence("verification_failed", verification={
                "verdict": "fail", "checks": vres.get("checks"), "findings": vres.get("findings")})
            return self._returned(c.return_for_changes(run_id, unit_id, "verification_failed", ev), record)
        if vres["verdict"] == "inconclusive":
            # The verifier's environment stopped some checks and none it ran
            # failed. Sending the unit back to the coder cannot fix that (live
            # finding, rm-m0b M0-02: nine attempts lost to a sandbox with no
            # network); ECC's own gates on this head decide instead.
            blocked = "; ".join(f"{ch.get('what')}: {ch.get('blocked')}" for ch in vres.get("checks") or []
                                if ch.get("blocked"))
            if not (verification.get("passed") is True and verification.get("head") == head):
                return c._needs_user(run_id, unit_id, "verifier_inconclusive",
                                     f"verifier {vres.get('provider')} could not run: {blocked}", op)
            vres["accepted"] = f"inconclusive verifier ({blocked}); ECC gates passed on {head[:12]}"
            save()
        return self._approve_and_merge(run_id, unit_id, pr, vres, head, record, op)

    def _handle_review(self, run_id, unit_id, res, op):
        c = self.c
        verdict = res.get("verdict")
        if verdict is None:
            return c._needs_user(run_id, unit_id, "no_qualified_reviewer",
                                 f"{res['role']}: {res.get('problem')}; skipped {res.get('skipped')}", op)
        if verdict == "rejected":
            return c._needs_user(run_id, unit_id, f"review_rejected_by_{res['role']}",
                                 "; ".join(str(f.get("what")) for f in res.get("findings") or []), op)
        if verdict == "changes_requested":
            ev = R.failure_evidence(f"{res['role']}_changes_requested", review={
                "role": res["role"], "verdict": verdict, "findings": res.get("findings")})
            return self._returned(c.return_for_changes(run_id, unit_id, f"{res['role']}_changes_requested", ev),
                                  None)
        return None

    def _returned(self, res, record):
        res["operation"] = "team.review"
        if record is not None:
            res.setdefault("data", {})["team"] = record
        return res

    def _scratch(self, repo, head, run_id, unit_id):
        path = os.path.join(self.worktree_root, "_verify", f"{run_id}-{unit_id}-{int(self.clock() * 1000)}")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        run_git(["worktree", "add", "-q", "--detach", path, head], repo)

        def cleanup():
            run_git(["worktree", "remove", "--force", path], repo, check=False)
        return path, cleanup

    def _approve_and_merge(self, run_id, unit_id, pr, vres, head, record, op):
        c = self.c
        notes = "; ".join(f"{r['role']}={r.get('provider')}:{r.get('verdict')}" for r in record["reviews"])
        notes += f"; verifier={vres.get('provider')}:{vres.get('verdict')}"
        session = f"team-{run_id}"
        res = c.record_review(run_id, unit_id, {
            "reviewer": f"team-{pr['provider']}", "runner": pr["provider"], "model": pr.get("model"),
            "tier": pr.get("tier"), "verdict": "approved", "head": head, "notes": notes}, session_id=session)
        if not res["ok"]:
            res["operation"] = op
            return res
        request = (res.get("data") or {}).get("approval_request") or {}
        if self.cfg.get("merge_approval") != "agents":
            res["operation"] = op
            res.setdefault("data", {})["team"] = record
            return res
        stored = c.broker.get(request["request_id"])
        decision = {k: stored[k] for k in ("request_id", "run_id", "unit_id", "action", "plan_sha256", "session_id")}
        decision.update(decision="approved",
                        decided_by=f"agents:{'+'.join(r['provider'] for r in record['reviews'])}+{vres['provider']}")
        resolved = c.broker.resolve(decision)
        if not resolved["ok"]:
            resolved["operation"] = op
            return resolved
        queued = c.enqueue_merge(run_id, unit_id)
        if not queued["ok"]:
            queued["operation"] = op
            return queued
        result = None
        for _ in range(50):
            processed = c.process_merge_queue()
            data = processed.get("data") or {}
            if data.get("status") in (None, "empty"):
                break
            if data.get("run_id") == run_id and data.get("unit_id") == unit_id:
                result = (processed, data)
                break
        record["merge"] = {k: (result[1] if result else {}).get(k)
                           for k in ("status", "merged_commit", "conflicts", "detail")}
        c.manager.annotate_unit(run_id, c.id, unit_id, "team", record)
        if result is None:
            return envelope(op, ok=False, changed=True, checks=[check("merged", False, "not processed")],
                            data={"team": record})
        processed, data = result
        status = data.get("status")
        if status == "merged":
            return envelope(op, changed=True, checks=[check("merged", True, data.get("merged_commit"))],
                            data={"team": record})
        if status in ("conflict", "gate_failed"):
            ev = R.failure_evidence(f"merge_{status}", files=data.get("conflicts"),
                                    gates=(data.get("verification") or {}).get("gates")
                                    or data.get("gates") or [])
            return self._returned(c.return_for_changes(run_id, unit_id, f"merge_{status}", ev), record)
        return envelope(op, ok=False, changed=True, checks=[check("merged", False, f"{status}: {data.get('detail')}")],
                        required_user_actions=processed.get("required_user_actions") or [], data={"team": record})
