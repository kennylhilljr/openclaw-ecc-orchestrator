"""Lessons about shared skills go to the skills catalog, never into the submodule
(skills-catalog SPEC SUB-11 and SUB-12, DESIGN sections 4.5 and 6).

A consumer repository pins the shared skills as a git submodule at
`.agents/skills/shared`, and Claude Code sees them through in-repository links
`.claude/skills/<name>` that resolve into it. An edit there lands in the pinned
checkout and is lost, or is committed on a detached submodule HEAD. So:

* the practices steward's ownership map never covers the submodule or a shared
  link (`filter_ownership`), and the applier refuses such a path anyway
  (`refused_paths`, `shared_snapshot`);
* a lesson whose practice resolves to a shared skill (`shared_skill`) is
  written by the harness under "Proposed for the catalog" in the ledger
  (`write_ledger`);
* when the milestone ends, the entries are grouped by skill and one `lesson`
  issue per skill is opened on the catalog (`file_issues`). A marker in the
  body makes a rerun open nothing new. The opener is injected; the default
  (`GhIssues`) runs `gh issue create` with the ambient or configured identity.
"""

import collections
import hashlib
import os
import re
import subprocess

SHARED_ROOT = ".agents/skills/shared"
SHARED_SKILLS = SHARED_ROOT + "/skills"
LINK_DIR = ".claude/skills"
SECTION = "Proposed for the catalog"
CATALOG_REPO = "kennylhilljr/skills-catalog"
LABEL = "lesson"
_WILD = "*?["


def _norm(path):
    p = str(path or "").strip().strip("`'\"").strip()
    while p.startswith("./"):
        p = p[2:]
    p = os.path.normpath(p) if p else ""
    return "" if p in (".", "") else p.replace(os.sep, "/")


def _inside(path, target):
    return path == target or path.startswith(target + "/")


# == what is shared ==
def shared_links(roots):
    """{name: ".claude/skills/<name>"} for every link under `.claude/skills` in
    any of `roots` that resolves into the shared submodule."""
    out = {}
    for root in roots or ():
        base = os.path.join(root, LINK_DIR)
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            full = os.path.join(base, name)
            if not os.path.islink(full):
                continue
            try:
                dest = os.readlink(full)
                real = os.path.realpath(full)
            except OSError:
                continue
            shared_real = os.path.realpath(os.path.join(root, SHARED_ROOT))
            # Relative links by their text (they may dangle without the submodule), and any
            # link (absolute or chained) by where it resolves.
            if (not os.path.isabs(dest) and _inside(_norm(os.path.join(LINK_DIR, dest)), SHARED_ROOT)) or \
                    real == shared_real or real.startswith(shared_real + os.sep):
                out[name] = f"{LINK_DIR}/{name}"
    return out


def shared_names(roots):
    """Names of the shared skills: folders in the initialized submodule plus the shared links."""
    names = set(shared_links(roots))
    for root in roots or ():
        try:
            names.update(n for n in os.listdir(os.path.join(root, SHARED_SKILLS))
                         if not n.startswith(".") and os.path.isdir(os.path.join(root, SHARED_SKILLS, n)))
        except OSError:
            pass
    return names


def protected(roots):
    """Paths the steward may never change: the submodule and the shared links."""
    return [SHARED_ROOT] + sorted(shared_links(roots).values())


def is_shared_path(path, targets):
    p = _norm(path)
    return any(_inside(p, t) for t in targets)


def shared_skill(practice, roots):
    """The shared skill a lesson's practice resolves to, or None.

    Resolves `.agents/skills/shared/skills/<name>/...`, a shared
    `.claude/skills/<name>` link, and `.agents/skills/<name>` or a bare `<name>`
    when `<name>` is a shared skill with no project folder of that name (shared
    names are reserved in consumers, DESIGN 4.3)."""
    p = _norm(practice)
    if not p or p.startswith("../"):
        return None
    if p.startswith(SHARED_SKILLS + "/"):
        name = p[len(SHARED_SKILLS) + 1:].split("/")[0]
        return name or None
    names = shared_names(roots)
    parts = p.split("/")
    if p.startswith(LINK_DIR + "/"):
        name = parts[2]
        return name if name in shared_links(roots) else None
    if p.startswith(".agents/skills/") and len(parts) >= 3:
        name = parts[2]
    elif len(parts) == 1:
        name = parts[0]
    else:
        return None
    if name not in names or name == "shared":
        return None
    if any(os.path.isdir(os.path.join(r, ".agents", "skills", name)) and
           not os.path.islink(os.path.join(r, ".agents", "skills", name)) for r in roots or ()):
        return None  # a project folder with that name is the project's own skill
    return name


