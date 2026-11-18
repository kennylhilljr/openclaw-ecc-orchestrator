"""The shared skills submodule in orchestrator worktrees (skills-catalog SPEC
SUB-5, SUB-6 to SUB-8 as applied at worktree creation, SUB-10, SUB-15).

Real git in temp folders: a bare fake catalog, and a consumer that carries
it as a submodule at `.agents/skills/shared`. Only these tests allow
`file://` (through the module's test seam); one test pins the production
command line to `protocol.file.allow=never`."""

import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

try:
    from ._integration import PY, IntegrationBase, git
    from ._units import work_unit
    from .test_merge_queue import Base as QueueBase
    from .test_merge_queue import unit as queue_unit
    from .test_team_review import TeamBase
except ImportError:  # discovered as a top level module
    from _integration import PY, IntegrationBase, git
    from _units import work_unit
    from test_merge_queue import Base as QueueBase
    from test_merge_queue import unit as queue_unit
    from test_team_review import TeamBase

from openclaw_ecc_orchestrator.cli import preflight as PF
from openclaw_ecc_orchestrator.gates.runner import GIT_REFUSED_SUBCOMMANDS, check_command_policy, run_gates
from openclaw_ecc_orchestrator.merge_queue import queue as Q
from openclaw_ecc_orchestrator.process.supervisor import Supervisor
from openclaw_ecc_orchestrator.runners.cli_adapters import ClaudeAdapter
from openclaw_ecc_orchestrator.worktrees import git as G
from openclaw_ecc_orchestrator.worktrees import shared_skills as SS

CATALOG_JSON = os.path.join(SS.SHARED_PATH, "catalog.json")


