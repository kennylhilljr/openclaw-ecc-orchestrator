"""skills-catalog SUB-11 and SUB-12: lessons about shared skills never edit the
`.agents/skills/shared` submodule; they go to "Proposed for the catalog" in the
ledger and become one `lesson` issue per skill on the catalog."""

import fnmatch
import json
import os
import re
import subprocess
import tempfile
import unittest

try:
    from ._integration import PY, git
    from .test_team_drive import DriveBase
except ImportError:  # discovered as a top level module
    from _integration import PY, git
    from test_team_drive import DriveBase

from openclaw_ecc_orchestrator.runners.discovery import HttpResponse
from openclaw_ecc_orchestrator.team import catalog_lessons as CL
from openclaw_ecc_orchestrator.team import learning as L
from openclaw_ecc_orchestrator.team import personas as P

PRACTICES = "changed the repository's practice files"
SHARED_FILE = ".agents/skills/shared/skills/verify-change/SKILL.md"
LINK = ".claude/skills/verify-change"


def owned(path, globs):
    """True when `path` is editable under `globs`, by fnmatch and by the applier's own matcher."""
    return any(fnmatch.fnmatch(path, g) for g in globs) or L.Steward._matches(path, globs)


def make_consumer(root):
    """A consumer tree: shared skill folder, a shared link, a project overlay skill and a project link."""
    os.makedirs(os.path.join(root, ".agents/skills/shared/skills/verify-change"))
    os.makedirs(os.path.join(root, ".agents/skills/shared/skills/cross-review"))
    os.makedirs(os.path.join(root, ".agents/skills/security-review-proj"))
    os.makedirs(os.path.join(root, ".claude/skills"))
    os.symlink("../../.agents/skills/shared/skills/verify-change", os.path.join(root, LINK))
    os.symlink("../../.agents/skills/security-review-proj", os.path.join(root, ".claude/skills/security-review-proj"))


