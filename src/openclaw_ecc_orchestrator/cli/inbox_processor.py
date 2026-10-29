"""`approvals process-inbox`: the standing consumer of the decision inbox.

It decides nothing. Every file goes through `DecisionInbox` and
`ApprovalBroker.resolve`, exactly as in `approvals approve` and `approvals
reject`, so every binding check applies (request known and pending, expiry,
run, unit, action, plan hash, session, head sha when named, decision value,
a non empty `decided_by`) and the broker emits `approval.resolved`. Identity
comes from the decision file's `decided_by`, written by OpenClaw.

Modes: once (default), `--watch` (poll every `--interval` seconds until
SIGTERM or SIGINT, finishing the file in progress) and `--dry-run` (read
only). One processor per inbox: it holds `processor_lock_path(...)`; a
second one exits 3. Everything printed passes through the redaction engine.
"""

import signal
import threading
import time

from ..plugin.inbox import DecisionInbox, ProcessorLock, probe_processor_lock, processor_lock_path
from ..runs.envelope import check, envelope
from ..runs.store import iso
from . import output
from .config import UsageError

OP = "approvals.process-inbox"
MIN_INTERVAL = 0.5
MAX_INTERVAL = 60.0
DEFAULT_INTERVAL = 2.0
MAX_FILE_ERRORS = 3  # watch mode: failed attempts before a file is set aside
_SLICE = 0.1


def validate_args(args):
    if args.watch and args.dry_run:
        raise UsageError("--dry-run lists the inbox once; it cannot be combined with --watch")
    if not args.watch and (args.interval is not None or args.max_passes is not None):
        raise UsageError("--interval and --max-passes are only used together with --watch")
    if args.interval is None:
        args.interval = DEFAULT_INTERVAL


def _failed(result):
    return [f"{c.get('name')}: {c.get('detail')}" if c.get("detail") not in (None, "") else str(c.get("name"))
            for c in result.get("checks") or [] if not c.get("ok")]


def file_summary(result, preview=False):
    src = result.get("source_decision") or {}
    data = result.get("data") or {}
    ok = bool(result.get("ok"))
    out = {"file": result.get("source_file"), "ok": ok}
    if preview:
        out["would"] = "apply" if ok else "refuse"
    else:
        out["outcome"] = "applied" if ok else "refused"
    for key in ("request_id", "decision", "decided_by"):
        out[key] = data.get(key) or src.get(key)
    out["failed"] = [] if ok else _failed(result)
    return out


def _file_line(summary, prefix=""):
    verb = summary.get("outcome") or f"would {summary.get('would')}"
    head = f"{prefix}{summary['file']}: {verb}"
    who = f"request {summary.get('request_id')} {summary.get('decision')} by {summary.get('decided_by')}"
    if summary["ok"]:
        return f"{head} ({who})"
    return f"{head}: {'; '.join(summary['failed'])}"


def _lock_refusal(lock_path, inbox_dir):
    info = probe_processor_lock(lock_path)
    pid = f" (pid {info['pid']})" if info.get("pid") else ""
    detail = f"another inbox processor{pid} holds {lock_path} for {inbox_dir}"
    return envelope(OP, ok=False, checks=[check("processor_lock_acquired", False, detail)],
                   required_user_actions=[{"kind": "inbox_processor_running",
                                           "detail": f"another inbox processor{pid} is already consuming the inbox; "
                                                     "let it run, or stop it before starting another"}],
                   data={"inbox": inbox_dir, "lock": info})


class _Totals:
    def __init__(self):
        self.applied = self.refused = self.errors = 0
        self.error_details = []

    def add(self, summary):
        if summary["ok"]:
            self.applied += 1
        else:
            self.refused += 1


def _run_pass(inbox, totals, report_file, report_error, should_stop, failures=None):
    """One pass over the pending files. An exception in one file is
    reported and never stops the pass. With `failures` (watch mode) a file
    failing MAX_FILE_ERRORS times is set aside into processed/."""
    try:
        names = inbox.pending_files()
    except Exception as exc:
        report_error(None, f"{type(exc).__name__}: {exc}", None)
        return
    for name in names:
        if should_stop():
            return
        try:
            result = inbox.process_one(name)
        except Exception as exc:
            text = output.redact_line(f"{type(exc).__name__}: {exc}")
            if failures is None:
                report_error(name, text, None)
                continue
            failures[name] = failures.get(name, 0) + 1
            report_error(name, text, failures[name])
            if failures[name] < MAX_FILE_ERRORS:
                continue
            try:
                result = inbox.reject(name, "processing_error",
                                      f"set aside after {failures[name]} failed attempts; last error: {text}")
            except Exception as exc2:
                report_error(name, f"{type(exc2).__name__}: {exc2}", failures[name])
                continue
        if failures is not None:
            failures.pop(name, None)
        if result is None:
            continue
        summary = file_summary(result)
        totals.add(summary)
        report_file(summary)


