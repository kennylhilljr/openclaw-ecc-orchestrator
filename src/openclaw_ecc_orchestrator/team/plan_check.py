"""The architect's milestone-start plan check, with or without a run (issue #8).

`ecc drive` runs this check after `create-run` has pinned the plan, so every
finding used to cost a plan version, an approval, a new run id and a paid
architect turn; research-mcp M1 went through five such rounds (rm-m1a to
rm-m1e), each reporting only part of what was wrong. `ecc plan-check` runs
the same check on a plan file. Results are cached by plan sha under
`<state_dir>/plan-checks/`, so an unchanged plan is not checked twice and the
drive skips its own check when the run's plan already passed.
"""

import json
import os

from ..runs.fsutil import atomic_write_json
from ..runs.store import iso
from . import evidence as E
from . import personas as P

CACHE_DIR = "plan-checks"

CATEGORIES = (
    "a unit whose scope misses a file it must create or change",
    "a missing depends_on: a unit uses a module, symbol, table, route or fixture another unit creates",
    "a symbol, table or file used by a unit before any unit (or the target tree) creates it",
    "composition wiring nobody owns: registering a module, a route, a CLI command or a migration in a "
    "shared entry point that no unit's scope includes",
    "a shared harness, CI, lint or test runner step a unit needs but cannot change within its scope",
    "plan text that contradicts an approved design document (LLD, contract, schema)",
    "scopes that overlap without an order between the units",
    "acceptance criteria or commands that cannot pass with the files in scope",
)


def prompt(pack, head):
    return "\n\n".join([
        "You are the architect on an agent delivery team. Before any unit of this milestone runs, check the "
        "plan against the design: every unit matches its design section, scopes do not overlap, dependencies "
        "are complete and in the right order, and each shared contract has exactly one owning unit that runs "
        "before its users. Report only problems that would make a unit fail or force rework.",
        "Make one exhaustive pass. Go through every unit for each category below and report every instance "
        "you find, not the first few: a partial list is a failed check, because each finding you leave out "
        "costs another plan revision and another check.\n" + "\n".join(f"- {c}" for c in CATEGORIES),
        P._SEVERITY_RULES,
        "End your reply with exactly one line of single-line JSON, nothing after it:\n"
        f'{P.REVIEW_MARK} {{"verdict": "approved|changes_requested|rejected", "head": "{head}", '
        '"findings": [{"severity": "blocker|major|minor", "where": "<unit or section>", "what": "<text>"}]}',
        "# Evidence\n\n" + pack])


def pack(repo, plan_units):
    """The plan's units and the design sections their descriptions name."""
    units = [{k: u.get(k) for k in ("id", "title", "depends_on", "scope", "acceptance", "risk")} for u in plan_units]
    refs = E.design_references(repo, " ".join(str(u.get("description") or "") for u in plan_units))
    return "## Plan\n\n```json\n" + json.dumps(units, indent=1) + "\n```\n\n## Design references\n\n" + refs


def cache_path(state_dir, plan_sha):
    return os.path.join(state_dir, CACHE_DIR, f"{plan_sha}.json")


def cached(state_dir, plan_sha):
    try:
        with open(cache_path(state_dir, plan_sha), encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("plan_sha256") == plan_sha else None


def store(state_dir, plan_sha, result, head, clock_now=None):
    """Cache a check that produced a verdict (a missing architect is not a result)."""
    if result.get("verdict") is None:
        return None
    doc = dict(result, plan_sha256=plan_sha, head=head)
    if clock_now is not None:
        doc["checked_at"] = iso(clock_now)
    path = cache_path(state_dir, plan_sha)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_write_json(path, doc)
    return doc


def passed(result):
    return result.get("verdict") == "approved"


def run_check(step, repo, plan_units, plan_sha, head, *, refresh=False):
    """(result, from_cache). Pays for an architect turn only when the plan has
    no cached verdict or `refresh` is set."""
    if not refresh:
        hit = cached(step.state_dir, plan_sha)
        if hit is not None:
            return hit, True
    evidence = pack(repo, plan_units)
    res = step.consult("architect", P.REVIEW_MARK, lambda: prompt(evidence, head), head,
                       run_id=f"plan-check-{plan_sha[:12]}")
    return store(step.state_dir, plan_sha, res, head, step.clock()) or res, False
