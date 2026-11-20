"""One branch + one worktree per work unit, under a root outside all repos.

Layout:  <root>/<repo_key>/<run_id>/<unit_id>            the worktree
         <root>/<repo_key>/<run_id>/<unit_id>.owner.json  ownership marker
         <root>/<repo_key>/_archive/*.bundle              archived unique work

The marker lives beside the worktree (not inside it) so it never shows up as
an untracked file in the unit's checkout.

Cleanup treats as unique work (blocking unless ``archive=True``): uncommitted
changes and untracked files (always blocking), commits on the unit branch not
on the target or a remote, commits reachable only from the worktree's HEAD
reflog (detached HEAD work that was never on the unit branch), and gitignored
files other than regenerable caches. Archived commits go into a verified
bundle (detached work under ``refs/ecc-archive/<run>/<unit>/...``), ignored
files into a tarball beside it.
"""

import fnmatch
import hashlib
import json
import os
import tarfile
import time

from ..runs.envelope import SCHEMA_VERSION, check, envelope
from ..runs.fsutil import atomic_write_json
from ..runs.store import iso, valid_identifier
from .git import GitError, git_out, rev_exists, run_git, toplevel, unique_commits
from .shared_skills import SHARED_PATH, _gitlink, dirty_shared_skills

FORBIDDEN_COMPONENTS = {".openclaw", ".git"}