def _once(ctx, inbox, lock_path):
    totals = _Totals()
    files = []

    def report_error(name, text, attempt):
        totals.errors += 1
        totals.error_details.append({"file": name, "error": output.redact_line(text)})

    _run_pass(inbox, totals, files.append, report_error, lambda: False)
    data = {"mode": "once", "inbox": inbox.inbox_dir, "lock": lock_path, "files": files, "applied": totals.applied,
            "refused": totals.refused, "errors": totals.errors, "error_details": totals.error_details}
    checks = [check("processor_lock_acquired", True, lock_path),
              check("files_processed", not totals.errors, f"{totals.errors} file(s) failed with an error"
                    if totals.errors else f"{len(files)} file(s)")]
    warnings = [f"{f['file']} refused: {'; '.join(f['failed'])}" for f in files if not f["ok"]]
    env = envelope(OP, ok=not totals.errors, changed=bool(files), checks=checks, warnings=warnings, data=data)
    env["_summary"] = [f"inbox {inbox.inbox_dir}: {len(files)} file(s): {totals.applied} applied, "
                       f"{totals.refused} refused, {totals.errors} error(s)"]
    env["_summary"] += [_file_line(f) for f in files]
    env["_summary"] += [f"{e['file'] or 'inbox'}: error {e['error']}" for e in totals.error_details]
    return env


def _dry_run(ctx, inbox, lock_path):
    previews = [file_summary(r, preview=True) for r in inbox.preview()]
    lock = probe_processor_lock(lock_path)
    warnings = []
    if lock.get("held"):
        warnings.append(f"a standing inbox processor (pid {lock.get('pid')}) is running and may process these "
                        "files first")
    data = {"mode": "dry_run", "inbox": inbox.inbox_dir, "lock": lock, "files": previews}
    env = envelope(OP, changed=False, checks=[check("dry_run", True, "nothing changed")], warnings=warnings,
                   data=data)
    env["_summary"] = [f"dry run: {len(previews)} pending file(s) in {inbox.inbox_dir}"]
    env["_summary"] += [_file_line(p) for p in previews]
    return env


def _watch(ctx, inbox, lock_path):
    a = ctx.args
    stop = {"reason": None}
    totals = _Totals()
    failures = {}

    def emit(ok, data, line, checks=()):
        if a.json:
            output.write_json(ctx.stdout, output.public(envelope(OP, ok=ok, changed=data.get("event") == "file",
                                                                 checks=list(checks), data=data)))
        else:
            ctx.stdout.write(output.redact_line(f"{iso(ctx.clock())} {line}") + "\n")
            ctx.stdout.flush()

    def report_file(summary):
        emit(summary["ok"], dict(summary, event="file"), _file_line(summary),
             [check("decision_applied", summary["ok"], "; ".join(summary["failed"]))])

    def report_error(name, text, attempt):
        totals.errors += 1
        text = output.redact_line(text)
        tries = f" (attempt {attempt} of {MAX_FILE_ERRORS})" if attempt else ""
        emit(False, {"event": "error", "file": name, "error": text, "attempt": attempt},
             f"{name or 'inbox'}: error {text}{tries}", [check("processing_error", False, text)])

    def on_signal(signum, frame):
        stop["reason"] = signal.Signals(signum).name

    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, on_signal)
    passes = 0
    try:
        emit(True, {"event": "started", "inbox": inbox.inbox_dir, "lock": lock_path, "interval": a.interval},
             f"watching {inbox.inbox_dir} every {a.interval:g}s (lock {lock_path})")
        while stop["reason"] is None:
            _run_pass(inbox, totals, report_file, report_error, lambda: stop["reason"] is not None, failures)
            passes += 1
            if a.max_passes is not None and passes >= a.max_passes:
                stop["reason"] = stop["reason"] or "max_passes"
                break
            deadline = time.monotonic() + a.interval
            while stop["reason"] is None and time.monotonic() < deadline:
                time.sleep(min(_SLICE, max(0.0, deadline - time.monotonic())))
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    emit(True, {"event": "stopped", "reason": stop["reason"], "passes": passes, "applied": totals.applied,
                "refused": totals.refused, "errors": totals.errors},
         f"stopped ({stop['reason']}) after {passes} pass(es): {totals.applied} applied, {totals.refused} refused, "
         f"{totals.errors} error(s)")
    return None


def cmd_process_inbox(ctx):
    cfg = ctx.cfg
    inbox = DecisionInbox(cfg.decision_inbox, ctx.rt.broker)
    lock_path = processor_lock_path(cfg.state_dir, cfg.decision_inbox)
    if ctx.args.dry_run:
        return _dry_run(ctx, inbox, lock_path)
    lock = ProcessorLock(lock_path)
    if not lock.acquire(inbox=inbox.inbox_dir, clock=ctx.clock):
        return _lock_refusal(lock_path, inbox.inbox_dir)
    try:
        if ctx.args.watch:
            return _watch(ctx, inbox, lock_path)
        return _once(ctx, inbox, lock_path)
    finally:
        lock.release()