class CatalogMixin:
    """Builds the fake catalog and wires a consumer repository to it."""

    def make_catalog(self, name="catalog", with_catalog_json=True):
        src = os.path.join(self.tmp, f"{name}-src")
        os.makedirs(os.path.join(src, "skills", "demo"))
        git(src, "init", "-q", "-b", "main")
        git(src, "config", "user.email", "test@example.invalid")
        git(src, "config", "user.name", "Test")
        with open(os.path.join(src, "skills", "demo", "SKILL.md"), "w") as fh:
            fh.write("---\nname: demo\ndescription: Use when testing.\n---\n")
        if with_catalog_json:
            with open(os.path.join(src, "catalog.json"), "w") as fh:
                json.dump({"skills": ["demo"]}, fh)
        git(src, "add", "-A")
        git(src, "commit", "-q", "-m", "catalog")
        bare = os.path.join(self.tmp, f"{name}.git")
        git(self.tmp, "clone", "-q", "--bare", src, bare)
        return "file://" + bare

    def add_submodule(self, repo, url):
        git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", url, SS.SHARED_PATH)
        git(repo, "commit", "-q", "-m", "add shared skills")
        return git(repo, "rev-parse", f"HEAD:{SS.SHARED_PATH}")

    def allow(self, url):
        """The test seam: accept this file:// catalog instead of the GitHub URL."""
        for attr, value in (("ALLOWED_URL", url), ("ALLOW_FILE_PROTOCOL", True)):
            patcher = mock.patch.object(SS, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def consumer(self, repo, with_catalog_json=True):
        url = self.make_catalog(with_catalog_json=with_catalog_json)
        self.allow(url)
        return self.add_submodule(repo, url)

    def worktree(self, repo, name):
        path = os.path.join(self.tmp, "wts", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        git(repo, "worktree", "add", "-q", "--detach", path, "HEAD")
        return path


class ModuleTests(CatalogMixin, IntegrationBase):
    def test_init_and_check_return_the_pinned_commit(self):
        pinned = self.consumer(self.repo)
        wt = self.worktree(self.repo, "a")
        self.assertFalse(os.path.exists(os.path.join(wt, CATALOG_JSON)), "worktree add leaves it empty")
        self.assertEqual(SS.init_shared_skills(wt), pinned)
        self.assertTrue(os.path.isfile(os.path.join(wt, CATALOG_JSON)))
        self.assertEqual(SS.check_shared_skills(wt), pinned)

    def test_repo_without_the_entry_is_untouched(self):
        wt = self.worktree(self.repo, "plain")
        with mock.patch.object(SS, "run_git", wraps=SS.run_git) as spy:
            self.assertIsNone(SS.init_shared_skills(wt))
            self.assertIsNone(SS.check_shared_skills(wt))
            self.assertIsNone(SS.dirty_shared_skills(wt))
        self.assertEqual(spy.call_count, 0)
        self.assertEqual(git(wt, "status", "--porcelain"), "")
        self.assertFalse(os.path.exists(os.path.join(wt, ".agents")))

    def test_other_url_fails_submodule_url_not_allowed(self):
        self.consumer(self.repo)
        other = self.make_catalog(name="other")
        git(self.repo, "config", "--file", ".gitmodules", f"submodule.{SS.SHARED_PATH}.url", other)
        git(self.repo, "commit", "-q", "-am", "repoint")
        wt = self.worktree(self.repo, "b")
        with self.assertRaises(SS.SharedSkillsError) as ctx:
            SS.init_shared_skills(wt)
        self.assertEqual(ctx.exception.reason, "submodule URL not allowed")
        self.assertFalse(os.path.exists(os.path.join(wt, CATALOG_JSON)), "nothing was fetched")

    def test_duplicate_entries_for_the_path_fail_submodule_url_not_allowed(self):
        """A second entry mapping the same path cannot slip past the URL check."""
        self.consumer(self.repo)
        other = self.make_catalog(name="other")
        with open(os.path.join(self.repo, ".gitmodules"), "a") as fh:
            fh.write(f'[submodule "evil"]\n\tpath = {SS.SHARED_PATH}\n\turl = {other}\n')
        git(self.repo, "commit", "-q", "-am", "second entry")
        wt = self.worktree(self.repo, "dup")
        with self.assertRaises(SS.SharedSkillsError) as ctx:
            SS.init_shared_skills(wt)
        self.assertEqual(ctx.exception.reason, "submodule URL not allowed")

    def test_unreadable_gitmodules_naming_the_path_fails_closed(self):
        with open(os.path.join(self.repo, ".gitmodules"), "w") as fh:
            fh.write(f'[submodule "x"\n\tpath = {SS.SHARED_PATH}\n')
        git(self.repo, "add", ".gitmodules")
        git(self.repo, "commit", "-q", "-m", "broken")
        wt = self.worktree(self.repo, "broken")
        for fn in (SS.init_shared_skills, SS.check_shared_skills):
            with self.assertRaises(SS.SharedSkillsError) as ctx:
                fn(wt)
            self.assertEqual(ctx.exception.reason, "shared skills missing")

    def test_fetch_timeout_fails_shared_skills_missing(self):
        self.consumer(self.repo)
        wt = self.worktree(self.repo, "slow")

        def fake(args, cwd, **kw):
            if "update" in args:
                raise subprocess.TimeoutExpired(args, kw.get("timeout"))
            return G.run_git(args, cwd, **kw)

        with mock.patch.object(SS, "run_git", fake):
            with self.assertRaises(SS.SharedSkillsError) as ctx:
                SS.init_shared_skills(wt)
        self.assertEqual(ctx.exception.reason, "shared skills missing")
        self.assertIn("timed out", ctx.exception.detail)

    def test_missing_catalog_json_fails_shared_skills_missing(self):
        self.consumer(self.repo, with_catalog_json=False)
        wt = self.worktree(self.repo, "c")
        with self.assertRaises(SS.SharedSkillsError) as ctx:
            SS.init_shared_skills(wt)
        self.assertEqual(ctx.exception.reason, "shared skills missing")

    def test_wrong_commit_fails_shared_skills_missing(self):
        self.consumer(self.repo)
        wt = self.worktree(self.repo, "d")
        SS.init_shared_skills(wt)
        sub = os.path.join(wt, SS.SHARED_PATH)
        git(sub, "-c", "user.email=t@example.invalid", "-c", "user.name=T", "commit", "-q", "--allow-empty",
            "-m", "moved")
        self.assertTrue(git(wt, "submodule", "status").startswith("+"))
        with self.assertRaises(SS.SharedSkillsError) as ctx:
            SS.check_shared_skills(wt)
        self.assertEqual(ctx.exception.reason, "shared skills missing")

    def test_uninitialized_fails_shared_skills_missing(self):
        self.consumer(self.repo)
        wt = self.worktree(self.repo, "e")
        with self.assertRaises(SS.SharedSkillsError) as ctx:
            SS.check_shared_skills(wt)
        self.assertEqual(ctx.exception.reason, "shared skills missing")

    def test_production_argv_refuses_file_and_pins_the_catalog_url(self):
        """No seam: the fetch runs https only, with the GitHub catalog URL."""
        with open(os.path.join(self.repo, ".gitmodules"), "w") as fh:
            fh.write(f'[submodule "{SS.SHARED_PATH}"]\n\tpath = {SS.SHARED_PATH}\n\turl = {SS.CATALOG_URL}\n')
        git(self.repo, "add", ".gitmodules")
        git(self.repo, "commit", "-q", "-m", "gitmodules")
        wt = self.worktree(self.repo, "f")
        self.assertEqual(SS.ALLOWED_URL, SS.CATALOG_URL)
        self.assertFalse(SS.ALLOW_FILE_PROTOCOL)
        calls = []

        def fake(args, cwd, **kw):
            if "submodule" in args and "update" in args:
                calls.append(["git"] + G.safety_args() + list(args))
                return subprocess.CompletedProcess(args, 1, "", "refused")
            return G.run_git(args, cwd, **kw)

        with mock.patch.object(SS, "run_git", fake):
            with self.assertRaises(SS.SharedSkillsError) as ctx:
                SS.init_shared_skills(wt)
        self.assertEqual(ctx.exception.reason, "shared skills missing")
        self.assertEqual(len(calls), 2, "a plain fetch, then one with an empty credential helper")
        for argv in calls:
            pairs = {argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-c"}
            for want in ("protocol.file.allow=never", "protocol.ext.allow=never", "protocol.allow=never",
                         "protocol.https.allow=always", "submodule.recurse=false",
                         f"submodule.{SS.SHARED_PATH}.url={SS.CATALOG_URL}"):
                self.assertIn(want, pairs)
            self.assertEqual(argv[-7:], ["submodule", "update", "--init", "--depth", "1", "--", SS.SHARED_PATH])
        self.assertIn("credential.helper=", calls[1])
        self.assertIn("http.extraHeader=", calls[1])
        for scope in ("https://github.com", SS.CATALOG_URL):
            self.assertIn(f"credential.{scope}.helper=", calls[1])
            self.assertIn(f"http.{scope}.extraHeader=", calls[1])
        self.assertNotIn("credential.helper=", calls[0][len(G.safety_args()) + 1:])


class DirtyCheckTests(CatalogMixin, IntegrationBase):
    def test_edit_inside_an_initialized_submodule_is_dirty(self):
        self.consumer(self.repo)
        wt = self.worktree(self.repo, "g")
        SS.init_shared_skills(wt)
        self.assertIsNone(SS.dirty_shared_skills(wt))
        with open(os.path.join(wt, SS.SHARED_PATH, "skills", "demo", "SKILL.md"), "a") as fh:
            fh.write("agent edit\n")
        reason, detail = SS.dirty_shared_skills(wt)
        self.assertEqual(reason, "edit inside shared skills")
        self.assertIn("SKILL.md", detail)

    def test_moved_and_committed_gitlink_is_dirty_against_the_reference(self):
        """Committing inside the submodule and then the matching parent gitlink is still caught."""
        pinned = self.consumer(self.repo)
        wt = self.worktree(self.repo, "moved")
        base = git(wt, "rev-parse", "HEAD")
        SS.init_shared_skills(wt)
        sub = os.path.join(wt, SS.SHARED_PATH)
        ident = ["-c", "user.email=t@example.invalid", "-c", "user.name=T"]
        git(sub, *ident, "commit", "-q", "--allow-empty", "-m", "moved")
        git(wt, "add", SS.SHARED_PATH)
        git(wt, *ident, "commit", "-q", "-m", "bump")
        status = subprocess.run(["git", "submodule", "status"], cwd=wt, capture_output=True, text=True).stdout
        self.assertTrue(status.startswith(" "), "the index matches the checkout")
        reason, detail = SS.dirty_shared_skills(wt, base)
        self.assertEqual(reason, "edit inside shared skills")
        self.assertIn(pinned[:12], detail)

    def test_broken_gitmodules_after_a_unit_is_dirty_not_an_exception(self):
        self.consumer(self.repo)
        wt = self.worktree(self.repo, "broke")
        base = git(wt, "rev-parse", "HEAD")
        with open(os.path.join(wt, ".gitmodules"), "w") as fh:
            fh.write(f'[submodule "x"\n\tpath = {SS.SHARED_PATH}\n')
        self.assertEqual(SS.dirty_shared_skills(wt)[0], "edit inside shared skills")
        with open(os.path.join(wt, ".gitmodules"), "w") as fh:
            fh.write("")
        self.assertIsNone(SS.dirty_shared_skills(wt, base), "uninitialized and untouched is clean")

    def _unit_run(self, argv):
        self.consumer(self.repo)
        c, m, _ = self.conductor("c1")
        u = work_unit("u1", files=["a.txt"], commands=[f"{PY} -c pass"], budget={"attempts": 3})
        self.ok(c.create_run({"units": [u]}, run_id="r1"))
        res = self.drive_to_review(c, "r1", "u1", argv)
        return res, m.load("r1")["units"]["u1"]

    def test_unit_writing_inside_the_submodule_fails(self):
        script = (f"import os, runpy, sys; os.makedirs({SS.SHARED_PATH!r}, exist_ok=True); "
                  f"open(os.path.join({SS.SHARED_PATH!r}, 'lesson.md'), 'w').write('x'); "
                  f"sys.argv = [{self.runner!r}, 'write', 'a.txt=done']; runpy.run_path({self.runner!r})")
        res, unit = self._unit_run([PY, "-c", script])
        self.assertFalse(res["ok"])
        self.assertIn("shared_skills_clean", self.failed_checks(res))
        self.assertEqual(unit["state"], "failed")
        self.assertEqual(unit["last_reason"], "edit inside shared skills")

    def test_clean_unit_passes(self):
        res, unit = self._unit_run(self.argv("write", "a.txt=done"))
        self.ok(res)
        self.assertEqual(unit["state"], "reviewing")


class PreflightPathTests(CatalogMixin, IntegrationBase):
    def run_preflight(self):
        marker = os.path.join(self.tmp, "seen.txt")
        script = f"import os; open({marker!r}, 'w').write(str(os.path.isfile({CATALOG_JSON!r})))"
        sup = Supervisor(base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.tmp,
                                   "USER": "t", "LOGNAME": "t"}, grace=1.0)
        res = PF.preflight_runner(repo=self.repo, target_branch="main", worktree_root=os.path.join(self.tmp, "wt"),
                                  logs_dir=os.path.join(self.tmp, "logs"), runner="claude", adapter=ClaudeAdapter(),
                                  supervisor=sup, timeout=60, argv_override=[PY, "-c", script])
        return res, marker

    def test_preflight_worktree_is_initialized_before_the_runner(self):
        pinned = self.consumer(self.repo)
        res, marker = self.run_preflight()
        with open(marker) as fh:
            self.assertEqual(fh.read(), "True")
        self.assertEqual(res["shared_skills_commit"], pinned)
        self.assertNotIn("_preflight", git(self.repo, "worktree", "list"), "removed with --force")

    def test_preflight_fails_the_step_with_the_reason(self):
        self.consumer(self.repo)
        SS.ALLOWED_URL = "https://github.com/kennylhilljr/skills-catalog.git"  # restored by the patcher
        res, marker = self.run_preflight()
        self.assertFalse(res["ok"])
        self.assertFalse(os.path.exists(marker), "the runner never started")
        failed = [c for c in res["checks"] if not c["ok"]]
        self.assertEqual(failed[0]["name"], "shared_skills")
        self.assertIn("submodule URL not allowed", failed[0]["detail"])


class MergeQueuePathTests(CatalogMixin, QueueBase):
    def setUp(self):
        super().setUp()
        self.pinned = self.consumer(self.repo)
        git(self.repo, "push", "-q", "origin", "main")
        self.seen = []

        def gate_runner(unit, path):
            self.seen.append(os.path.isfile(os.path.join(path, CATALOG_JSON)))
            return {"passed": True, "gates": []}
        self.gate_runner = gate_runner
        self.q = self.new_queue()

    def test_merge_worktree_is_initialized_before_the_gates(self):
        self.branch("ecc/r1/a", {"a.txt": "a\n"})
        self.assertTrue(self.enqueue("a", "ecc/r1/a", unit=queue_unit("a", files=["a.txt"]))["ok"])
        out = self.q.process_next()
        self.assertEqual(out["data"]["status"], "merged", out)
        self.assertEqual(self.seen, [True])
        self.assertEqual(out["data"]["shared_skills_commit"], self.pinned)
        self.assertEqual(self.q.state()["history"][-1]["shared_skills_commit"], self.pinned)

    def test_merge_check_fallback_worktree_is_initialized(self):
        self.branch("ok", {"b.txt": "b\n"})
        returned = []

        def spy(path):
            returned.append(SS.init_shared_skills(path))
            return returned[-1]

        with mock.patch.object(Q, "init_shared_skills", spy):
            res = Q.merge_check(self.repo, "main", "ok", self.scratch, force_fallback=True)
        self.assertTrue(res["clean"])
        self.assertEqual(returned, [self.pinned], "initialized and checked in the throwaway worktree")

    def test_fallback_conflict_still_records_the_commit(self):
        self.branch("ecc/r1/a", {"README.md": "theirs\n"})
        self.commit("README.md", "ours\n")
        self.assertTrue(self.enqueue("a", "ecc/r1/a", unit=queue_unit("a", files=["README.md"]))["ok"])
        self.q = self.new_queue(force_fallback=True)
        out = self.q.process_next()
        self.assertEqual(out["data"]["status"], "conflict", out)
        self.assertEqual(self.q.state()["history"][-1]["shared_skills_commit"], self.pinned)

    def test_merge_stops_with_the_reason(self):
        self.branch("ecc/r1/a", {"a.txt": "a\n"})
        self.assertTrue(self.enqueue("a", "ecc/r1/a", unit=queue_unit("a", files=["a.txt"]))["ok"])
        SS.ALLOWED_URL = "https://github.com/kennylhilljr/skills-catalog.git"  # restored by the patcher
        before = git(self.repo, "rev-parse", "main")
        out = self.q.process_next()
        self.assertFalse(out["ok"])
        self.assertEqual(out["data"]["status"], "shared_skills_failed")
        self.assertIn("submodule URL not allowed", out["data"]["detail"])
        self.assertEqual(self.seen, [], "no gate ran")
        self.assertEqual(git(self.repo, "rev-parse", "main"), before)


class ReviewPathTests(CatalogMixin, TeamBase):
    def cli_override(self, role, runner, prompt):
        argv = super().cli_override(role, runner, prompt)
        marker = os.path.join(self.tmp, "verifier-saw.txt")
        probe = (f"import os\nif '_verify' in os.getcwd(): "
                 f"open({marker!r}, 'a').write(str(os.path.isfile({CATALOG_JSON!r})) + '\\n')\n")
        if argv[:2] == [PY, "-c"]:
            argv = [PY, "-c", probe + argv[2]]
        return argv

    def test_verifier_checkout_is_initialized_and_the_run_record_holds_the_commit(self):
        pinned = self.consumer(self.repo)
        self.setup_unit()
        self.step.run("r1", "u1")
        with open(os.path.join(self.tmp, "verifier-saw.txt")) as fh:
            self.assertEqual(fh.read().split(), ["True"])
        team = self.unit()["annotations"]["team"]
        self.assertEqual(team["shared_skills_commit"], pinned)
        self.assertEqual(self.unit()["annotations"]["merge"]["shared_skills_commit"], pinned)
        self.assertEqual(self.unit()["state"], "merged")
        self.assertNotIn("_verify", git(self.repo, "worktree", "list"))

    def test_verifier_check_failure_stops_for_the_owner(self):
        self.consumer(self.repo)
        self.setup_unit()
        SS.ALLOWED_URL = "https://github.com/kennylhilljr/skills-catalog.git"  # restored by the patcher
        res = self.step.run("r1", "u1")
        self.assertFalse(res["ok"])
        self.assertEqual(self.unit()["state"], "needs_user")
        self.assertEqual(self.unit()["last_reason"], "shared_skills_failed")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "verifier-saw.txt")), "no verifier ran")


class RefusedSubcommandTests(unittest.TestCase):
    """SUB-15: only the harness initializes submodules."""

    def test_submodule_stays_refused(self):
        self.assertIn("submodule", GIT_REFUSED_SUBCOMMANDS)
        self.assertEqual(check_command_policy(["git", "submodule", "update", "--init"]), "git submodule is refused")

    def test_gate_runner_refuses_git_submodule_update(self):
        with tempfile.TemporaryDirectory() as wt:
            unit = work_unit("u1", commands=["git submodule update --init --depth 1"])
            sup = Supervisor(base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, grace=0.3)
            res = run_gates(unit, wt, supervisor=sup)
        self.assertFalse(res["passed"])
        self.assertEqual(res["gates"][0]["status"], "refused")


if __name__ == "__main__":
    unittest.main()
