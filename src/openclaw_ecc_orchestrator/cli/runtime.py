"""Builds runtime components (store, manager, broker, worktrees, queue,
conductor) from a resolved CLI configuration, lazily and without creating
any file until a component actually writes.
"""

import copy
import json
import os

from .. import schemas
from ..merge_queue.queue import MergeQueue
from ..plugin.approvals import ApprovalBroker
from ..plugin.events import JsonlEventSink
from ..process.supervisor import Supervisor
from ..runs.conductor import Conductor
from ..runs.envelope import check, envelope
from ..runs.fsutil import read_json
from ..runs.manager import RunManager
from ..runs.store import RunStore
from ..worktrees.git import GitError, toplevel
from ..worktrees.manager import WorktreeError, WorktreeManager
from .config import check_state_outside_repo

MAX_DOC_BYTES = 4 * 1024 * 1024


class CommandFailed(Exception):
    """Carries a finished (failed) envelope out of a command handler."""

    def __init__(self, env):
        super().__init__(env.get("operation"))
        self.envelope = env


def fail(operation, name, detail="", actions=None, data=None):
    raise CommandFailed(envelope(operation, ok=False, checks=[check(name, False, str(detail))],
                                 required_user_actions=actions, data=data))


def read_document(path, label, operation):
    """Read a JSON document (duplicate keys and non finite numbers refused)."""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > MAX_DOC_BYTES:
            fail(operation, f"{label}_readable", f"{path} is not a regular file under {MAX_DOC_BYTES} bytes")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        fail(operation, f"{label}_readable", f"{type(exc).__name__} reading {path}")

    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError(f"duplicate key {key!r}")
            out[key] = value
        return out

    def constant(name):
        raise ValueError(f"non-finite number {name}")

    try:
        return text, json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, RecursionError) as exc:
        fail(operation, f"{label}_valid", f"{path} is not valid JSON: {exc}")


