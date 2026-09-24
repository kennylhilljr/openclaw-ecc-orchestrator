"""The single secret detection and redaction engine.

Every module that writes, logs or emits text uses this engine:
``process.redact`` is a thin re-export, the process supervisor and quality
gates build a :class:`Redactor` per process, the event emitter uses
:func:`redact_obj`, and the schema validator uses :func:`contains_secret`.

Detection is heuristic: well known credential prefixes (``sk-``, ``sk-ant-``,
``sk-or-``, ``gsk_``, ``AIza``, ``ghp_``/``gho_``/``github_pat_``, ``xox*``,
``AKIA``), JSON web tokens, bearer and authorization headers, private key
blocks and a high entropy token rule. Redaction additionally masks the value
of ``name=value`` assignments whose name looks secret, credentials embedded in
URLs, and any literal value registered with :meth:`Redactor.add_secret`.

Callers must never echo a detected value. Error messages produced here never
contain the secret either.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable

REDACTED = "[REDACTED]"
MASK = REDACTED
MIN_SECRET_LEN = 6

# Ordered: longer / more specific prefixes first.
_PREFIX_PATTERNS = [
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----",
    r"sk-ant-[A-Za-z0-9_\-]{8,}",
    r"sk-proj-[A-Za-z0-9_\-]{8,}",
    r"sk-or-[A-Za-z0-9_\-]{8,}",
    r"sk-[A-Za-z0-9_\-]{16,}",
    r"gsk_[A-Za-z0-9]{16,}",
    r"AIza[A-Za-z0-9_\-]{20,}",
    r"github_pat_[A-Za-z0-9_]{16,}",
    r"gh[pousr]_[A-Za-z0-9]{20,}",
    r"glpat-[A-Za-z0-9_\-]{16,}",
    r"xox[a-z]-[A-Za-z0-9\-]{10,}",
    r"(?:AKIA|ASIA)[A-Z0-9]{16}",
    r"hf_[A-Za-z0-9]{20,}",
    r"eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}",
    r"(?i:bearer)\s+[A-Za-z0-9._~+/\-]{16,}=*",
]
_PREFIX_RE = re.compile("|".join("(?:%s)" % p for p in _PREFIX_PATTERNS))

_TOKEN_RE = re.compile(r"[A-Za-z0-9+/_=\-]{32,}")
_HEX_RE = re.compile(r"[0-9a-fA-F]+")
_ENTROPY_THRESHOLD = 4.0

# A whole private key block inside one piece of text (multi line values).
_PEM_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)")
_PEM_BEGIN_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_PEM_END_RE = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")

_NAME_WORDS = (r"token|secret|passw(?:or)?d|passphrase|api[_-]?key|access[_-]?key|private[_-]?key"
               r"|credential|cookie|session[_-]?id|auth")
_SECRET_NAME = re.compile(r"(?i)(?:%s)|(?:^|[_.-])(?:key|pass|pwd)(?:$|[_.-])" % _NAME_WORDS)

# Redaction only rules. Each masks one capture group (the value), keeping context.
_VALUE_RULES = [
    # NAME=value, NAME: value, "name": "value" with a secret looking name.
    re.compile(
        r"(?i)\b[A-Za-z0-9_.-]*(?:%s|_key\b|_pass\b|_pwd\b)[A-Za-z0-9_.-]*[\"']?"
        r"\s*[:=]\s*(\"[^\"]*\"|'[^']*'|[^\s,;\"']+)" % _NAME_WORDS),
    # Authorization: <scheme> <credential>, or Authorization: <credential>.
    re.compile(r"(?i)\b(?:proxy-)?authorization\s*[:=]\s*(?:[A-Za-z]+\s+)?([^\s,;\"']{4,})"),
    # Bearer / Basic credentials of any length.
    re.compile(r"(?i)\b(?:bearer|basic)\s+([A-Za-z0-9._~+/=\-]{8,})"),
    # https://user:password@host
    re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^/\s:@]+:([^/\s@]+)@"),
]
_AUTH_HEADER_RE = re.compile(r"(?i)(?:proxy-)?authorization\b")


def looks_secret_name(name: object) -> bool:
    """True when an environment variable, key or flag name looks like a credential."""
    return bool(_SECRET_NAME.search(str(name)))


def _entropy(token: str) -> float:
    counts = Counter(token)
    total = len(token)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def _is_high_entropy(token: str) -> bool:
    if _HEX_RE.fullmatch(token) and len(token) <= 64:
        return False  # commit SHAs and content digests
    if not (re.search(r"[a-z]", token) and re.search(r"[A-Z]", token)
            and re.search(r"[0-9]", token)):
        return False
    return _entropy(token) >= _ENTROPY_THRESHOLD


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    spans = sorted(s for s in spans if s[1] > s[0])
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _detect_spans(text: str) -> list[tuple[int, int]]:
    spans = [m.span() for m in _PREFIX_RE.finditer(text)]
    spans += [m.span() for m in _PEM_BLOCK_RE.finditer(text)]
    for m in _TOKEN_RE.finditer(text):
        if _is_high_entropy(m.group(0)):
            spans.append(m.span())
    return spans


def _spans(text: str) -> list[tuple[int, int]]:
    return _merge(_detect_spans(text))


def _redaction_spans(text: str) -> list[tuple[int, int]]:
    spans = _detect_spans(text)
    for index, rule in enumerate(_VALUE_RULES):
        for m in rule.finditer(text):
            if index == 0 and _AUTH_HEADER_RE.match(m.group(0)):
                continue  # the header rule keeps the scheme and masks the credential
            value = m.group(1)
            if value and value != REDACTED and value.strip("\"'") != REDACTED:
                spans.append(m.span(1))
    return _merge(spans)


def _apply(text: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return text
    out, last = [], 0
    for start, end in spans:
        out.append(text[last:start])
        out.append(REDACTED)
        last = end
    out.append(text[last:])
    return "".join(out)


def contains_secret(text: object) -> bool:
    """True when ``text`` holds something that looks like a credential.

    Only value bearing patterns count here (prefixes, JWTs, key blocks, high
    entropy tokens); a bare ``password=`` mention is not a detection.
    """
    if not isinstance(text, str) or not text:
        return False
    return bool(_spans(text))


def _replace_literals(text: str, values: Iterable[str]) -> str:
    for value in sorted({v for v in values if v}, key=len, reverse=True):
        if value in text:
            text = text.replace(value, REDACTED)
    return text


def redact_text(text: object, extra_values: tuple[str, ...] | list[str] = ()) -> str:
    """Return ``text`` with secret looking substrings replaced.

    ``extra_values`` are literal values (for example an API key read from the
    environment) that must be removed even if they do not look secret.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    text = _replace_literals(text, extra_values)
    return _apply(text, _redaction_spans(text))