class FakeIssues:
    def __init__(self, existing=()):
        self.opened = []
        self.existing = set(existing)

    def exists(self, mark):
        return any(mark in body for body in self.existing) or any(mark in o["body"] for o in self.opened)

    def open(self, title, body, label):
        self.opened.append({"title": title, "body": body, "label": label})
        return f"https://github.com/kennylhilljr/skills-catalog/issues/{len(self.opened)}"


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        make_consumer(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_steward_ownership_never_covers_shared_skills(self):
        """SUB-11: default config and a config granting .agents/skills/** (and wider) exclude the submodule."""
        configs = [L.settings({})["ownership"],
                   L.settings({"learning": {"ownership": {
                       "coder": [".agents/skills/**"], "gates": [".agents/**", ".claude/**"],
                       "verifier": [".agents/skills/*/**", ".agents/**/SKILL.md", ".claude/*/**", ".*/**"],
                       "architect": ["**"], "designer": [".agents/skills/s*/**", ".agents/skills/?hared/**",
                                                         ".agents/skills/shared", ".claude/skills/verify-change"],
                   }}})["ownership"]]
        for ownership in configs:
            filtered = CL.filter_ownership(ownership, [self.root])
            self.assertEqual(set(filtered), set(ownership))
            for role, globs in filtered.items():
                for path in (SHARED_FILE, CL.SHARED_ROOT, ".agents/skills/shared/catalog.json", LINK,
                             LINK + "/SKILL.md"):
                    self.assertFalse(owned(path, globs), (role, path, globs))
        # What the configuration granted outside the submodule is kept.
        wide = CL.filter_ownership({"coder": [".agents/skills/**"], "architect": ["**"]}, [self.root])
        for path in (".agents/skills/security-review-proj/SKILL.md", ".agents/skills/sharedx/SKILL.md",
                     ".agents/skills/s/SKILL.md", ".agents/skills/new-skill/SKILL.md"):
            self.assertTrue(owned(path, wide["coder"]), path)
        for path in ("AGENTS.md", ".claude/skills/security-review-proj", "src/x.py"):
            self.assertTrue(owned(path, wide["architect"]), path)
        self.assertEqual(CL.filter_ownership({"pr_reviewer": ["AGENTS.md", ".agents/skills/review-*/**"]},
                                             [self.root])["pr_reviewer"], ["AGENTS.md", ".agents/skills/review-*/**"])

    def test_applier_refuses_shared_paths(self):
        """SUB-11: the applier refuses the gitlink, files under it and shared links, and sees unseen writes."""
        changed = [CL.SHARED_ROOT, SHARED_FILE, LINK, "AGENTS.md", ".claude/skills/security-review-proj"]
        self.assertEqual(CL.refused_paths(changed, [self.root]), [CL.SHARED_ROOT, SHARED_FILE, LINK])
        before = CL.shared_snapshot(self.root)
        with open(os.path.join(self.root, SHARED_FILE), "w") as fh:
            fh.write("edited\n")
        after_write = CL.shared_snapshot(self.root)
        self.assertNotEqual(after_write, before)
        os.chmod(os.path.join(self.root, SHARED_FILE), 0o755)  # a mode flip git would see
        after_mode = CL.shared_snapshot(self.root)
        self.assertNotEqual(after_mode, after_write)
        alias = os.path.join(self.root, CL.SHARED_SKILLS, "alias")
        os.symlink("verify-change", alias)
        after_link = CL.shared_snapshot(self.root)
        self.assertNotEqual(after_link, after_mode)
        os.remove(alias)
        os.symlink("cross-review", alias)  # a folder link retargeted
        self.assertNotEqual(CL.shared_snapshot(self.root), after_link)

    def test_shared_lessons_land_in_the_catalog_section(self):
        """SUB-11: shared skill practices resolve; overlay skills and other files do not."""
        lessons = [
            {"id": "L0001", "unit": "U1", "practice": SHARED_FILE, "what": "Run every acceptance command."},
            {"id": "L0002", "unit": "U1", "practice": LINK, "what": "Quote the failing output."},
            {"id": "L0003", "unit": "U2", "practice": ".agents/skills/cross-review", "what": "Name the head."},
            {"id": "L0004", "unit": "U2", "practice": ".agents/skills/security-review-proj/SKILL.md",
             "what": "Project rule."},
            {"id": "L0005", "unit": "U2", "practice": ".claude/skills/security-review-proj", "what": "Overlay."},
            {"id": "L0006", "unit": "U2", "practice": "AGENTS.md", "what": "Repo rule."},
            {"id": "L0007", "unit": "U2", "what": "No practice."},
        ]
        entries = CL.catalog_entries(lessons, [self.root])
        self.assertEqual([(e["id"], e["skill"]) for e in entries],
                         [("L0001", "verify-change"), ("L0002", "verify-change"), ("L0003", "cross-review")])
        ledger = os.path.join(self.root, ".agents/LESSONS.md")
        with open(ledger, "w") as fh:
            fh.write("# Lessons\n\n## Applied\n\n- L0009 something\n\n## Proposed for the owner\n\n- L0008 x\n")
        self.assertEqual(CL.write_ledger(ledger, entries, "2026-11-18"), ["L0001", "L0002", "L0003"])
        self.assertEqual(CL.write_ledger(ledger, entries, "2026-11-18"), [])  # idempotent
        with open(ledger) as fh:
            text = fh.read()
        section = text.split("## Proposed for the catalog")[1]
        self.assertIn("L0001 2026-11-18, unit U1, skill `verify-change`: Run every acceptance command.", section)
        self.assertIn("L0003 2026-11-18, unit U2, skill `cross-review`: Name the head.", section)
        self.assertNotIn("Project rule.", text)
        self.assertIn("- L0008 x", text.split("## Proposed for the catalog")[0])

    def test_lesson_text_stays_verbatim(self):
        """SUB-12: the text as the persona wrote it reaches the entry, with its line breaks and spacing."""
        log = L.Lessons(self.root, "r1")
        text = "Run the commands verbatim:\n  - first  `make test`\n  - then `make lint`"
        log.add(source="lesson", role="verifier", unit="U1", practice=SHARED_FILE, what=text)
        log.add(source="lesson", role="verifier", unit="U2", practice=SHARED_FILE, what="One line.")
        entries = CL.catalog_entries(log.all(), [self.root])
        self.assertEqual([e["lesson"] for e in entries], [text, "One line."])
        self.assertEqual(log.all()[0]["what"], "Run the commands verbatim: - first `make test` - then `make lint`")
        ledger = os.path.join(self.root, "LESSONS.md")
        CL.write_ledger(ledger, entries)
        with open(ledger) as fh:
            self.assertIn("`verify-change`: Run the commands verbatim:\n    - first  `make test`\n", fh.read())
        body = CL.issue_body("o/r", "M3", "verify-change", entries)
        self.assertIn("> Run the commands verbatim:\n>   - first  `make test`\n>   - then `make lint`", body)

    def test_ledger_keeps_same_ids_from_different_runs(self):
        """Lesson ids restart in each run, so the ledger keys entries by id and run."""
        ledger = os.path.join(self.root, "LESSONS.md")
        a = {"id": "L0001", "run": "r1", "skill": "verify-change", "unit": "U1", "lesson": "first"}
        b = {"id": "L0001", "run": "r2", "skill": "cross-review", "unit": "U9", "lesson": "second"}
        self.assertEqual(CL.write_ledger(ledger, [a]), ["L0001"])
        self.assertEqual(CL.write_ledger(ledger, [b]), ["L0001"])
        self.assertEqual(CL.write_ledger(ledger, [a, b]), [])
        with open(ledger) as fh:
            text = fh.read()
        self.assertIn("run r1, unit U1, skill `verify-change`: first", text)
        self.assertIn("run r2, unit U9, skill `cross-review`: second", text)

    def test_absolute_links_and_submodule_commits_are_seen(self):
        os.symlink(os.path.join(self.root, CL.SHARED_SKILLS, "cross-review"),
                   os.path.join(self.root, ".claude/skills/cross-review"))
        self.assertIn("cross-review", CL.shared_links([self.root]))
        self.assertIn(".claude/skills/cross-review", CL.refused_paths([".claude/skills/cross-review"], [self.root]))
        sub = os.path.join(self.root, CL.SHARED_ROOT)
        for args in (("init", "-q"), ("config", "user.email", "t@example.invalid"), ("config", "user.name", "T"),
                     ("add", "-A"), ("commit", "-qm", "one", "--allow-empty")):
            git(sub, *args)
        before = CL.shared_snapshot(self.root)
        git(sub, "commit", "--allow-empty", "-qm", "same tree")
        self.assertNotEqual(CL.shared_snapshot(self.root), before)

    def test_steward_prompt_names_the_section(self):
        prompt = P.steward_prompt([{"id": "L0004", "role": "coder", "what": "x"}], {"coder": ["AGENTS.md"]},
                                  ".agents/LESSONS.md", "docs/spec/**",
                                  catalog=[{"id": "L0001", "skill": "verify-change", "unit": "U1", "lesson": "y"}])
        self.assertIn("Proposed for the catalog", prompt)
        self.assertIn("Never edit the shared skills", prompt)
        self.assertIn("L0001 [skill verify-change, unit U1]: y", prompt)


class IssueTests(unittest.TestCase):
    ENTRIES = [
        {"id": "L0001", "skill": "verify-change", "unit": "U1", "lesson": "Run every acceptance command."},
        {"id": "L0002", "skill": "cross-review", "unit": "U1", "lesson": "Name the head sha."},
        {"id": "L0003", "skill": "verify-change", "unit": "U2", "lesson": "Quote the *failing* output\nverbatim."},
    ]

    def link(self, uid):
        return f"https://github.com/example/consumer/commit/{uid}abc"

    def test_one_issue_per_skill_with_label_lessons_and_links(self):
        """SUB-12: one issue per affected skill, labeled lesson, lessons verbatim, unit links, marker."""
        fake = FakeIssues()
        res = CL.file_issues(self.ENTRIES, repo="example/consumer", milestone="M3", opener=fake.open,
                             exists=fake.exists, unit_link=self.link)
        self.assertEqual([r["status"] for r in res], ["opened", "opened"])
        self.assertEqual([o["title"] for o in fake.opened],
                         ["lesson: verify-change: 2 lessons from example/consumer M3",
                          "lesson: cross-review: 1 lessons from example/consumer M3"])
        self.assertEqual({o["label"] for o in fake.opened}, {"lesson"})
        body = fake.opened[0]["body"]
        self.assertIn("> Run every acceptance command.", body)
        self.assertIn("> Quote the *failing* output\n> verbatim.", body)
        self.assertIn("[U1](https://github.com/example/consumer/commit/U1abc)", body)
        self.assertIn("[U2](https://github.com/example/consumer/commit/U2abc)", body)
        self.assertIn("<!-- catalog-lesson: example/consumer M3 verify-change -->", body)
        self.assertNotIn("Name the head sha.", body)

    def test_rerun_with_marker_present_opens_nothing(self):
        """SUB-12: an existing issue (open or closed) with the marker blocks a second one."""
        bodies = [CL.issue_body("example/consumer", "M3", s, []) for s in ("verify-change", "cross-review")]
        fake = FakeIssues(existing=bodies)
        res = CL.file_issues(self.ENTRIES, repo="example/consumer", milestone="M3", opener=fake.open,
                             exists=fake.exists)
        self.assertEqual(fake.opened, [])
        self.assertEqual([r["status"] for r in res], ["already filed", "already filed"])
        # And the drive's own record is enough without asking the catalog.
        filed, fresh = {}, FakeIssues()
        CL.file_issues(self.ENTRIES, repo="r", milestone="M3", opener=fresh.open, exists=fresh.exists, filed=filed)
        CL.file_issues(self.ENTRIES, repo="r", milestone="M3", opener=fresh.open,
                       exists=lambda m: self.fail("asked the catalog again"), filed=filed)
        self.assertEqual(len(fresh.opened), 2)

    def test_no_catalog_lessons_opens_nothing(self):
        fake = FakeIssues()
        self.assertEqual(CL.file_issues([], repo="r", milestone="M3", opener=fake.open, exists=fake.exists), [])
        self.assertEqual(fake.opened, [])

    def test_opener_failure_is_reported_not_raised(self):
        def boom(title, body, label):
            raise RuntimeError("no permission")
        res = CL.file_issues(self.ENTRIES[:1], repo="r", milestone="M3", opener=boom, exists=lambda m: False)
        self.assertEqual(res[0]["status"], "error")
        self.assertIn("no permission", res[0]["error"])

    def test_default_opener_runs_gh_create_with_the_lesson_label(self):
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw))
            out = "[]" if argv[1:3] == ["issue", "list"] else "https://github.com/kennylhilljr/skills-catalog/issues/9\n"
            return subprocess.CompletedProcess(argv, 0, out, "")
        gh = CL.GhIssues(token_env="CATALOG_TEST_TOKEN", run=run)
        os.environ["CATALOG_TEST_TOKEN"] = "value-not-printed"
        try:
            self.assertFalse(gh.exists("<!-- m -->"))
            url = gh.open("lesson: x: 1 lesson from r M3", "body", "lesson")
        finally:
            del os.environ["CATALOG_TEST_TOKEN"]
        self.assertEqual(url, "https://github.com/kennylhilljr/skills-catalog/issues/9")
        argv, kw = calls[1]
        self.assertEqual(argv[:6], ["gh", "issue", "create", "-R", "kennylhilljr/skills-catalog", "--label"])
        self.assertEqual(argv[6], "lesson")
        self.assertEqual(kw["input"], "body")
        self.assertEqual(kw["env"]["GH_TOKEN"], "value-not-printed")
        self.assertNotIn("pr", argv)


