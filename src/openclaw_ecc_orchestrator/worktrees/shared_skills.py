"""The shared skills submodule in orchestrator worktrees (skills-catalog
SPEC SUB-5, SUB-6 to SUB-8, SUB-10, SUB-15; DESIGN 4.2 and 4.5).

A consumer repository may carry the skills catalog as a git submodule at
``.agents/skills/shared``. ``git worktree add`` does not initialize
submodules, so every worktree the orchestrator creates would otherwise hold
an empty folder. The harness, never an agent, initializes it:

* :func:`init_shared_skills` checks the ``.gitmodules`` URL against the
  allowlist, then runs ``git submodule update --init --depth 1`` for that
  one path with every transport but https refused, and finally runs the
  check below. It returns the pinned catalog commit.
* :func:`check_shared_skills` is the submodule check: the URL is the
  allowlisted catalog URL, ``catalog.json`` exists, and ``git submodule
  status`` shows neither ``-`` (not initialized) nor ``+`` (wrong commit).
* :func:`dirty_shared_skills` is the post-unit check: an agent that wrote
  inside the submodule produces no diff in the parent, so its edit would be
  lost silently. A dirty or moved submodule fails the unit.

Without an ``.agents/skills/shared`` entry in ``.gitmodules`` all three
return None and run nothing.
"""

import os
import subprocess

from .git import run_git

SHARED_PATH = ".agents/skills/shared"
CATALOG_URL = "https://github.com/kennylhilljr/skills-catalog.git"

URL_NOT_ALLOWED = "submodule URL not allowed"
MISSING = "shared skills missing"
DIRTY = "edit inside shared skills"

# Test seam: tests point ALLOWED_URL at a local bare repository and set
# ALLOW_FILE_PROTOCOL so the fetch may use file://. Production never changes
# either; `update_argv()` with the defaults is what runs.
ALLOWED_URL = CATALOG_URL
ALLOW_FILE_PROTOCOL = False

UPDATE_TIMEOUT = 300


