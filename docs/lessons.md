# Lessons and the practices steward

While `ecc drive` builds a milestone, the personas record lessons: reviewers
and the verifier through the `lessons` list in their verdicts, coders through
`Lesson:` lines in their handoff notes, and the gates through failures. They
are appended to `<state_dir>/lessons/<run_id>.jsonl` (`team/learning.py`,
`Lessons`).

Every few merged units, and once at milestone end, the delivery lead acts as
practices steward (`Steward` in `team/learning.py`, prompt in
`team/personas.py` `steward_prompt`). On its own branch it turns the lessons
that generalize into edits to the practice files each persona owns (the
`ownership` map in team.json's `learning` block), and records every lesson in
the ledger (`ledger`, default `.agents/LESSONS.md`) under "Applied" or
"Proposed for the owner". The owning personas review the change, the gates
run on the merged result, and the target branch is fast forwarded.

## Shared skills (skills-catalog SUB-11, SUB-12)

A consumer repository can pin the shared skills catalog as a git submodule at
`.agents/skills/shared`, with in-repository links `.claude/skills/<name>` that
resolve into it. An edit there lands in the pinned checkout and is lost, or is
committed on a detached submodule HEAD. So lessons about shared skills go to
the catalog as issues instead (`team/catalog_lessons.py`):

1. **Ownership filter.** Before the steward prompt is built, every role's
   globs are rewritten so that none matches `.agents/skills/shared` or
   anything under it, or a `.claude/skills/<name>` link that resolves into
   it, whatever the configured ownership says. A grant such as
   `.agents/skills/**` keeps covering the project's own skills.
2. **Applier guard.** The steward's change is rejected when it touches the
   submodule gitlink, a path under it or a shared link, or when any file in
   the submodule folder of the steward's worktree or of the operator's
   checkout changed (git does not show such writes in the parent's diff).
   Links are taken before the steward runs, so deleting or retargeting one
   is refused too. When the operator's submodule was a clean checkout before
   the turn, it is reset to that state; otherwise the change is reported in
   the drive progress for the operator to fix.
3. **Ledger section.** A lesson whose `practice` resolves to a shared skill
   (a path under `.agents/skills/shared/skills/<name>/`, a shared
   `.claude/skills/<name>` link, or `.agents/skills/<name>` or `<name>` when
   that is a shared skill name and the project has no folder of that name) is
   not given to the steward. The harness writes it under "Proposed for the
   catalog" in the ledger with the lesson id, date, unit, skill name and the
   lesson text verbatim, and commits that on a branch of its own. The ledger
   path must be a relative path inside the worktree with no symlink on the
   way and outside the shared skills, else nothing is written. The
   steward prompt names the section and forbids editing shared skills.
4. **Lesson issues.** At milestone end the entries are grouped by skill, and
   one issue per skill is opened on the catalog with the label `lesson`, the
   title `lesson: <skill>: <n> lessons from <repo> <milestone>`, the lessons
   quoted verbatim (up to 4000 characters each), a link to each unit's merge
   commit once that commit is on a pushed branch (until then its short id,
   marked as not pushed), and the marker
   `<!-- catalog-lesson: <repo> <milestone> <skill> -->`. Before opening, the
   catalog's `lesson` issues (open and closed) are searched for the marker,
   and the drive state records what it opened, so a rerun opens nothing new.
   Nothing is written inside the submodule and no pull request is opened on
   the catalog. A failed opener is reported in the drive progress and state
   and retried on the next milestone end; it does not stop the milestone.

`<repo>` is `owner/name` from the target repository's `origin` remote (the
folder name when there is none); `<milestone>` is team.json's
`milestone.name`.

### Settings

In team.json's `learning` block:

| Key | Default | Meaning |
| :- | :- | :- |
| `catalog_issues` | `true` | open the lesson issues at milestone end; `false` keeps the entries in the ledger only |
| `catalog_repo` | `kennylhilljr/skills-catalog` | where the issues are opened |
| `catalog_token_env` | none | name of an environment variable holding the token of the identity that opens the issues, passed to `gh` as `GH_TOKEN`; without it `gh` uses its own login |

The opener is injectable: a `Drive` with a `catalog_issues` attribute (an
object with `open(title, body, label)` returning the issue URL and
`exists(marker)`) uses it instead of the default `GhIssues`, which runs
`gh issue list` and `gh issue create -R <catalog_repo> --label lesson`. The
skills-catalog M3 decision is that iwb's bot App opens these issues, so only
configuration changes when that identity is wired in.