def scan_strings(value: object, path: str = "") -> list[str]:
    """Return dotted field paths of every string inside ``value`` holding a secret."""
    hits: list[str] = []
    if isinstance(value, str):
        if contains_secret(value):
            hits.append(path or "$")
    elif isinstance(value, dict):
        for key, item in value.items():
            if contains_secret(str(key)):
                hits.append((path + "." if path else "") + "<key>")
            hits.extend(scan_strings(item, (path + "." if path else "") + str(key)))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            hits.extend(scan_strings(item, "%s[%d]" % (path, index)))
    return hits


class Redactor:
    """Pattern redaction plus a set of literal secrets registered at runtime.

    ``patterns`` is an optional list of extra ``(compiled_regex, replacement)``
    pairs applied after the built in engine.
    """

    def __init__(self, secrets: Iterable[str] = (), patterns=None):
        self._secrets: set[str] = set()
        self.patterns = list(patterns or ())
        for value in secrets:
            self.add_secret(value)

    def add_secret(self, value: object) -> None:
        if isinstance(value, str) and len(value) >= MIN_SECRET_LEN:
            self._secrets.add(value)

    @property
    def secrets(self) -> frozenset:
        return frozenset(self._secrets)

    def copy(self) -> "Redactor":
        return Redactor(self._secrets, self.patterns)

    def redact(self, text):
        if not isinstance(text, str):
            return text
        text = redact_text(text, tuple(self._secrets))
        for pattern, repl in self.patterns:
            text = pattern.sub(repl, text)
        return text

    def stream(self) -> "StreamRedactor":
        return StreamRedactor(self)