class SharedSkillsError(RuntimeError):
    """The submodule check failed; `reason` is one of the fixed reasons."""

    def __init__(self, reason, detail=""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


def submodule_entry(worktree):
    """(name, url) of the `.agents/skills/shared` entry in the worktree's
    `.gitmodules`, or None when there is no such entry."""
    path = os.path.join(worktree, ".gitmodules")
    if not os.path.isfile(path):
        return None
    proc = _git(["config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"], worktree)
    if proc.returncode not in (0, 1):  # 1 is "no match"; anything else is an unreadable file
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                mentioned = SHARED_PATH in fh.read()
        except OSError:
            mentioned = True
        if mentioned:  # fail closed rather than run without the shared skills
            raise SharedSkillsError(MISSING, f".gitmodules cannot be read: {proc.stderr.strip()[-200:]}")
        return None
    names = []
    for line in proc.stdout.splitlines():
        key, _, value = line.partition(" ")
        if value.strip().strip("/") == SHARED_PATH:
            names.append(key[len("submodule."):-len(".path")])
    if not names:
        return None
    if len(names) > 1:
        # Ambiguous: git may map the path to a different entry than the one checked.
        return names[0], "<duplicate .gitmodules entries for the path>"
    urls = _git(["config", "--file", ".gitmodules", "--get-all", f"submodule.{names[0]}.url"],
                worktree).stdout.splitlines()
    return names[0], urls[0].strip() if len(urls) == 1 else "<missing or repeated url>"


def _git(args, cwd, timeout=60, **kw):
    """run_git that reports a timeout as a failed process instead of raising."""
    try:
        return run_git(args, cwd, check=False, timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"timed out after {timeout} s")


def _check_url(entry):
    name, url = entry
    if url != ALLOWED_URL or not name or any(ch in name for ch in "=\n\r"):
        raise SharedSkillsError(URL_NOT_ALLOWED, f"{SHARED_PATH} points at {url or '<none>'}")


def protocol_config():
    """Transport settings for the fetch: https only (SUB-7)."""
    return [("protocol.file.allow", "always" if ALLOW_FILE_PROTOCOL else "never"),
            ("protocol.ext.allow", "never"),
            ("protocol.allow", "never"),
            ("protocol.https.allow", "always"),
            ("submodule.recurse", "false")]


def update_argv(name, fallback=False):
    """The git arguments (after `git` and the safety overrides) that
    initialize the shared submodule. The URL is pinned on the command line so
    a stale `submodule.<name>.url` in the repository config cannot redirect
    the fetch. The fallback clears credential helpers and extra auth headers,
    for a fetch that the inherited auth made GitHub refuse."""
    args = []
    for key, value in protocol_config():
        args += ["-c", f"{key}={value}"]
    args += ["-c", f"submodule.{name}.url={ALLOWED_URL}"]
    if fallback:
        args += ["-c", "credential.helper=", "-c", "http.extraHeader="]
        host = ALLOWED_URL.split("//", 1)[-1].split("/", 1)[0]
        for scope in (f"https://{host}", f"https://{host}/", ALLOWED_URL):
            args += ["-c", f"credential.{scope}.helper=", "-c", f"http.{scope}.extraHeader="]
    return args + ["submodule", "update", "--init", "--depth", "1", "--", SHARED_PATH]


def init_shared_skills(worktree):
    """Initialize the shared skills submodule in a fresh worktree and check
    it. Returns the pinned catalog commit, or None without an entry. Raises
    SharedSkillsError with the check's reason."""
    entry = submodule_entry(worktree)
    if entry is None:
        return None
    _check_url(entry)
    proc = _git(update_argv(entry[0]), worktree, timeout=UPDATE_TIMEOUT)
    if proc.returncode != 0:
        proc = _git(update_argv(entry[0], fallback=True), worktree, timeout=UPDATE_TIMEOUT)
    if proc.returncode != 0:
        raise SharedSkillsError(MISSING, (proc.stderr or proc.stdout).strip()[-300:])
    return check_shared_skills(worktree)


def _status_line(worktree):
    proc = _git(["submodule", "status", "--", SHARED_PATH], worktree, config={"submodule.recurse": "false"})
    if proc.returncode != 0:
        raise SharedSkillsError(MISSING, f"git submodule status: {proc.stderr.strip()[-300:]}")
    out = proc.stdout
    lines = [line for line in out.splitlines() if line.strip()]
    return lines[0] if lines else ""


def check_shared_skills(worktree):
    """The submodule check (DESIGN 4.2 steps 1 to 3). Returns the pinned
    catalog commit, or None without an entry. Raises SharedSkillsError."""
    entry = submodule_entry(worktree)
    if entry is None:
        return None
    _check_url(entry)
    catalog = os.path.join(worktree, SHARED_PATH, "catalog.json")
    if not os.path.isfile(catalog) or os.path.islink(catalog):
        raise SharedSkillsError(MISSING, f"{SHARED_PATH}/catalog.json is missing")
    line = _status_line(worktree)
    if not line or line[0] != " ":
        raise SharedSkillsError(MISSING, f"git submodule status: {line.strip() or '<empty>'}")
    commit = line[1:].split()[0]
    if len(commit) != 40:
        raise SharedSkillsError(MISSING, f"git submodule status: {line.strip()}")
    return commit


def _gitlink(worktree, rev=None):
    """The commit pinned at SHARED_PATH in `rev`'s tree, or in the index
    when `rev` is None; None when there is no gitlink there."""
    args = ["ls-tree", rev, "--", SHARED_PATH] if rev else ["ls-files", "-s", "--", SHARED_PATH]
    for line in _git(args, worktree).stdout.splitlines():
        fields = line.split("\t", 1)[0].split()
        if fields and fields[0] == "160000":
            return fields[2] if rev else fields[1]
    return None


def dirty_shared_skills(worktree, reference=None):
    """The post-unit check (SUB-10). Returns (DIRTY, detail) when the agent
    wrote inside the submodule or moved its commit, else None. `reference`
    is a commit whose gitlink the unit must keep (the merge base with the
    target): a committed or staged gitlink change is then caught even when
    the agent also moved the submodule checkout to match it."""
    pinned = _gitlink(worktree, reference) if reference else None
    if pinned is None:
        try:
            if submodule_entry(worktree) is None:
                return None
        except SharedSkillsError as exc:
            return DIRTY, exc.detail
    else:
        for label, value in (("committed", _gitlink(worktree, "HEAD")), ("staged", _gitlink(worktree))):
            if value != pinned:
                return DIRTY, f"{label} gitlink of {SHARED_PATH} changed from {pinned[:12]} to {value}"
    sub = os.path.join(worktree, SHARED_PATH)
    if not os.path.lexists(os.path.join(sub, ".git")):
        # Not initialized: never run git there, it would report the parent.
        if os.path.isdir(sub) and os.listdir(sub):
            return DIRTY, f"{SHARED_PATH} holds files but is not a checkout"
        return None
    proc = _git(["status", "--porcelain"], sub)
    if proc.returncode != 0:
        return DIRTY, f"git status failed in {SHARED_PATH}: {proc.stderr.strip()[-200:]}"
    if proc.stdout.strip():
        files = [line[3:] for line in proc.stdout.splitlines() if line.strip()]
        return DIRTY, ", ".join(files[:20])
    try:
        line = _status_line(worktree)
    except SharedSkillsError as exc:
        return DIRTY, exc.detail
    if not line.startswith(" "):
        return DIRTY, f"{SHARED_PATH} moved off its pinned commit: {line.strip()}"
    head = _git(["rev-parse", "HEAD"], sub).stdout.strip()
    if pinned and head != pinned:
        return DIRTY, f"{SHARED_PATH} checkout is at {head[:12]}, pinned {pinned[:12]}"
    return None
