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
            except OSError:
                continue
            if os.path.isabs(dest):
                continue
            if _inside(_norm(os.path.join(LINK_DIR, dest)), SHARED_ROOT):
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


def catalog_entries(lessons, roots):
    """Ledger entries for the lessons that belong to the catalog, in lesson order."""
    out = []
    for x in lessons or []:
        skill = shared_skill(x.get("practice"), roots)
        if skill:
            out.append({"id": x.get("id"), "skill": skill, "lesson": x.get("what"), "unit": x.get("unit")})
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


def exclude_shared(globs, targets):
    out = []
    for g in globs or []:
        parts = [g]
        for t in targets:
            parts = [x for p in parts for x in _exclude(p, t)]
        for p in parts:
            if p not in out:
                out.append(p)
    return out


def filter_ownership(ownership, roots):
    """Each role's practice globs with the submodule and the shared links removed,
    whatever the configured ownership says."""
    targets = protected(roots)
    return {role: exclude_shared(globs, targets) for role, globs in (ownership or {}).items()}


# == the applier's guard ==
def refused_paths(changed, roots):
    targets = protected(roots)
    return [f for f in changed or [] if is_shared_path(f, targets)]


def shared_snapshot(root):
    """{relative path: sha256} of the files under the submodule folder, so a
    write the parent repository cannot see (DESIGN 4.5) is still caught."""
    out = {}
    top = os.path.join(root, SHARED_ROOT)
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            if name == ".git":
                continue
            full = os.path.join(dirpath, name)
            try:
                if os.path.islink(full):
                    digest = "link:" + os.readlink(full)
                else:
                    with open(full, "rb") as fh:
                        digest = hashlib.sha256(fh.read()).hexdigest()
            except OSError:
                digest = "unreadable"
            out[os.path.relpath(full, root).replace(os.sep, "/")] = digest
    return out


# == the ledger section (SUB-11) ==
def ledger_line(entry, at=None):
    return (f"- {entry['id']}{' ' + at if at else ''}, unit {entry.get('unit') or '-'}, skill "
            f"`{entry['skill']}`: {entry['lesson']}")


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
    listed = {m.group(1) for ln in section for m in [re.match(r"^- (L\d+)\b", ln)] if m}
    new = [e for e in entries if e.get("id") not in listed]
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
    return f"lesson: {skill}: {n} lesson{'' if n == 1 else 's'} from {repo} {milestone}"


def issue_body(repo, milestone, skill, entries, unit_link=None):
    lines = [f"Lessons the agent team recorded about the shared skill `{skill}` while building {repo} "
             f"{milestone}. They are quoted verbatim. Each one names the unit it came from.", ""]
    for e in entries:
        link = (unit_link or (lambda u: None))(e.get("unit"))
        unit = f"[{e.get('unit')}]({link})" if link else f"`{e.get('unit') or '-'}`"
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


def repo_slug(repo, origin_url=None):
    """`owner/name` from the origin remote URL of `repo`, else the folder name."""
    m = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$", str(origin_url or "").strip())
    return m.group(1) if m else os.path.basename(os.path.abspath(repo))