def catalog_entries(lessons, roots, run=None):
    """Ledger entries for the lessons that belong to the catalog, in lesson order.
    `run` is the drive's run id: lesson ids restart in every run."""
    out = []
    for x in lessons or []:
        skill = shared_skill(x.get("practice"), roots)
        if skill:
            out.append({"id": x.get("id"), "run": run, "skill": skill, "lesson": x.get("text") or x.get("what"),
                        "unit": x.get("unit")})
    return out


# == the ownership filter (SUB-11) ==
def _exclude(glob, target):
    """`glob` rewritten into fnmatch globs that match what it matched, minus
    `target` and everything under it."""
    marks = [glob.find(ch) for ch in _WILD if ch in glob]
    if not marks:
        # A literal folder also stands for everything under it (learning.Steward._matches),
        # so an ancestor of the target goes too.
        lit = _norm(glob)
        return [] if _inside(lit, target) or target.startswith(lit + "/") else [glob]
    i = min(marks)
    prefix = glob[:i]
    inner = target + "/"
    k = min(len(prefix), len(inner))
    if prefix[:k] != inner[:k]:
        return [glob]  # every match diverges from the target before its first wildcard
    if len(prefix) >= len(inner):
        return []  # every match is under the target
    rest = inner[len(prefix):]  # what still has to match for a path to be under the target
    w = glob[i]
    if w == "[":
        close = glob.find("]", i + 2)
        body = glob[i + 1:close] if close != -1 else ""
        tail = glob[close + 1:]
        neg = body.startswith("!")
        chars = body[1:] if neg else body
        c = rest[0]
        if close == -1 or not chars or "-" in chars[1:-1] or "[" in chars or "\\" in chars:
            return []  # a class this does not parse: drop it rather than guess
        if (c in chars) == (not neg):
            # the class can be c: split it into "not c" and "exactly c"
            other = ("[!" + chars + c + "]") if neg else ("[" + chars.replace(c, "") + "]" if chars.replace(c, "") else None)
            return ([prefix + other + tail] if other else []) + _exclude(prefix + c + tail, target)
        return [glob]  # the class never matches the next target character
    if w == "?":
        tail = glob[i + 1:]
        return [prefix + _not(rest[0]) + tail] + _exclude(prefix + rest[0] + tail, target)
    j = i
    while j < len(glob) and glob[j] == "*":
        j += 1
    tail = glob[j:]
    out = []
    for n in range(len(rest)):
        # the star's match diverges from `rest` at position n...
        out.append(prefix + rest[:n] + _not(rest[n]) + "*" + tail)
        # ...or is a proper prefix of `rest` and the tail continues from there
        out += _exclude(prefix + rest[:n] + tail, target)
    return out


def _not(ch):
    return "[!" + ch + "]"  # target paths hold only letters, digits, ".", "-", "_" and "/"


def _safe(glob, targets):
    """True when neither `glob` nor the folder form the applier also accepts
    (`learning.Steward._matches`: the glob without trailing "/" and "*", plus
    "/*") can match a target."""
    wide = glob.rstrip("/*").rstrip("/")
    forms = [glob] + ([wide + "/*"] if wide else ["*"])
    return all(_exclude(f, t) == [f] for f in forms for t in targets)


def exclude_shared(globs, targets):
    out = []
    for g in globs or []:
        parts = [g]
        for t in targets:
            parts = [x for p in parts for x in _exclude(p, t)]
        for p in parts:
            if p not in out and _safe(p, targets):
                out.append(p)
    return out


