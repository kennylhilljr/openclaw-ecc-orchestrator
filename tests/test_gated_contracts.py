"""Contract tests for every gated CLI command (`cli.commands.GATED`).

Two contracts are checked on the real executors, never on fabricated
envelopes:

* Envelopes: the dict returned by `Spec.execute`, before `run_gated` or the
  output formatting can normalize it, has exactly the seven envelope keys
  (`ok`, `operation`, `changed`, `checks`, `warnings`,
  `required_user_actions`, `rollback_checkpoint`) plus an optional `data`.
  A successful archiving cleanup and a successful merge return a non-null
  `rollback_checkpoint`.
* Authorizers: every plan marked destructive has a callable
  `Spec.authorize`, and each declared fixture variant has the expected
  `destructive` value.

Each fixture builds its own temporary repository, state, HOME and worktree
root. Coding runners are the fake runner script from `tests/_integration.py`
(`--runner-command-json` with `--allow-test-runner`); team reviewers answer
through TeamStep's `cli_override`/`http_post` seams. Fake `gh`, `claude` and
`codex` executables lead PATH and record any call, and socket connections are
refused, so no live network or real coding CLI runs.
"""

import contextlib
import importlib
import io
import json
import os
import socket
import stat
import time
import unittest
from unittest import mock

try:
    from . import test_cli_support as S
except ImportError:  # discovered as a top level module
    import test_cli_support as S

from openclaw_ecc_orchestrator.cli.commands import GATED
from openclaw_ecc_orchestrator.cli.config import load_config
from openclaw_ecc_orchestrator.cli.runtime import CommandFailed
from openclaw_ecc_orchestrator.plugin.approvals import plan_digest
from openclaw_ecc_orchestrator.runners.discovery import HttpResponse
from openclaw_ecc_orchestrator.runs.manager import RunManager
from openclaw_ecc_orchestrator.runs.store import RunStore
from openclaw_ecc_orchestrator.team import drive as TD
from openclaw_ecc_orchestrator.team import personas as TP
from openclaw_ecc_orchestrator.team import review as TR

# cli/__init__.py exports the main() function under the same name as the module.
M = importlib.import_module("openclaw_ecc_orchestrator.cli.main")

ENVELOPE_KEYS = frozenset({"ok", "operation", "changed", "checks", "warnings", "required_user_actions",
                           "rollback_checkpoint"})
OPTIONAL_KEYS = frozenset({"data"})

# Executors that cannot run in isolation without production changes:
# {GATED name: reason}. Skipping execution never removes planner coverage.
SKIPPED = {}

FAKE_TOOLS = ("gh", "claude", "codex")
FAKE_TOOL = """#!/bin/sh
echo "$0 $*" >> {log}
exit 97
"""

# Prompt markers of the team roles (team/review.py, team/drive.py prompts).
ROLE_MARKERS = (("Before any unit of this milestone runs", "plan_check"),
                ("The milestone's units are merged", "drift"),
                ("You are the delivery lead", "delivery_lead"),
                ("You are a product owner", "product_owner"),
                ("You are the QA verifier", "verifier"),
                ("You are the PR reviewer", "pr_reviewer"),
                ("You are the security reviewer", "security_reviewer"),
                ("You are the architect", "architect"),
                ("You are the designer", "designer"))

# (GATED name, variant) -> expected Plan.destructive. "refused" means the
# planner refuses the variant (CommandFailed): no valid plan can be built.
VARIANTS = {
    ("create-run", "new"): False,
    ("dispatch", "ready"): False,
    ("verify", "reviewing"): False,
    ("review", "reviewing"): False,
    ("team-review", "reviewing"): False,
    ("drive", "created"): False,
    ("merge", "reviewing"): True,
    ("merge", "queued_for_merge"): True,
    ("cleanup", "neither"): False,
    ("cleanup", "neither-archive"): False,
    ("cleanup", "bundle-archive"): True,
    ("cleanup", "ignored-archive"): True,
    ("cleanup", "both-archive"): True,
    ("cleanup", "bundle"): "refused",
    ("cleanup", "ignored"): "refused",
    ("cleanup", "both"): "refused",
    ("resume", "needs_user"): False,
    ("approvals.approve", "pending"): False,
    ("approvals.reject", "pending"): False,
}

# Variants the envelope test executes: every GATED name, both merge states and
# every archiving cleanup.
EXECUTED = [key for key, destructive in VARIANTS.items() if destructive != "refused"]