class DriveFlowTests(DriveBase):
    """The whole flow in a consumer repository with the catalog as a real submodule."""

    LESSONS = [{"practice": SHARED_FILE, "lesson": "Run the acceptance commands verbatim"},
               {"practice": LINK, "lesson": "Quote the failing output"},
               {"practice": ".agents/skills/security-review-proj/SKILL.md", "lesson": "Project overlay rule"}]

    def setUp(self):
        super().setUp()
        cat = os.path.join(self.tmp, "catalog")
        os.makedirs(os.path.join(cat, "skills", "verify-change"))
        git(cat, "init", "-q", "-b", "main")
        git(cat, "config", "user.email", "test@example.invalid")
        git(cat, "config", "user.name", "Test")
        for name, text in (("skills/verify-change/SKILL.md", "# verify-change\n"), ("catalog.json", "{}\n")):
            with open(os.path.join(cat, name), "w") as fh:
                fh.write(text)
        git(cat, "add", "-A")
        git(cat, "commit", "-q", "-m", "catalog")
        self.catalog = cat
        git(self.repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", cat, CL.SHARED_ROOT)
        os.makedirs(os.path.join(self.repo, ".claude/skills"))
        os.symlink("../../.agents/skills/shared/skills/verify-change", os.path.join(self.repo, LINK))
        os.makedirs(os.path.join(self.repo, ".agents/skills/security-review-proj"))
        with open(os.path.join(self.repo, ".agents/skills/security-review-proj/SKILL.md"), "w") as fh:
            fh.write("# overlay\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "consumer with shared skills")
        self.steward_script = os.path.join(self.tmp, "steward.py")
        self.write_steward(["AGENTS.md"])
        self.fake = FakeIssues()

    def write_steward(self, files, extra=""):
        body = "".join(f"os.makedirs(os.path.dirname({f!r}) or '.', exist_ok=True)\n"
                       f"open({f!r}, 'a').write('- Project overlay rule.\\n')\n" for f in files)
        with open(self.steward_script, "w") as fh:
            fh.write("import os, subprocess\n" + body + extra +
                     "subprocess.run(['git', 'add', '-A'], check=True)\n"
                     "subprocess.run(['git', 'commit', '-qm', 'Practices: overlay'], check=True)\n"
                     f"print({P.STEWARD_MARK!r} + ' ' + '{{\"applied\": [\"L0003\"], \"proposed\": [], \"skipped\": []}}')\n")

    def reply(self, role, head):
        if role == "pr_reviewer":
            doc = {"verdict": "approved", "head": head or self.current_head(), "findings": [], "lessons": self.LESSONS}
            return f"{P.REVIEW_MARK} {json.dumps(doc)}"
        return super().reply(role, head)

    def practices_reply(self, prompt):
        head = re.search(r'"head": "([0-9a-f]{40})"', prompt).group(1)
        return f"{P.REVIEW_MARK} {json.dumps({'verdict': 'approved', 'head': head, 'findings': []})}"

    def cli_override(self, role, runner, prompt):
        if role == "steward":
            self.calls.append(("steward", runner))
            self.prompts.append(("steward", prompt))
            return [PY, self.steward_script]
        if PRACTICES in prompt:
            self.calls.append(("practices_review", runner))
            return [PY, "-c", f"print({json.dumps(self.practices_reply(prompt))})"]
        return super().cli_override(role, runner, prompt)

    def http_post(self, url, headers, body, timeout):
        prompt = (body.get("messages") or [{}])[0].get("content") or \
            ((body.get("contents") or [{}])[0].get("parts") or [{}])[0].get("text") or ""
        if PRACTICES in prompt:
            self.calls.append(("practices_review", "api"))
            text = self.practices_reply(prompt)
            data = {"candidates": [{"content": {"parts": [{"text": text}]}}]} if "googleapis" in url else \
                {"choices": [{"message": {"content": text}}]}
            return HttpResponse(200, json.dumps(data))
        return super().http_post(url, headers, body, timeout)

    def team(self):
        return {"merge_approval": "agents", "milestone": {"name": "M3"},
                "learning": {"after_merged_units": 1, "min_new_lessons": 1}}

    def drive(self):
        d = self.make(self.units2(), team=self.team())
        d.catalog_issues = self.fake
        d.steward.state["catalog"]["repo"] = "example/consumer"
        return d

    def runs(self):
        with open(os.path.join(self.state_root, "team", "r1-drive.json")) as fh:
            return json.load(fh)["learning"]["runs"]

    def shared_state(self):
        sub = os.path.join(self.repo, CL.SHARED_ROOT)
        return (CL.shared_snapshot(self.repo), git(sub, "rev-parse", "HEAD"), git(sub, "status", "--porcelain"),
                git(self.catalog, "rev-parse", "HEAD"), git(self.catalog, "status", "--porcelain"),
                git(self.repo, "rev-parse", "main:" + CL.SHARED_ROOT))

    def test_milestone_files_one_issue_per_skill_and_leaves_the_submodule_alone(self):
        """SUB-11 and SUB-12 end to end."""
        before = self.shared_state()
        d = self.drive()
        res = d.go()
        self.assertTrue(res["ok"], json.dumps(res, indent=1, default=str)[:3000])
        # Exactly one issue, for the one affected shared skill.
        self.assertEqual(len(self.fake.opened), 1, self.fake.opened)
        issue = self.fake.opened[0]
        self.assertEqual(issue["label"], "lesson")
        self.assertTrue(issue["title"].startswith("lesson: verify-change: "), issue["title"])
        self.assertTrue(issue["title"].endswith(" lessons from example/consumer M3"), issue["title"])
        self.assertIn("> Run the acceptance commands verbatim", issue["body"])
        self.assertIn("> Quote the failing output", issue["body"])
        self.assertNotIn("Project overlay rule", issue["body"])
        self.assertIn("https://github.com/example/consumer/commit/", issue["body"])
        self.assertIn("<!-- catalog-lesson: example/consumer M3 verify-change -->", issue["body"])
        # The ledger on main lists them under Proposed for the catalog; the overlay lesson went to the steward.
        ledger = git(self.repo, "show", "main:.agents/LESSONS.md")
        section = ledger.split("## Proposed for the catalog")[1]
        self.assertIn("skill `verify-change`: Run the acceptance commands verbatim", section)
        self.assertNotIn("Project overlay rule", section)
        self.assertIn("Project overlay rule", git(self.repo, "show", "main:AGENTS.md"))
        steward = [p for r, p in self.prompts if r == "steward"]
        self.assertTrue(steward)
        self.assertTrue(all("Proposed for the catalog" in p and "Never edit the shared skills" in p for p in steward))
        self.assertTrue(all("Project overlay rule" in p.split("Lessons:")[1].split("Already under")[0]
                            for p in steward))
        # A rerun of the milestone end opens nothing more.
        self.assertEqual([r["status"] for r in d.steward.file_catalog_lessons()], [])
        self.assertFalse(d.steward.due(final=True))
        self.assertEqual(len(self.fake.opened), 1)
        self.assertEqual(self.shared_state(), before)

    def test_steward_edit_inside_the_submodule_is_refused(self):
        """SUB-11: a steward that writes into the submodule anyway is rejected and nothing lands."""
        self.write_steward(["AGENTS.md", SHARED_FILE])
        before = self.shared_state()
        res = self.drive().go()
        self.assertTrue(res["ok"], json.dumps(res, indent=1, default=str)[:3000])
        runs = self.runs()
        self.assertTrue(runs and all(r["status"] == "rejected: wrote inside the shared skills" for r in runs), runs)
        self.assertEqual(self.shared_state(), before)

    def test_steward_retargeting_a_shared_link_is_refused(self):
        self.write_steward(["AGENTS.md"], extra=f"os.remove({LINK!r})\nos.symlink('../../AGENTS.md', {LINK!r})\n")
        res = self.drive().go()
        self.assertTrue(res["ok"], json.dumps(res, indent=1, default=str)[:3000])
        runs = self.runs()
        self.assertTrue(runs and all(r["status"] == "rejected: outside the practice files" and LINK in r["files"]
                            for r in runs), runs)
        self.assertEqual(os.readlink(os.path.join(self.repo, LINK)), "../../.agents/skills/shared/skills/verify-change")

    def test_issue_waits_until_the_ledger_line_is_on_the_target(self):
        """SUB-11 before SUB-12: no issue while the ledger merge fails; the next milestone end retries both."""
        d = self.drive()
        real = d.steward._merge
        d.steward._merge = lambda record, branch, head: (
            dict(record, status="gates failed") if str(record["n"]).startswith("catalog") else
            real(record, branch, head))
        res = d.go()
        self.assertTrue(res["ok"], json.dumps(res, indent=1, default=str)[:3000])
        self.assertEqual(self.fake.opened, [])
        self.assertNotIn("Proposed for the catalog", git(self.repo, "show", "main:.agents/LESSONS.md")
                         if "LESSONS.md" in git(self.repo, "ls-tree", "-r", "--name-only", "main") else "")
        d.steward._merge = real
        self.assertTrue(d.steward.due(final=True))
        d.steward.run(final=True)
        self.assertIn("Proposed for the catalog", git(self.repo, "show", "main:.agents/LESSONS.md"))
        self.assertEqual(len(self.fake.opened), 1)
        self.assertFalse(d.steward.due(final=True))

    def test_milestone_without_catalog_lessons_opens_nothing(self):
        self.LESSONS = [self.LESSONS[2]]
        res = self.drive().go()
        self.assertTrue(res["ok"], json.dumps(res, indent=1, default=str)[:3000])
        self.assertEqual(self.fake.opened, [])
        self.assertNotIn("Proposed for the catalog", git(self.repo, "show", "main:.agents/LESSONS.md")
                         if "LESSONS.md" in git(self.repo, "ls-tree", "-r", "--name-only", "main") else "")


if __name__ == "__main__":
    unittest.main()