def filter_ownership(ownership, roots):
    """Each role's practice globs with the submodule and the shared links removed,
    whatever the configured ownership says."""
    targets = protected(roots)
    return {role: exclude_shared(globs, targets) for role, globs in (ownership or {}).items()}


# == the applier's guard ==
def refused_paths(changed, roots, known=()):
    """The changed paths inside the shared skills. `known` adds targets taken
    before the steward ran, so a link it deleted or retargeted still counts."""
    targets = list(dict.fromkeys(list(known or ()) + protected(roots)))
    return [f for f in changed or [] if is_shared_path(f, targets)]


def shared_snapshot(root):
    """{relative path: fingerprint} of everything under the submodule folder
    (content, mode, link targets, empty folders), so a write the parent
    repository cannot see (DESIGN 4.5) is still caught. An initialized
    submodule's own `git status --porcelain` is included too."""
    out = {}
    top = os.path.join(root, SHARED_ROOT)
    for dirpath, dirnames, filenames in os.walk(top):
        if ".git" in dirnames:
            dirnames.remove(".git")
        rel = os.path.relpath(dirpath, root).replace(os.sep, "/")
        try:
            out[rel + "/"] = "dir:%o" % os.lstat(dirpath).st_mode
        except OSError:
            out[rel + "/"] = "unreadable"
        for name in list(dirnames) + filenames:
            if name == ".git" and dirpath == top:
                continue
            full = os.path.join(dirpath, name)
            key = os.path.relpath(full, root).replace(os.sep, "/")
            try:
                st = os.lstat(full)
                if os.path.islink(full):
                    out[key] = "link:" + os.readlink(full)
                elif os.path.isdir(full):
                    continue  # walked on its own
                else:
                    with open(full, "rb") as fh:
                        out[key] = "%o:%s" % (st.st_mode, hashlib.sha256(fh.read()).hexdigest())
            except OSError:
                out[key] = "unreadable"
    if os.path.exists(os.path.join(top, ".git")):
        try:
            proc = subprocess.run(["git", "-C", top, "status", "--porcelain", "--ignored"], capture_output=True,
                                  text=True, timeout=60, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                                              "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"})
            out[" git status"] = proc.stdout if proc.returncode == 0 else f"status failed {proc.returncode}"
            head = subprocess.run(["git", "-C", top, "rev-parse", "HEAD"], capture_output=True, text=True,
                                  timeout=60, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                                   "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"})
            out[" git head"] = head.stdout.strip() if head.returncode == 0 else f"head failed {head.returncode}"
        except (OSError, subprocess.SubprocessError):
            out[" git status"] = "status failed"
    return out


def shared_state(roots):
    """`shared_snapshot` of every root: the steward's worktree and the operator's
    checkout, since an absolute or chained link can reach either submodule."""
    return {r: shared_snapshot(r) for r in dict.fromkeys(os.path.abspath(x) for x in roots or ())}


def restore_shared(root, before):
    """Put the initialized submodule at `root` back to the state `before` (a
    `shared_snapshot`) when that state was a clean checkout, which makes the
    restore lossless. Returns True when the snapshot matches `before` again."""
    top = os.path.join(root, SHARED_ROOT)
    head = (before or {}).get(" git head")
    if not os.path.exists(os.path.join(top, ".git")) or (before or {}).get(" git status") != "" or \
            not re.fullmatch(r"[0-9a-f]{40,64}", str(head or "")):
        return False
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"}
    try:
        for args in (["reset", "-q", "--hard", head], ["clean", "-qfdx"]):
            subprocess.run(["git", "-C", top] + args, capture_output=True, text=True, timeout=120, env=env)
    except (OSError, subprocess.SubprocessError):
        return False
    return shared_snapshot(root) == before