class GatedContractBase(S.CliTestBase):
    """CliTestBase plus fixture repositories with signing off, a guarded PATH,
    and direct access to plans, authorizers and executors."""

    def setUp(self):
        super().setUp()
        for key in ("commit.gpgsign", "tag.gpgsign"):
            S.git(self.repo, "config", key, "false")
        self.fake_bin = os.path.join(self.tmp, "fake-bin")
        self.tool_log = os.path.join(self.tmp, "fake-tools.log")
        os.makedirs(self.fake_bin)
        for name in FAKE_TOOLS:
            path = os.path.join(self.fake_bin, name)
            with open(path, "w") as fh:
                fh.write(FAKE_TOOL.format(log=self.tool_log))
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.http_calls = []

    def env(self):
        return {"HOME": self.home, "PATH": self.fake_bin + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin")}

    def fresh(self):
        """A new, empty fixture environment inside the same test method."""
        self.tearDown()
        self.setUp()

    @contextlib.contextmanager
    def guarded(self):
        """No live network, HOME and PATH pointing at the fixture, and a real
        TeamStep that answers through its cli_override/http_post seams."""
        real_team_step = TR.TeamStep
        test = self

        def team_step(*args, **kwargs):
            kwargs.setdefault("cli_override", test.cli_override)
            kwargs.setdefault("http_post", test.http_post)
            kwargs.setdefault("env", {})
            return real_team_step(*args, **kwargs)

        def refuse_connect(*args, **kwargs):
            raise AssertionError(f"network connection attempted: {args!r}")

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, self.env()))
            for key in ("GEMINI_API_KEY", "MOONSHOT_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY",
                        "ANTHROPIC_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"):
                os.environ.pop(key, None)  # restored when the patch.dict above exits
            stack.enter_context(mock.patch.object(socket.socket, "connect", refuse_connect))
            stack.enter_context(mock.patch.object(socket, "create_connection", refuse_connect))
            stack.enter_context(mock.patch.object(TR, "TeamStep", team_step))
            yield
        self.assertFalse(os.path.exists(self.tool_log), self.read_tool_log())
        self.assertEqual(self.http_calls, [])

    def read_tool_log(self):
        if not os.path.exists(self.tool_log):
            return ""
        with open(self.tool_log) as fh:
            return "real tool invoked: " + fh.read()

    # == fake team members ==
    def manager(self):
        return RunManager(RunStore(os.path.join(self.state, "runs")))

    def reviewing_head(self):
        run = self.manager().load(self.RUN_ID)
        for unit in run["units"].values():
            if unit["state"] == "reviewing":
                return unit["annotations"]["verification"]["head"]
        return S.git(self.repo, "rev-parse", "main")

    def role_of(self, prompt):
        for marker, role in ROLE_MARKERS:
            if marker in prompt:
                return role
        return "unknown"

    def reply(self, role):
        main_head = S.git(self.repo, "rev-parse", "main")
        if role == "product_owner":
            doc = {"verdict": "accept", "head": main_head,
                   "criteria": [{"criterion": "tests pass", "met": True, "evidence": "fixture"}]}
            return f"{TD.PRODUCT_MARK} {json.dumps(doc)}"
        if role == "plan_check":
            base = (self.manager().load(self.RUN_ID).get("metadata") or {}).get("base_commit") or main_head
            return f"{TP.REVIEW_MARK} {json.dumps({'verdict': 'approved', 'head': base, 'findings': []})}"
        if role in ("drift", "delivery_lead"):
            return f"{TP.REVIEW_MARK} {json.dumps({'verdict': 'approved', 'head': main_head, 'findings': []})}"
        head = self.reviewing_head()
        if role == "verifier":
            doc = {"verdict": "pass", "head": head, "findings": [],
                   "checks": [{"what": "cli", "how": "x", "expected": "y", "actual": "y", "ok": True}]}
            return f"{TP.VERIFY_MARK} {json.dumps(doc)}"
        return f"{TP.REVIEW_MARK} {json.dumps({'verdict': 'approved', 'head': head, 'findings': []})}"

    def cli_override(self, role, runner, prompt):
        return [S.PY, "-c", f"print({json.dumps(self.reply(self.role_of(prompt)))})"]

    def http_post(self, url, headers, body, timeout):
        self.http_calls.append(url)
        return HttpResponse(503, "{}")

    # == direct plan, authorize and execute ==
    def context(self, *cmd):
        argv = list(cmd) + ["--config", self.config_path, "--json"]
        args = M.build_parser().parse_args(argv)
        env = self.env()
        cfg = load_config(args, env, self.tmp)
        M._validate_gated_args(args, self.tmp)
        ctx = M.Ctx(args, cfg, stdin=io.StringIO(""), stdout=io.StringIO(), stderr=io.StringIO(), env=env,
                    is_tty=lambda: False, clock=time.time, cwd=self.tmp)
        return ctx, M.operation_of(args)

    def build(self, *cmd):
        """(ctx, spec, plan, digest) for `cmd`, built through Spec.plan."""
        ctx, op = self.context(*cmd)
        spec = GATED[op]
        plan = M.build_plan(ctx, spec)
        self.assertEqual(plan.operation, op)
        return ctx, spec, plan, plan_digest(plan.doc)

    def execute(self, ctx, spec, plan):
        run_id = spec.lease_run(plan)
        if run_id:
            lease = ctx.rt.manager.acquire_lease(run_id, ctx.cfg.conductor_id)
            self.assertTrue(lease["ok"], lease)
        return spec.execute(ctx, plan)

    def approve(self, request_id, verb="approve"):
        return self.ok(self.reviewed(*self.approve_args(request_id, verb=verb)))

    # == lifecycle helpers ==
    def stop(self, unit_id, reason="escalation_stopped"):
        mgr = self.manager()
        run = mgr.load(self.RUN_ID)
        res = mgr.transition(self.RUN_ID, run["lease"]["conductor_id"], unit_id, "needs_user", reason=reason)
        self.assertTrue(res.get("ok", True), res)

    def workspace(self, unit_id):
        return self.manager().load(self.RUN_ID)["units"][unit_id]["workspace"]

    def add_gitignore(self):
        with open(os.path.join(self.repo, ".gitignore"), "w") as fh:
            fh.write("*.local\n")
        S.git(self.repo, "add", ".gitignore")
        S.git(self.repo, "commit", "-q", "-m", "ignore local files")