class Runtime:
    def __init__(self, cfg, clock):
        self.cfg = cfg
        self.clock = clock
        self._repo = None
        self._cache = {}

    # == repository and documents ==
    def repo_top(self, operation="config"):
        if self._repo is None:
            try:
                self._repo = toplevel(self.cfg.repo)
            except (GitError, OSError) as exc:
                fail(operation, "repo_is_git", f"{self.cfg.repo}: {getattr(exc, 'stderr', '') or exc}".strip())
            check_state_outside_repo(self.cfg, self._repo)
        return self._repo

    def policy_path(self):
        if self.cfg.policy_file:
            return self.cfg.policy_file
        default = getattr(self.cfg, "policy_default", None)
        return default if default and os.path.isfile(default) else None

    def load_policy(self, operation):
        """The repository policy (`.orchestration/config.yaml` style: JSON
        compatible YAML only), validated, or None when none is configured."""
        path = self.policy_path()
        if path is None:
            return None, None
        text, _ = read_document(path, "policy", operation)
        try:
            return schemas.load_repository_policy(text), path
        except schemas.DocumentError as exc:
            detail = "; ".join([str(exc)] + list(getattr(exc, "errors", []) or [])[:5])
            fail(operation, "policy_valid", f"{path}: {detail}")

    def certified(self, operation):
        if "certified" in self._cache:
            return self._cache["certified"]
        path = self.cfg.certifications
        if not path:
            self._cache["certified"] = ({}, [])
            return self._cache["certified"]
        _, doc = read_document(path, "certifications", operation)
        records = doc.get("records") if isinstance(doc, dict) else doc
        if not isinstance(records, list):
            fail(operation, "certifications_valid", "expected a list of certification records")
        from ..runners.registry import certified_runners
        try:
            certified, excluded = certified_runners(records, self.clock())
        except ValueError as exc:
            fail(operation, "certifications_valid", str(exc))
        self._cache["certified"] = (certified, excluded)
        return self._cache["certified"]

    def catalog(self, operation):
        if not self.cfg.catalog:
            return None
        from ..runners.discovery import load_snapshot
        try:
            return load_snapshot(self.cfg.catalog)
        except (OSError, ValueError) as exc:
            fail(operation, "catalog_valid", f"{self.cfg.catalog}: {exc}")

    def tier_costs(self):
        out = {}
        for key, value in (self.cfg.tier_costs or {}).items():
            try:
                out[int(key)] = float(value)
            except (TypeError, ValueError):
                continue
        return out

    def repo_checks(self, policy):
        """Configured gate commands; every check the policy requires is
        required here too, so a config cannot drop a policy check."""
        rc = copy.deepcopy(self.cfg.repo_checks or {})
        required = [n for n in rc.get("required") or [] if isinstance(n, str)]
        for name in (policy or {}).get("required_checks") or []:
            if name not in required:
                required.append(name)
        rc["required"] = required
        rc["commands"] = dict(rc.get("commands") or {})
        return rc

    # == components ==
    @property
    def emitter(self):
        if "emitter" not in self._cache:
            self._cache["emitter"] = JsonlEventSink(self.cfg.event_log, clock=self.clock)
        return self._cache["emitter"]

    @property
    def store(self):
        if "store" not in self._cache:
            self._cache["store"] = RunStore(self.cfg.runs_dir, clock=self.clock)
        return self._cache["store"]

    @property
    def manager(self):
        if "manager" not in self._cache:
            self._cache["manager"] = RunManager(self.store, clock=self.clock, emitter=self.emitter,
                                                lease_ttl=self.cfg.lease_ttl)
        return self._cache["manager"]

    @property
    def broker(self):
        if "broker" not in self._cache:
            self._cache["broker"] = ApprovalBroker(self.cfg.approvals_path, clock=self.clock, emitter=self.emitter)
        return self._cache["broker"]

    def worktrees(self, operation):
        if "worktrees" not in self._cache:
            try:
                self._cache["worktrees"] = WorktreeManager(self.repo_top(operation), self.cfg.worktree_root,
                                                           clock=self.clock)
            except WorktreeError as exc:
                fail(operation, "worktree_root_valid", str(exc))
        return self._cache["worktrees"]

    def queue_state(self):
        if os.path.exists(self.cfg.queue_path):
            try:
                return read_json(self.cfg.queue_path)
            except (OSError, ValueError):
                return None
        return None

    def conductor(self, policy, operation, runner_env_allow=()):
        """A conductor for one invocation. `policy` is the run's stored policy
        (or, for create-run, the policy file), never re-read for a run."""
        repo = self.repo_top(operation)
        worktrees = self.worktrees(operation)
        certified, _ = self.certified(operation)
        catalog = self.catalog(operation)
        holder = {}
        try:
            queue = MergeQueue(repo, self.cfg.target_branch, self.cfg.queue_path, self.cfg.scratch_root,
                               gate_runner=lambda unit, path: holder["c"].gate(unit, path), clock=self.clock,
                               emitter=self.emitter, broker=self.broker, policy=policy,
                               certified_runners=certified, catalog=catalog)
        except ValueError as exc:
            fail(operation, "merge_queue_valid", str(exc))
        supervisor = Supervisor(clock=self.clock)
        c = Conductor(self.cfg.conductor_id, manager=self.manager, worktrees=worktrees, supervisor=supervisor,
                      queue=queue, broker=self.broker, emitter=self.emitter, repo_checks=self.repo_checks(policy),
                      target_branch=self.cfg.target_branch, log_root=self.cfg.logs_dir,
                      runner_timeout=self.cfg.runner_timeout, gate_timeout=self.cfg.gate_timeout,
                      runner_env_allow=tuple(runner_env_allow), protected_globs=tuple(
                          (policy or {}).get("protected_commands") or ()),
                      clock=self.clock, policy=policy, certified_runners=certified, catalog=catalog,
                      tier_costs=self.tier_costs())
        holder["c"] = c
        return c