# == the ledger section (SUB-11) ==
def ledger_target(tree, rel, roots):
    """The absolute path the harness may write the ledger at, or None. `rel`
    must be a relative path inside `tree`, outside the submodule and the
    shared links, with no symlink on the way (so the write cannot land on the
    host or inside a shared skill)."""
    raw = str(rel or "")
    p = _norm(raw)
    if not p or os.path.isabs(raw) or p == ".." or p.startswith("../") or "/../" in "/" + p + "/":
        return None
    if is_shared_path(p, protected(list(roots or ()) + [tree])):
        return None
    tree_real = os.path.realpath(tree)
    cur = tree
    for part in p.split("/"):
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            return None
    full = os.path.join(tree, p)
    real = os.path.realpath(full)
    if not (real == tree_real or real.startswith(tree_real + os.sep)):
        return None
    for r in list(roots or ()) + [tree]:
        shared_real = os.path.realpath(os.path.join(r, SHARED_ROOT))
        if real == shared_real or real.startswith(shared_real + os.sep):
            return None
    return full


def ledger_line(entry, at=None):
    text = "\n  ".join(str(entry["lesson"]).splitlines() or [""])  # continuation lines stay in the list item
    run = f", run {entry['run']}" if entry.get("run") else ""
    return (f"- {entry['id']}{' ' + at if at else ''}{run}, unit {entry.get('unit') or '-'}, skill "
            f"`{entry['skill']}`: {text}")