class StreamRedactor:
    """Stateful line redactor: a private key block split across lines is masked
    from its BEGIN line through its END line, including every body line."""

    def __init__(self, redactor: Redactor | None = None):
        self.redactor = redactor or Redactor()
        self.in_key_block = False

    def feed(self, line: str) -> str:
        if not isinstance(line, str):
            return line
        if self.in_key_block:
            end = _PEM_END_RE.search(line)
            if not end:
                return REDACTED
            self.in_key_block = False
            return REDACTED + self.feed(line[end.end():])
        begin = _PEM_BEGIN_RE.search(line)
        if begin and not _PEM_END_RE.search(line, begin.end()):
            self.in_key_block = True
            return self.redactor.redact(line[:begin.start()]) + REDACTED
        return self.redactor.redact(line)


# Flags whose next argument is always a secret.
_SECRET_FLAGS = {"-p", "--pass", "--password", "--passwd", "--passphrase", "--token", "--api-key",
                 "--apikey", "--secret", "--client-secret", "--access-token", "--auth",
                 "--auth-token", "--key", "--private-key", "--credentials", "--cookie"}
_ENV_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*)=(.*)$", re.S)


def _secret_flag(flag: str) -> bool:
    if flag in _SECRET_FLAGS:
        return True
    return flag.startswith("--") and looks_secret_name(flag[2:])


def redact_argv(argv: Iterable[object], redactor: Redactor | None = None) -> list[str]:
    """Redact an argument vector before it is stored in a status or event.

    Values of ``--token``/``--password``/``--api-key``/``--secret``/``-p``
    style flags (separate or ``--flag=value``) and of ``NAME=value`` pairs
    with a secret looking name are masked; every element then goes through
    the text engine and the registered literal secrets.
    """
    r = redactor or Redactor()
    out: list[str] = []
    pending_flag = None
    for raw in argv:
        arg = str(raw)
        if pending_flag is not None:
            flag, pending_flag = pending_flag, None
            # ``-p`` is overloaded (``claude -p --output-format``); an option
            # following it is not its value. Long secret flags always take one.
            if not (flag == "-p" and arg.startswith("-")):
                out.append(REDACTED)
                continue
        if arg.startswith("-") and "=" not in arg and _secret_flag(arg):
            pending_flag = arg
            out.append(arg)
            continue
        if arg.startswith("-") and "=" in arg:
            flag, _, _value = arg.partition("=")
            if _secret_flag(flag):
                out.append(flag + "=" + REDACTED)
                continue
        m = _ENV_ASSIGN_RE.match(arg)
        if m and not arg.startswith("-") and looks_secret_name(m.group(1)):
            out.append(m.group(1) + "=" + REDACTED)
            continue
        out.append(r.redact(arg))
    return out


_DEFAULT = Redactor()


def redact_obj(obj, redactor: Redactor | None = None):
    """Recursively redact strings, masking values under secret looking keys."""
    r = redactor or _DEFAULT
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if looks_secret_name(key) and isinstance(value, (str, int, float)) and not isinstance(value, bool):
                out[key] = REDACTED
            else:
                out[key] = redact_obj(value, r)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v, r) for v in obj]
    if isinstance(obj, str):
        return r.redact(obj)
    return obj