# Ignored paths that are regenerable caches, never unique work (matched per path component).
REGENERABLE_IGNORED = ("__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".mypy_cache", ".ruff_cache")
ARCHIVE_REF_PREFIX = "refs/ecc-archive"
MAX_ORPHANS = 100


class WorktreeError(ValueError):
    pass


def _components(path):
    return [p for p in path.split(os.sep) if p]


def _inside(child, parent):
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def _enclosing_repo(path):
    """Return the nearest ancestor (inclusive) holding a `.git` entry, if any."""
    current = path
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def validate_root(root, repo):
    """Check a worktree root. Returns an envelope; never creates anything."""
    checks = []
    raw = os.path.abspath(os.path.expanduser(root))
    real = os.path.realpath(raw)
    bad = FORBIDDEN_COMPONENTS.intersection(_components(raw) + _components(real))
    checks.append(check("root_not_in_openclaw_or_git_dir", not bad, ",".join(sorted(bad))))
    try:
        repo_top = toplevel(repo)
    except (GitError, OSError) as exc:
        checks.append(check("repo_is_git", False, str(exc)))
        return envelope("worktree.validate_root", ok=False, checks=checks)
    checks.append(check("root_outside_repo", not _inside(real, repo_top), repo_top))
    existing = real
    while not os.path.exists(existing):
        existing = os.path.dirname(existing)
    enclosing = _enclosing_repo(existing)
    checks.append(check("root_outside_any_repo", enclosing is None, enclosing or ""))
    ok = all(c["ok"] for c in checks)
    return envelope("worktree.validate_root", ok=ok, checks=checks, data={"root": real, "repo": repo_top})


class WorktreeManager:
    def __init__(self, repo, root, clock=time.time, branch_prefix="ecc", archive_dir=None,
                 regenerable_ignored=REGENERABLE_IGNORED):
        result = validate_root(root, repo)
        if not result["ok"]:
            failed = [f"{c['name']}: {c['detail']}" for c in result["checks"] if not c["ok"]]
            raise WorktreeError("invalid worktree root: " + "; ".join(failed))
        self.repo = result["data"]["repo"]
        self.root = result["data"]["root"]
        self.clock = clock
        self.branch_prefix = branch_prefix
        self.regenerable_ignored = tuple(regenerable_ignored or ())
        digest = hashlib.sha256(self.repo.encode()).hexdigest()[:10]
        name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in os.path.basename(self.repo)) or "repo"
        self.base = os.path.join(self.root, f"{name}-{digest}")
        self.archive_dir = os.path.realpath(archive_dir) if archive_dir else os.path.join(self.base, "_archive")
        if _inside(self.archive_dir, self.repo) or ".openclaw" in _components(self.archive_dir):
            raise WorktreeError("archive dir must be outside the repository and .openclaw")

    # == naming ==
    def paths(self, run_id, unit_id):
        for label, value in (("run id", run_id), ("unit id", unit_id)):
            if not valid_identifier(value) or value.startswith("."):
                raise WorktreeError(f"invalid {label} {value!r}")
        run_dir = os.path.join(self.base, run_id)
        path = os.path.join(run_dir, unit_id)
        if not _inside(os.path.realpath(path), self.root):
            raise WorktreeError("worktree path escapes the root")
        return {
            "path": path,
            "branch": f"{self.branch_prefix}/{run_id}/{unit_id}",
            "marker": os.path.join(run_dir, f"{unit_id}.owner.json"),
        }

    def _registered(self):
        out = git_out(["worktree", "list", "--porcelain"], self.repo)
        entries, current = [], {}
        for line in out.splitlines():
            if not line:
                if current:
                    entries.append(current)
                current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value or True
        if current:
            entries.append(current)
        return {os.path.realpath(e["worktree"]): e for e in entries if "worktree" in e}

    # == create ==
    def create(self, run_id, unit_id, base_ref="HEAD", owner=None, dry_run=False):
        op = "worktree.create"
        try:
            p = self.paths(run_id, unit_id)
        except WorktreeError as exc:
            return envelope(op, ok=False, checks=[check("ids_valid", False, str(exc))])
        checks = [check("ids_valid", True)]
        fmt = run_git(["check-ref-format", "--branch", p["branch"]], self.repo, check=False)
        checks.append(check("branch_name_valid", fmt.returncode == 0, p["branch"]))
        checks.append(check("branch_absent", not rev_exists(self.repo, f"refs/heads/{p['branch']}"), p["branch"]))
        checks.append(check("path_absent", not os.path.lexists(p["path"]), p["path"]))
        checks.append(check("marker_absent", not os.path.lexists(p["marker"]), p["marker"]))
        base_ok = rev_exists(self.repo, base_ref)
        checks.append(check("base_ref_exists", base_ok, base_ref))
        if not all(c["ok"] for c in checks):
            return envelope(op, ok=False, checks=checks)
        base_commit = git_out(["rev-parse", "--verify", base_ref + "^{commit}"], self.repo)
        commands = [["git", "worktree", "add", "-b", p["branch"], p["path"], base_commit]]
        data = dict(p, run_id=run_id, unit_id=unit_id, base_commit=base_commit, commands=commands)
        if dry_run:
            return envelope(op, changed=False, checks=checks, data=data)
        os.makedirs(os.path.dirname(p["path"]), exist_ok=True)
        run_git(commands[0][1:], self.repo)
        marker = {
            "schema_version": SCHEMA_VERSION,
            "kind": "openclaw-ecc-worktree",
            "run_id": run_id,
            "unit_id": unit_id,
            "branch": p["branch"],
            "path": p["path"],
            "repo": self.repo,
            "base_commit": base_commit,
            "owner": owner,
            "created_at": iso(self.clock()),
        }
        atomic_write_json(p["marker"], marker)
        return envelope(op, changed=True, checks=checks, data=data,
                        rollback_checkpoint={"cleanup": {"run_id": run_id, "unit_id": unit_id}})

    # == list ==
    def list(self):
        registered = self._registered()
        found = []
        if os.path.isdir(self.base):
            for run_id in sorted(os.listdir(self.base)):
                run_dir = os.path.join(self.base, run_id)
                if run_id.startswith("_") or not os.path.isdir(run_dir):
                    continue
                for name in sorted(os.listdir(run_dir)):
                    if not name.endswith(".owner.json"):
                        continue
                    try:
                        with open(os.path.join(run_dir, name)) as fh:
                            marker = json.load(fh)
                    except (OSError, ValueError):
                        continue
                    marker["registered"] = os.path.realpath(marker.get("path", "")) in registered
                    marker["exists"] = os.path.isdir(marker.get("path", ""))
                    found.append(marker)
        return envelope("worktree.list", changed=False, data={"worktrees": found})

    # == cleanup ==
    def inspect(self, run_id, unit_id, target_branch):
        """Read-only safety inspection used by cleanup (and its dry-run)."""
        p = self.paths(run_id, unit_id)
        checks, info = [], dict(p)
        marker = None
        try:
            with open(p["marker"]) as fh:
                marker = json.load(fh)
        except (OSError, ValueError):
            pass
        owned = bool(marker) and marker.get("run_id") == run_id and marker.get("unit_id") == unit_id \
            and marker.get("repo") == self.repo and marker.get("branch") == p["branch"]
        checks.append(check("ownership_marker_matches", owned, p["marker"]))
        exists = os.path.isdir(p["path"])
        info["exists"] = exists
        target_ok = rev_exists(self.repo, f"refs/heads/{target_branch}")
        checks.append(check("target_branch_exists", target_ok, target_branch))
        branch_ok = rev_exists(self.repo, f"refs/heads/{p['branch']}")
        info["branch_exists"] = branch_ok
        info["ignored_files"] = []
        info["detached_head"] = None
        info["shared_skills_initialized"] = False
        if exists:
            untracked, changed, ignored = self._status(p["path"])
            checks.append(check("no_uncommitted_changes", not changed, ",".join(changed[:20])))
            checks.append(check("no_untracked_files", not untracked, ",".join(untracked[:20])))
            info["ignored_files"] = [f for f in ignored if not self._regenerable(f)]
            head_ref = run_git(["symbolic-ref", "-q", "HEAD"], p["path"], check=False).stdout.strip()
            if not head_ref:
                info["detached_head"] = run_git(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], p["path"],
                                                check=False).stdout.strip() or None
            # A detached HEAD is allowed; its commits are covered by the orphan check below.
            head_ok = head_ref == f"refs/heads/{p['branch']}" or not head_ref
            checks.append(check("head_on_unit_branch", head_ok, head_ref or "detached"))
            entry = self._registered().get(os.path.realpath(p["path"])) or {}
            locked = entry.get("locked")
            checks.append(check("worktree_not_locked", not locked, "" if locked in (None, True) else str(locked)))
            sub = os.path.join(p["path"], SHARED_PATH)
            info["shared_skills_initialized"] = os.path.lexists(os.path.join(sub, ".git"))
            if info["shared_skills_initialized"]:
                checks += self._shared_skills_checks(p["path"], sub)
        unique = []
        if branch_ok and target_ok:
            unique = unique_commits(self.repo, [f"refs/heads/{p['branch']}"], [f"refs/heads/{target_branch}"])
            info["tip"] = git_out(["rev-parse", f"refs/heads/{p['branch']}"], self.repo)
        info["unique_commits"] = unique
        info["orphaned_commits"] = self._orphaned_commits(p, exists, info["detached_head"]) if target_ok else []
        return checks, info

    def _shared_skills_checks(self, worktree, sub):
        """Checks that let cleanup remove a worktree holding an initialized shared
        skills submodule with `git worktree remove --force`, which deletes the
        submodule checkout and its git directory without looking inside. Every
        failure, including a git error, blocks; archive=True does not cover any
        of it, because nothing inside the submodule is archived."""
        checks = []
        try:
            top = os.path.realpath(git_out(["rev-parse", "--show-toplevel"], sub))
            dirty = dirty_shared_skills(worktree)
            # Explicit flags so submodule config (status.showUntrackedFiles=no and
            # the like) cannot hide anything; run inside the submodule so the
            # parent's submodule.<name>.ignore and diff.ignoreSubmodules do not apply.
            out = run_git(["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored",
                           "--ignore-submodules=none"], sub).stdout
            untracked, changed, ignored = self._parse_status(out)
            sub_ignored = [f for f in ignored if not self._regenerable(f)]
            # Compared directly so parent ignore settings cannot hide a moved HEAD.
            head = git_out(["rev-parse", "HEAD"], sub)
            staged, committed = _gitlink(worktree), _gitlink(worktree, "HEAD")
            moved = not (head == staged == committed)
            details = [
                (top != os.path.realpath(sub), f"submodule work tree is {top}"),
                (bool(dirty), dirty[1] if dirty else ""),
                (bool(changed), "changed: " + ",".join(changed[:20])),
                (bool(untracked), "untracked: " + ",".join(untracked[:20])),
                (bool(sub_ignored), "ignored: " + ",".join(sub_ignored[:20])),
                (moved, f"moved: HEAD {head[:12]}, staged {staged}, committed {committed}"),
            ]
            bad = [d for flag, d in details if flag]
            checks.append(check("shared_skills_clean", not bad, bad[0] if bad else ""))
            # Commits made inside the submodule live only in its git directory, which
            # --force deletes. Every commit object stored there (whatever holds it: a
            # branch, tag, stash, reflog, ORIG_HEAD, FETCH_HEAD or nothing at all) must
            # be in the pinned commit's history or on the catalog remote.
            local = self._local_commits(sub, committed or head)
            checks.append(check("shared_skills_no_local_commits", not local,
                                f"{len(local)} commit(s) in {SHARED_PATH} on no remote: {','.join(local[:5])}"
                                if local else ""))
        except (GitError, OSError) as exc:
            checks.append(check("shared_skills_clean", False, str(exc)[-300:]))
        # Any other initialized submodule, sibling or nested inside the shared one,
        # is not inspected above, so --force could delete work in it.
        proc = run_git(["submodule", "status", "--recursive"], worktree, check=False,
                       config={"submodule.recurse": "false"})
        others = []
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            fields = line[1:].split()
            path = fields[1] if len(fields) >= 2 else line.strip()
            if path == SHARED_PATH:
                continue
            full = os.path.join(worktree, path)
            if line[0] != "-" or (os.path.isdir(full) and os.listdir(full)):
                others.append(path)
        checks.append(check("only_shared_submodule_initialized", proc.returncode == 0 and not others,
                            ",".join(others[:20]) or proc.stderr.strip()[-200:]))
        return checks

    @staticmethod
    def _local_commits(sub, pinned):
        objects = git_out(["cat-file", "--batch-all-objects", "--batch-check=%(objecttype) %(objectname)"], sub)
        commits = [line.split()[1] for line in objects.splitlines() if line.startswith("commit ")]
        local = []
        for i in range(0, len(commits), 500):
            out = git_out(["rev-list", *commits[i:i + 500], "--not", pinned, "--remotes"], sub)
            local += [line for line in out.splitlines() if line and line not in local]
        return local

    def _status(self, path):
        out = run_git(["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored"], path).stdout
        return self._parse_status(out)

    @staticmethod
    def _parse_status(out):
        entries = out.split("\0")
        untracked, changed, ignored = [], [], []
        i = 0
        while i < len(entries):
            entry = entries[i]
            i += 1
            if len(entry) < 4:
                continue
            code, name = entry[:2], entry[3:]
            if code == "??":
                untracked.append(name)
            elif code == "!!":
                ignored.append(name)
            else:
                changed.append(name)
                if "R" in code or "C" in code:
                    i += 1  # the rename/copy source path follows
        return untracked, changed, ignored

    def _regenerable(self, rel):
        parts = [part for part in rel.split("/") if part]
        return any(fnmatch.fnmatchcase(part, pattern) for part in parts for pattern in self.regenerable_ignored)

    def _reflog(self, ref, cwd):
        proc = run_git(["log", "-g", "--format=%H", ref, "--"], cwd, check=False)
        return [line for line in proc.stdout.splitlines() if line] if proc.returncode == 0 else []

    def _orphaned_commits(self, p, exists, detached_head):
        """Commits the worktree's HEAD visited that no branch, tag or remote reaches
        and that were never the tip of the unit branch (so a rebase or amend on
        the branch is not reported, but detached HEAD work is)."""
        candidates = []
        if exists:
            candidates += self._reflog("HEAD", p["path"])
        if detached_head:
            candidates.append(detached_head)
        if not candidates:
            return []
        branch_history = set(self._reflog(f"refs/heads/{p['branch']}", self.repo))
        seen, ordered = set(), []
        for sha in candidates:
            if sha not in seen and sha not in branch_history:
                seen.add(sha)
                ordered.append(sha)
        ordered = [sha for sha in ordered
                   if run_git(["cat-file", "-e", f"{sha}^{{commit}}"], self.repo, check=False).returncode == 0]
        if not ordered:
            return []
        out = git_out(["rev-list", *ordered, "--not", "--branches", "--tags", "--remotes"], self.repo)
        loose = set(out.splitlines())
        orphans = [sha for sha in ordered if sha in loose]
        if detached_head in orphans:
            orphans.remove(detached_head)
            orphans.insert(0, detached_head)
        return orphans[:MAX_ORPHANS]

    def cleanup(self, run_id, unit_id, target_branch, archive=False, dry_run=False):
        op = "worktree.cleanup"
        try:
            checks, info = self.inspect(run_id, unit_id, target_branch)
        except WorktreeError as exc:
            return envelope(op, ok=False, checks=[check("ids_valid", False, str(exc))])
        unique = info["unique_commits"]
        orphans = info["orphaned_commits"]
        ignored = info["ignored_files"]
        actions, commands = [], []
        stamp = (info.get("tip") or (orphans[0] if orphans else None) or info.get("detached_head") or "noref")[:12]
        bundle = None
        archived_refs = {}
        if orphans:
            prefix = f"{ARCHIVE_REF_PREFIX}/{run_id}/{unit_id}"
            for sha in orphans:
                name = f"{prefix}/detached-head" if sha == info["detached_head"] else f"{prefix}/orphan-{sha[:12]}"
                archived_refs[name] = sha
        if unique or orphans:
            if archive:
                bundle = os.path.join(self.archive_dir, f"{run_id}--{unit_id}--{stamp}.bundle")
                refs = ([f"refs/heads/{info['branch']}"] if unique else []) + list(archived_refs)
                for name, sha in archived_refs.items():
                    commands.append(["git", "update-ref", name, sha, ""])
                commands.append(["git", "bundle", "create", bundle, *refs,
                                 "--not", f"refs/heads/{target_branch}", "--remotes"])
                commands.append(["git", "bundle", "verify", bundle])
                for name, sha in archived_refs.items():
                    commands.append(["git", "update-ref", "-d", name, sha])
                total = len(unique) + len(orphans)
                checks.append(check("unique_commits_archived", True, f"{total} commit(s) to {bundle}"))
            else:
                if unique:
                    checks.append(check("no_unique_commits", False,
                                        f"{len(unique)} commit(s) not on {target_branch} or a remote"))
                    actions.append({"kind": "unmerged_work", "run_id": run_id, "unit_id": unit_id,
                                    "detail": f"{len(unique)} unique commit(s) on {info['branch']}; merge, push, "
                                              "or archive=True"})
                if orphans:
                    checks.append(check("no_orphaned_commits", False,
                                        f"{len(orphans)} detached HEAD commit(s) reachable from no branch or remote"))
                    actions.append({"kind": "unmerged_work", "run_id": run_id, "unit_id": unit_id,
                                    "detail": f"{len(orphans)} detached HEAD commit(s) in {info['path']}; "
                                              "branch them or archive=True"})
        ignored_archive = None
        if ignored:
            if archive:
                ignored_archive = os.path.join(self.archive_dir, f"{run_id}--{unit_id}--{stamp}--ignored.tar.gz")
                checks.append(check("ignored_files_archived", True, f"{len(ignored)} file(s) to {ignored_archive}"))
            else:
                checks.append(check("no_ignored_files", False, ",".join(ignored[:20])))
                actions.append({"kind": "unarchived_ignored_files", "run_id": run_id, "unit_id": unit_id,
                                "detail": f"{len(ignored)} gitignored file(s) in {info['path']} would be deleted; "
                                          "move them or archive=True"})
        n_archive = len(commands)
        if info["exists"] and info.get("shared_skills_initialized"):
            # git refuses to remove a worktree holding an initialized submodule. One
            # --force skips only that refusal and git's clean check, which inspect has
            # just done itself for the parent and the submodule. Never pass it twice:
            # a second --force would also remove a locked worktree.
            commands.append(["git", "worktree", "remove", "--force", info["path"]])
        elif info["exists"]:
            commands.append(["git", "worktree", "remove", info["path"]])
        else:
            commands.append(["git", "worktree", "prune"])
        if info["branch_exists"]:
            commands.append(["git", "branch", "-D", info["branch"]])
        ok = all(c["ok"] for c in checks)
        data = {"path": info["path"], "branch": info["branch"], "unique_commits": unique,
                "orphaned_commits": orphans, "ignored_files": ignored, "bundle": bundle,
                "archived_refs": archived_refs if bundle else {}, "ignored_archive": ignored_archive,
                "commands": commands}
        if not ok or dry_run:
            return envelope(op, ok=ok, changed=False, checks=checks, required_user_actions=actions, data=data)
        rollback = None
        if bundle or ignored_archive:
            os.makedirs(self.archive_dir, exist_ok=True)
            rollback = {}
        if ignored_archive:
            self._archive_ignored(info["path"], ignored, ignored_archive)
            rollback.update({"ignored_archive": ignored_archive, "ignored_files": ignored,
                             "restore_ignored": ["tar", "-xzf", ignored_archive, "-C", info["path"]]})
        if bundle:
            try:
                for cmd in commands[:n_archive]:
                    if cmd[1:3] != ["update-ref", "-d"]:
                        run_git(cmd[1:], self.repo)
            finally:
                for name, sha in archived_refs.items():
                    run_git(["update-ref", "-d", name, sha], self.repo, check=False)
            rollback.update({"bundle": bundle, "branch": info["branch"], "tip": info.get("tip")})
            if unique:
                rollback["restore"] = ["git", "fetch", bundle, f"{info['branch']}:refs/heads/{info['branch']}"]
            if archived_refs:
                rollback["archived_refs"] = dict(archived_refs)
                rollback["restore_refs"] = ["git", "fetch", bundle] + [f"{n}:{n}" for n in archived_refs]
        for cmd in commands[n_archive:]:
            run_git(cmd[1:], self.repo)
        try:
            os.unlink(info["marker"])
        except FileNotFoundError:
            pass
        try:
            os.rmdir(os.path.dirname(info["path"]))
        except OSError:
            pass
        return envelope(op, changed=True, checks=checks, data=data, rollback_checkpoint=rollback)

    @staticmethod
    def _archive_ignored(worktree, files, tarball):
        """Write ignored files to ``tarball`` atomically and verify every member is present."""
        tmp = tarball + ".partial"
        with tarfile.open(tmp, "w:gz") as tf:
            for rel in files:
                tf.add(os.path.join(worktree, rel), arcname=rel, recursive=False)
        with tarfile.open(tmp, "r:gz") as tf:
            names = set(tf.getnames())
        missing = [rel for rel in files if rel not in names]
        if missing:
            os.unlink(tmp)
            raise WorktreeError(f"ignored file archive incomplete: {len(missing)} file(s) missing")
        os.replace(tmp, tarball)