def write_ledger(path, entries, at=None):
    """Add the entries under "Proposed for the catalog" in the ledger at `path`
    (created when missing). Entries whose id is already listed are skipped.
    Returns the ids added."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        lines = ["# Lessons ledger", "", "## Applied", "", "## Proposed for the owner", ""]
    head = re.compile(r"^(#+)\s+" + re.escape(SECTION) + r"\s*$")
    start = next((n for n, ln in enumerate(lines) if head.match(ln)), None)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [f"## {SECTION}", ""]
        start = len(lines) - 2
    level = len(head.match(lines[start]).group(1))
    end = len(lines)
    for n in range(start + 1, len(lines)):
        m = re.match(r"^(#+)\s", lines[n])
        if m and len(m.group(1)) <= level:
            end = n
            break
    section = lines[start + 1:end]
    listed = set()
    for ln in section:
        m = re.match(r"^- (L\d+)\b(?:[^,]*)(?:, run ([^,]+))?", ln)
        if m:
            listed.add((m.group(1), m.group(2)))
    new = [e for e in entries if (e.get("id"), e.get("run")) not in listed]
    if not new:
        return []
    while section and not section[-1].strip():
        section.pop()
    if not section:
        section = [""]
    section += [ledger_line(e, at) for e in new] + [""]
    lines = lines[:start + 1] + section + lines[end:]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip("\n") + "\n")
    return [e["id"] for e in new]


# == the lesson issues (SUB-12) ==
def marker(repo, milestone, skill):
    return f"<!-- catalog-lesson: {repo} {milestone} {skill} -->"


def issue_title(repo, milestone, skill, n):
    return f"lesson: {skill}: {n} lessons from {repo} {milestone}"  # the SUB-12 template, also for n == 1


def issue_body(repo, milestone, skill, entries, unit_link=None):
    lines = [f"Lessons the agent team recorded about the shared skill `{skill}` while building {repo} "
             f"{milestone}. They are quoted verbatim. Each one names the unit it came from.", ""]
    for e in entries:
        link = (unit_link or (lambda u: None))(e.get("unit"))
        if link and str(link).startswith("https://"):
            unit = f"[{e.get('unit')}]({link})"
        else:
            unit = f"`{e.get('unit') or '-'}`" + (f" ({link})" if link else "")
        lines += [f"### {e['id']}, unit {unit}", ""] + [f"> {ln}" for ln in str(e["lesson"]).splitlines() or [""]] + [""]
    lines += ["Opened by the orchestrator's practices steward (skills-catalog SUB-12). It makes no edit inside "
              "the submodule; the change reaches consumers through the next bump PR.", "",
              marker(repo, milestone, skill), ""]
    return "\n".join(lines)


def file_issues(entries, *, repo, milestone, opener, exists, unit_link=None, filed=None):
    """Open one issue per skill for `entries`. `opener(title, body, label)`
    returns the issue URL; `exists(marker)` says whether an issue with that
    marker is already open or closed on the catalog. `filed` ({marker: url})
    records what this drive already opened. Returns
    [{"skill", "marker", "status", "url"?, "error"?}]."""
    filed = {} if filed is None else filed
    groups = collections.OrderedDict()
    for e in entries or []:
        groups.setdefault(e["skill"], [])
        if all(x.get("id") != e.get("id") for x in groups[e["skill"]]):
            groups[e["skill"]].append(e)
    results = []
    for skill, items in groups.items():
        mark = marker(repo, milestone, skill)
        res = {"skill": skill, "marker": mark, "lessons": [e["id"] for e in items]}
        if mark in filed:
            results.append(dict(res, status="already filed", url=filed[mark]))
            continue
        try:
            found = exists(mark)
            if found:
                filed[mark] = found if isinstance(found, str) else ""
                results.append(dict(res, status="already filed", url=filed[mark]))
                continue
            url = opener(issue_title(repo, milestone, skill, len(items)),
                         issue_body(repo, milestone, skill, items, unit_link), LABEL)
        except Exception as exc:  # noqa: BLE001 - a failed opener must not stop the milestone
            results.append(dict(res, status="error", error=f"{type(exc).__name__}: {str(exc)[:300]}"))
            continue
        filed[mark] = str(url or "")
        results.append(dict(res, status="opened", url=filed[mark]))
    return results


class GhIssues:
    """The default opener: `gh` against the catalog. `token_env` names an
    environment variable holding the token of the identity that opens the
    issues (passed to gh as GH_TOKEN); without it gh uses its own login."""

    def __init__(self, repo=CATALOG_REPO, token_env=None, gh="gh", run=subprocess.run):
        self.repo = repo
        self.token_env = token_env
        self.gh = gh
        self._run = run

    def _env(self):
        env = dict(os.environ)
        if self.token_env:
            token = os.environ.get(self.token_env)
            if not token:
                raise RuntimeError(f"{self.token_env} is not set")
            env["GH_TOKEN"] = token
        env["GH_PROMPT_DISABLED"] = "1"
        return env

    def _gh(self, args, stdin=None):
        proc = self._run([self.gh] + args, input=stdin, capture_output=True, text=True, env=self._env(),
                         timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"gh {args[0]} {args[1]} failed ({proc.returncode}): {(proc.stderr or '').strip()[:300]}")
        return proc.stdout

    def exists(self, mark):
        import json
        out = self._gh(["issue", "list", "-R", self.repo, "--label", LABEL, "--state", "all", "--limit", "1000",
                        "--json", "url,body"])
        for item in json.loads(out or "[]"):
            if mark in (item.get("body") or ""):
                return item.get("url") or True
        return False

    def open(self, title, body, label=LABEL):
        out = self._gh(["issue", "create", "-R", self.repo, "--label", label, "--title", title,
                        "--body-file", "-"], stdin=body)
        return out.strip().splitlines()[-1] if out.strip() else ""


def published(repo, commit):
    """True when `commit` is on a remote-tracking branch of `origin` (the remote
    the issue links name), so a link to it resolves for someone reading the issue."""
    try:
        proc = subprocess.run(["git", "-C", repo, "for-each-ref", "--contains", commit, "--count", "1",
                               "--format", "%(refname)", "refs/remotes/origin"], capture_output=True, text=True,
                              timeout=60, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                               "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


def commit_ref(repo, repo_name, commit):
    """A unit's reference in an issue: the GitHub commit URL once the merge commit
    is pushed, else its short id marked as not pushed yet (a link would 404)."""
    if not commit:
        return None
    if "/" in str(repo_name or "") and published(repo, commit):
        return f"https://github.com/{repo_name}/commit/{commit}"
    return f"commit {str(commit)[:12]}, not on a pushed branch when this issue was opened"


def repo_slug(repo, origin_url=None):
    """`owner/name` from the origin remote URL of `repo`, else the folder name."""
    m = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$", str(origin_url or "").strip())
    return m.group(1) if m else os.path.basename(os.path.abspath(repo))