class Fixtures(GatedContractBase):
    """Each fixture prepares real state and returns a description of the
    command: {"cmd": argv, "approve": callable or None, "after": callable or
    None}. `approve(ctx, spec, plan, digest)` obtains real broker approvals
    and returns the command to rebuild before authorizing."""

    def fx_create_run_new(self):
        return {"cmd": ("create-run", "--run-id", self.RUN_ID, "--plan", self.plan_path)}

    def fx_dispatch_ready(self):
        self.create_run()
        return {"cmd": self.dispatch_args("alpha", "alpha.txt=a")}

    def fx_verify_reviewing(self):
        self.create_run()
        self.ok(self.dispatch("alpha", "alpha.txt=a"))
        return {"cmd": ("verify", "--run-id", self.RUN_ID, "--unit", "alpha")}

    def fx_review_reviewing(self):
        self.create_run()
        self.ok(self.dispatch("alpha", "alpha.txt=a"))
        return {"cmd": self.review_args("alpha", self.unit("alpha")["verified_head"])}

    def add_team(self, team):
        """Commit a repository team.json. The CLI fixtures certify only codex and
        claude; a standard pr_reviewer keeps the review on the other provider."""
        os.makedirs(os.path.join(self.repo, ".orchestration"), exist_ok=True)
        with open(os.path.join(self.repo, ".orchestration", "team.json"), "w") as fh:
            json.dump(team, fh)
        S.git(self.repo, "add", ".orchestration/team.json")
        S.git(self.repo, "commit", "-q", "-m", "team")

    def fx_team_review_reviewing(self):
        self.add_team({"roles": {"pr_reviewer": {"class": "standard"}}})
        self.create_run()
        self.ok(self.dispatch("alpha", "alpha.txt=a"))
        return {"cmd": ("team-review", "--run-id", self.RUN_ID, "--unit", "alpha"), "check": self.check_team_review}

    def fx_drive_created(self):
        # One unit: drive passes the same test runner command to every unit.
        self.add_team({"merge_approval": "agents", "roles": {"pr_reviewer": {"class": "standard"}}})
        self.write_json(os.path.basename(self.plan_path), {"units": self.plan["units"][:1]})
        self.create_run()
        return {"cmd": ("drive", "--run-id", self.RUN_ID, "--max-hours", "1", "--allow-test-runner",
                        "--runner-command-json", self.runner_json("alpha.txt=a")),
                "check": self.check_drive}

    def _merge_ready(self):
        _head, request_id = self.to_review("alpha")
        self.approve(request_id)
        self.target_tip = S.git(self.repo, "rev-parse", "main")
        return ("merge", "--run-id", self.RUN_ID, "--unit", "alpha")

    def fx_merge_reviewing(self):
        return {"cmd": self._merge_ready(), "approve": None, "check": self.check_merge}

    def fx_merge_queued_for_merge(self):
        cmd = self._merge_ready()
        ctx, _op = self.context(*cmd)
        run = ctx.rt.manager.load(self.RUN_ID)
        self.assertTrue(ctx.rt.manager.acquire_lease(self.RUN_ID, ctx.cfg.conductor_id)["ok"])
        queued = ctx.rt.conductor(run.get("policy"), "merge").enqueue_merge(self.RUN_ID, "alpha")
        self.assertTrue(queued["ok"], queued)
        self.assertEqual(self.unit("alpha")["state"], "queued_for_merge")
        return {"cmd": cmd, "check": self.check_merge}

    def _stopped_alpha(self):
        self.add_gitignore()
        self.create_run()
        self.ok(self.dispatch("alpha", "alpha.txt=a"))
        self.stop("alpha")
        return self.workspace("alpha")["path"]

    def _cleanup(self, *, commits, ignored, archive):
        path = self._stopped_alpha()
        if not commits:
            base = (self.manager().load(self.RUN_ID).get("metadata") or {}).get("base_commit")
            S.git(path, "reset", "-q", "--hard", base)
        if ignored:
            with open(os.path.join(path, "notes.local"), "w") as fh:
                fh.write("uncommitted notes\n")
        cmd = ("cleanup", "--run-id", self.RUN_ID, "--unit", "alpha") + (("--archive",) if archive else ())
        return {"cmd": cmd, "approve": self.approve_cleanup if (commits or ignored) and archive else None,
                "check": self.check_cleanup}

    def fx_cleanup_neither(self):
        return self._cleanup(commits=False, ignored=False, archive=False)

    def fx_cleanup_neither_archive(self):
        return self._cleanup(commits=False, ignored=False, archive=True)

    def fx_cleanup_bundle_archive(self):
        return self._cleanup(commits=True, ignored=False, archive=True)

    def fx_cleanup_ignored_archive(self):
        return self._cleanup(commits=False, ignored=True, archive=True)

    def fx_cleanup_both_archive(self):
        return self._cleanup(commits=True, ignored=True, archive=True)

    def fx_cleanup_bundle(self):
        return self._cleanup(commits=True, ignored=False, archive=False)

    def fx_cleanup_ignored(self):
        return self._cleanup(commits=False, ignored=True, archive=False)

    def fx_cleanup_both(self):
        return self._cleanup(commits=True, ignored=True, archive=False)

    def fx_resume_needs_user(self):
        self.create_run()
        self.dispatch("alpha", "alpha.txt=a", mode="fail")
        self.stop("alpha")
        return {"cmd": ("resume", "--run-id", self.RUN_ID, "--unit", "alpha")}

    def fx_approvals_approve_pending(self):
        _head, request_id = self.to_review("alpha")
        return {"cmd": self.approve_args(request_id)}

    def fx_approvals_reject_pending(self):
        _head, request_id = self.to_review("alpha")
        return {"cmd": self.approve_args(request_id, verb="reject")}

    def fixture(self, name, variant):
        method = getattr(self, "fx_" + name.replace("-", "_").replace(".", "_") + "_"
                         + variant.replace("-", "_"))
        fx = method()
        fx.setdefault("approve", None)
        fx.setdefault("check", None)
        return fx

    # == destructive approvals ==
    def approve_cleanup(self, cmd):
        """Request the cleanup approval through the authorizer, approve it with
        a real broker decision, and return the command that consumes it."""
        ctx, spec, plan, digest = self.build(*cmd, "--session", S.SESSION)
        checks, refusal = spec.authorize(ctx, plan, digest)
        self.assertIsNotNone(refusal)
        request_id = refusal["data"]["approval_request"]["request_id"]
        self.approve(request_id)
        return tuple(cmd) + ("--session", S.SESSION, "--approval-request", request_id)

    # == checkpoint checks ==
    def check_merge(self, result):
        self.assertTrue(result["ok"], json.dumps(result, indent=1, default=str)[:3000])
        rb = result["rollback_checkpoint"]
        self.assertIsNotNone(rb)
        self.assertEqual(rb["previous_commit"], self.target_tip)
        self.assertEqual(self.unit("alpha")["state"], "merged")

    def check_drive(self, result):
        self.assertTrue(result["ok"], json.dumps(result, indent=1, default=str)[:3000])
        self.assertEqual(self.unit("alpha")["state"], "merged")

    def check_team_review(self, result):
        self.assertTrue(result["ok"], json.dumps(result, indent=1, default=str)[:3000])

    def check_cleanup(self, result):
        self.assertTrue(result["ok"], json.dumps(result, indent=1, default=str)[:3000])
        data = result.get("data") or {}
        if not (data.get("bundle") or data.get("ignored_archive")):
            return
        rb = result["rollback_checkpoint"]
        self.assertIsNotNone(rb, "an archiving cleanup returned no rollback checkpoint")
        if data.get("bundle"):
            self.assertTrue(os.path.isfile(rb["bundle"]), rb)
        if data.get("ignored_archive"):
            self.assertTrue(os.path.isfile(rb["ignored_archive"]), rb)


