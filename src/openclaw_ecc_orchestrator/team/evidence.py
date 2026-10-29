"""The evidence pack (TEAM-DELIVERY-v2 section 3.2): everything a text-only
persona needs, so any certified provider can play the role."""

import os
import re

from ..worktrees.git import git_out, run_git

EXCLUDE_FROM_DIFF = ("uv.lock", "poetry.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock",
                     "docs/generated/**")
MAX_DIFF_CHARS = 60000
MAX_DOC_CHARS = 30000
MAX_RULES_CHARS = 6000

_DOC = re.compile(r"\b((?:docs|\.orchestration)/[\w./-]+\.md)\b")
_SECTIONS = re.compile(r"sections?\s*\(?\s*((?:\d+(?:\.\d+)*)(?:\s*(?:,|and)\s*\d+(?:\.\d+)*)*)", re.I)
_HEADING = re.compile(r"^(#{1,4})\s+(\d+(?:\.\d+)*)\.?\s", re.M)


def estimate_tokens(text):
    return len(text or "") // 4 + 1


def _clip(text, limit, label):
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[{label} truncated at {limit} characters of {len(text)}]"


def section_numbers(description):
    found = []
    for m in _SECTIONS.finditer(description or ""):
        found += re.findall(r"\d+(?:\.\d+)*", m.group(1))
    return list(dict.fromkeys(found))


def extract_sections(text, numbers):
    """The markdown sections whose heading number is in `numbers` (with their
    subsections)."""
    if not numbers:
        return ""
    heads = [(m.start(), len(m.group(1)), m.group(2)) for m in _HEADING.finditer(text)]
    out = []
    for i, (start, level, num) in enumerate(heads):
        if not any(num == n or num.startswith(n + ".") for n in numbers):
            continue
        end = len(text)
        for later_start, later_level, _ in heads[i + 1:]:
            if later_level <= level:
                end = later_start
                break
        out.append(text[start:end].rstrip())
    # A parent section already contains its matched children; keep unique text.
    unique = []
    for chunk in out:
        if not any(chunk in other for other in out if other is not chunk and len(other) > len(chunk)):
            unique.append(chunk)
    return "\n\n".join(unique)


def design_references(worktree, description):
    numbers = section_numbers(description)
    parts = []
    for rel in dict.fromkeys(_DOC.findall(description or "")):
        path = os.path.join(worktree, rel)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        picked = extract_sections(text, numbers)
        if picked:
            parts.append(f"### {rel} (sections {', '.join(numbers)})\n\n{picked}")
    return _clip("\n\n".join(parts), MAX_DOC_CHARS, "design references")


def repo_rules(worktree):
    for name in ("AGENTS.md", "CLAUDE.md"):
        path = os.path.join(worktree, name)
        if os.path.isfile(path):
            with open(path, encoding="utf-8", errors="replace") as fh:
                return _clip(fh.read(), MAX_RULES_CHARS, name)
    return ""


def diff_text(repo, base, head):
    excludes = [f":(exclude){p}" for p in EXCLUDE_FROM_DIFF]
    stat = git_out(["diff", "--stat", base, head], repo)
    proc = run_git(["diff", "--no-color", base, head, "--", ".", *excludes], repo, check=False)
    return stat, _clip(proc.stdout, MAX_DIFF_CHARS, "diff")


def unit_pack(repo, worktree, base, head, punit, verification=None, verifier=None):
    """(text, estimated_tokens) for one unit's review."""
    stat, diff = diff_text(repo, base, head)
    gates = (verification or {}).get("gates") or []
    gate_lines = [f"- {g.get('name')}: `{g.get('command')}` {g.get('status')} (exit {g.get('exit_code')})"
                  for g in gates]
    parts = [
        f"## Unit {punit.get('id')}: {punit.get('title', '')}",
        str(punit.get("description") or ""),
        "Scope (files the unit may change): " + ", ".join((punit.get("scope") or {}).get("files") or []),
        "Acceptance commands: " + "; ".join((punit.get("acceptance") or {}).get("commands") or []),
        f"Base commit: {base}\nVerified head: {head}",
        "## Repository rules\n\n" + (repo_rules(worktree) or "(none found)"),
        "## Design references\n\n" + (design_references(worktree, punit.get("description")) or "(none found)"),
        "## Gate results (run by the orchestrator on the verified head)\n\n" + ("\n".join(gate_lines) or "(none)"),
        "## Diff stat\n\n```\n" + stat + "\n```",
        "## Diff (lock files and generated files excluded)\n\n```diff\n" + diff + "\n```",
    ]
    if verifier:
        parts.append("## Verifier evidence\n\n" + str(verifier))
    text = "\n\n".join(parts)
    return text, estimate_tokens(text)