class GatedEnvelopeContractTest(Fixtures):
    def assert_envelope(self, result, op):
        self.assertIsInstance(result, dict)
        keys = set(result)
        self.assertEqual(ENVELOPE_KEYS - keys, set(), f"{op}: missing envelope keys")
        self.assertEqual(keys - ENVELOPE_KEYS - OPTIONAL_KEYS, set(), f"{op}: unexpected envelope keys")
        self.assertIsInstance(result["checks"], list, op)
        self.assertIsInstance(result["warnings"], list, op)
        self.assertIsInstance(result["required_user_actions"], list, op)
        self.assertIsInstance(result["ok"], bool, op)
        self.assertIsInstance(result["changed"], bool, op)

    def test_skips_are_explicit_and_coverage_is_exact(self):
        self.assertLessEqual(set(SKIPPED), set(GATED))
        for name, reason in SKIPPED.items():
            self.assertIsInstance(reason, str)
            self.assertTrue(reason.strip(), name)
        executed = {name for name, _ in EXECUTED}
        self.assertEqual(executed & set(SKIPPED), set())
        self.assertEqual(executed | set(SKIPPED), set(GATED))

    def test_every_gated_executor_returns_a_well_formed_envelope(self):
        exercised = set()
        for name, variant in EXECUTED:
            if name in SKIPPED:
                continue
            with self.subTest(command=name, variant=variant):
                self.fresh()
                with self.guarded():
                    fx = self.fixture(name, variant)
                    cmd = fx["cmd"]
                    ctx, spec, plan, digest = self.build(*cmd)
                    if plan.destructive:
                        self.assertTrue(callable(spec.authorize), name)
                        if fx["approve"]:
                            cmd = fx["approve"](cmd)
                            ctx, spec, plan, digest = self.build(*cmd)
                        checks, refusal = spec.authorize(ctx, plan, digest)
                        self.assertIsNone(refusal, json.dumps(refusal, default=str)[:3000])
                        self.assertTrue(checks and all(c["ok"] for c in checks), checks)
                    result = self.execute(ctx, spec, plan)
                    self.assert_envelope(result, name)
                    if fx["check"]:
                        fx["check"](result)
                exercised.add((name, variant))
        self.assertEqual({name for name, _ in exercised} | set(SKIPPED), set(GATED))


class GatedAuthorizerContractTest(Fixtures):
    def test_variant_registry_covers_gated_exactly(self):
        self.assertEqual({name for name, _ in VARIANTS}, set(GATED))

    def test_destructive_plans_have_callable_authorizers(self):
        exercised = set()
        for (name, variant), expected in VARIANTS.items():
            with self.subTest(command=name, variant=variant):
                self.fresh()
                with self.guarded():
                    fx = self.fixture(name, variant)
                    if expected == "refused":
                        with self.assertRaises(CommandFailed):
                            self.build(*fx["cmd"])
                    else:
                        _ctx, spec, plan, _digest = self.build(*fx["cmd"])
                        self.assertEqual(plan.destructive, expected, f"{name}/{variant} destructive")
                        self.assertIs(plan.doc["destructive"], expected)
                        if plan.destructive:
                            self.assertTrue(callable(spec.authorize), f"{name} has no authorizer")
                exercised.add((name, variant))
        self.assertEqual(exercised, set(VARIANTS))
        for name, spec in GATED.items():
            if spec.authorize is not None:
                self.assertTrue(callable(spec.authorize), name)


if __name__ == "__main__":
    unittest.main()
