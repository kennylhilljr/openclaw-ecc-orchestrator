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
from typing import Iterable, NamedTuple

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
    # Provider account and key identifiers echoed in error bodies (Moonshot,
    # observed 2026-10-18). Not secrets on their own, but they identify the account.
    r"\bak-[a-z0-9]{16,}",
    r"\borg-[0-9a-f]{24,}",
    r"eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}",
    r"(?i:bearer)\s+[A-Za-z0-9._~+/\-]{16,}=*",
]
_PREFIX_RE = re.compile("|".join("(?:%s)" % p for p in _PREFIX_PATTERNS))

_TOKEN_MIN_LEN = 32
_TOKEN_RE = re.compile(r"[A-Za-z0-9+/_=\-]{%d,}" % _TOKEN_MIN_LEN)
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
        r"(?i)\b[A-Za-z0-9_.-]*(?:%s|_key\b|_pass\b|_pwd\b)[A-Za-z0-9_.-]*(?:\\?[\"'])?"
        r"\s*[:=]\s*(\\\"(?:[^\"\\]|\\[^\"])*\\\"|\"[^\"]*\"|'[^']*'|[^\s,;\"']+)" % _NAME_WORDS),
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


# Absolute filesystem paths under these roots are judged one path segment at a
# time by the high entropy rule. Otherwise a whole macOS temp path such as
# /private/var/folders/6j/8_5x46rs.../T/tmpabc (mixed case, digits, slashes)
# reads as one random token and gets masked. A segment that is itself long
# and random is still masked, and the credential prefix rules apply anywhere.
# A standard base64 credential can contain "/", so its pieces may each be
# shorter than 32 characters. Adjacent pieces made of base64 characters form
# a chain: strong pieces (upper and lower case, not word like) joined by
# filler pieces (1 to 3 characters, or other non word like base64 runs), never
# two fillers in a row. A chain must start and end on a strong piece or on a
# short piece that is not plain lowercase, so root names such as "tmp" never
# join one. Every window of up to 8 pieces of a chain that reaches 32
# characters is also judged, and when one is random the whole chain is
# masked. Word like segments, segments with "-", "_" or ".", and two fillers
# in a row end a chain, so temp, venv, repository and acronym heavy paths
# such as /var/folders/6j/<id>/T/tmpabc or /opt/JSON2XML/CLI3 stay visible.
_PATH_ROOTS = ("/private/", "/var/", "/tmp/", "/Users/", "/home/", "/opt/", "/usr/",
               "/Library/", "/Volumes/", "/System/", "/mnt/", "/root/", "/etc/")

_PIECE_CHARS_RE = re.compile(r"[A-Za-z0-9+=]+")
_WORD_RUN_RE = re.compile(r"[a-z]{3,}")
_CHAIN_MIN_LEN = 32
_CHAIN_MAX_PIECES = 8


def _word_like(segment: str) -> bool:
    return 2 * sum(len(w) for w in _WORD_RUN_RE.findall(segment)) >= len(segment)


def _strong_piece(segment: str) -> bool:
    """A path segment that looks like a piece of a slash split random token."""
    return (len(segment) >= 4 and _PIECE_CHARS_RE.fullmatch(segment) is not None
            and re.search(r"[A-Z]", segment) is not None
            and re.search(r"[a-z]", segment) is not None
            and not _word_like(segment))


def _short_piece(segment: str) -> bool:
    return 1 <= len(segment) <= 3 and _PIECE_CHARS_RE.fullmatch(segment) is not None


def _filler_piece(segment: str) -> bool:
    """A piece that may sit between strong pieces: short, or a base64 run
    without the mixed case of a strong piece (such as "mb8n" or "MB8N")."""
    if _short_piece(segment):
        return True
    return (len(segment) >= 4 and _PIECE_CHARS_RE.fullmatch(segment) is not None
            and not _word_like(segment) and not _strong_piece(segment))


def _chain_end(segment: str) -> bool:
    return _strong_piece(segment) or (_short_piece(segment) and not segment.islower())


class _Piece(NamedTuple):
    start: int
    end: int
    text: str


def _chains(pieces: list[_Piece]) -> list[list[_Piece]]:
    """Runs of strong pieces and single fillers, trimmed to start and end on
    a chain end, holding at least two pieces and at least one strong piece."""
    runs: list[list[_Piece]] = [[]]
    for piece in pieces:
        if _strong_piece(piece.text):
            runs[-1].append(piece)
        elif _filler_piece(piece.text):
            if runs[-1] and not _strong_piece(runs[-1][-1].text):
                runs.append([])
            runs[-1].append(piece)
        else:
            runs.append([])
    chains = []
    for run in runs:
        while run and not _chain_end(run[0].text):
            run.pop(0)
        while run and not _chain_end(run[-1].text):
            run.pop()
        if len(run) >= 2 and any(_strong_piece(p.text) for p in run):
            chains.append(run)
    return chains


def _chain_spans(m: "re.Match[str]", pieces: list[_Piece]) -> list[tuple[int, int]]:
    """Spans of chains holding a random window of at least 32 characters.

    Every window of 2 to _CHAIN_MAX_PIECES adjacent pieces that reaches the
    minimum length is judged, so padding before or after a credential cannot
    dilute it. A random window masks its whole chain, so no piece of the
    credential is left visible.
    """
    spans = []
    for chain in _chains(pieces):
        if any(_is_high_entropy(m.string[chain[i].start:chain[j].end])
               for i in range(len(chain))
               for j in range(i + 1, min(len(chain), i + _CHAIN_MAX_PIECES))
               if chain[j].end - chain[i].start >= _CHAIN_MIN_LEN):
            spans.append((chain[0].start, chain[-1].end))
    return spans


def _path_start(token: str) -> int:
    """Index where an absolute path begins in ``token`` (at its start or right
    after ``=`` / ``:`` as in ``cwd=/private/...``), or -1."""
    best = -1
    for root in _PATH_ROOTS:
        idx = token.find(root)
        while idx > 0 and token[idx - 1] not in "=:":
            idx = token.find(root, idx + 1)
        if idx >= 0 and (best < 0 or idx < best):
            best = idx
    return best


# Orchestrator unit branches (``ecc/<run_id>/<unit_id>``, optionally under
# ``refs/heads/``) are judged one segment at a time, like paths. Joined, a
# time-stamped run id and a unit id often reach 32 characters with mixed
# case and digits and would read as one random token (for example
# ``ecc/smoke-20261019T005138Z/smoke``). A segment that is itself long and
# random is still masked.
_BRANCH_RE = re.compile(
    r"(?:refs/heads/)?ecc/[A-Za-z0-9][A-Za-z0-9._-]{0,127}/[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _branch_spans(m: "re.Match[str]") -> list[tuple[int, int]] | None:
    """Spans for a token holding an orchestrator branch, or None if it holds none."""
    token = m.group(0)
    for b in _BRANCH_RE.finditer(token):
        if b.start() > 0 and token[b.start() - 1] != "=":
            continue
        if b.end() < len(token) and token[b.end()] not in "=":
            continue
        spans = []
        for lo, hi in ((0, max(0, b.start() - 1)), (b.end(), len(token))):
            part = token[lo:hi]
            if len(part) >= 32 and _is_high_entropy(part):
                spans.append((m.start() + lo, m.start() + hi))
        offset = m.start() + b.start()
        for segment in token[b.start():b.end()].split("/"):
            if len(segment) >= 32 and _is_high_entropy(segment):
                spans.append((offset, offset + len(segment)))
            offset += len(segment) + 1
        return spans
    return None


_PATH_RUN_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-/")
_PATH_LOOKBACK = 4096


def _continues_path(m: "re.Match[str]") -> bool:
    """True when the token resumes an absolute path right after a dot.

    The token pattern has no ".", so a path splits there and the rest no longer
    starts at a path root: a hidden directory ("/.local/share/...") or a dotted
    name such as mktemp's "/tmp/ecc-check.Ab3Xy9/xdg-state/...". A credential is
    never preceded by "/.". For any other dot, the text before it must be an
    unbroken run of path characters (no whitespace, quotes, "=" or ":") that
    starts at a known path root, so a separate value after a path is not exempt.
    """
    text, i = m.string, m.start()
    if i < 2 or text[i - 1] != ".":
        return False
    if text[i - 2] == "/":
        return True
    j = i - 1
    floor = max(0, j - _PATH_LOOKBACK)
    while j > floor and text[j - 1] in _PATH_RUN_CHARS:
        j -= 1
    if j > floor or j == 0:
        return any(text.startswith(root, j) and j + len(root) <= i - 1 for root in _PATH_ROOTS)
    return False


# Repository-relative file paths (for example docs/prd/components/PRD-platform-v3.md)
# are judged one segment at a time, like absolute paths: joined, the folders and a
# versioned file name reach 32 characters with mixed case and digits. The token
# stops at the extension's dot, so a following known extension marks the path.
_FILE_EXT_RE = re.compile(r"\.(?:md|py|pyi|json|ya?ml|toml|txt|sql|sh|mjs|js|ts|html|css|cfg|ini|lock)\b")


def _repo_relative_path(m: "re.Match[str]") -> bool:
    return "/" in m.group(0) and _FILE_EXT_RE.match(m.string, m.end()) is not None


def _entropy_spans(m: "re.Match[str]") -> list[tuple[int, int]]:
    token = m.group(0)
    branch = _branch_spans(m)
    if branch is not None:
        return branch
    start = 0 if _continues_path(m) or _repo_relative_path(m) else _path_start(token)
    if start < 0:
        return [m.span()] if _is_high_entropy(token) else []
    spans = []
    head = token[:start]
    if len(head) >= 32 and _is_high_entropy(head):
        spans.append((m.start(), m.start() + start))
    offset = m.start() + start
    pieces = []
    for segment in token[start:].split("/"):
        if len(segment) >= 32 and _is_high_entropy(segment):
            spans.append((offset, offset + len(segment)))
        pieces.append(_Piece(offset, offset + len(segment), segment))
        offset += len(segment) + 1
    spans.extend(_chain_spans(m, pieces))
    return spans


# A JSON object key made of camelCase words whose value is a number, such as
# Claude Code's "cacheCreationEphemeral1hInputTokens": 60157 in modelUsage.
# Long counter names like that score above the entropy threshold. The
# exemption is narrow: a lowercase first word, then at least three
# capitalized words, at most two digits in all (each digit group one digit and
# at most one lowercase letter, like "1h"), no runs of capitals, and a numeric
# value. Random tokens have runs of capitals or many digits, so they still go
# through the entropy check.
_CAMEL_WORDS_RE = re.compile(r"[a-z]+(?:[A-Z][a-z]+|[0-9][a-z]?)*")
_NUMERIC_KEY_END_RE = re.compile(r'\\*"\s*:\s*-?\d')
_MIN_CAMEL_WORDS = 3
_MAX_KEY_DIGITS = 2


def _numeric_counter_key(m: "re.Match[str]") -> bool:
    text, start, end = m.string, m.start(), m.end()
    token = m.group(0)
    if start == 0 or text[start - 1] != '"':
        return False
    if not _CAMEL_WORDS_RE.fullmatch(token):
        return False
    if sum(c.isupper() for c in token) < _MIN_CAMEL_WORDS:
        return False
    if sum(c.isdigit() for c in token) > _MAX_KEY_DIGITS:
        return False
    return _NUMERIC_KEY_END_RE.match(text, end) is not None


def _detect_spans(text: str) -> list[tuple[int, int]]:
    spans = [m.span() for m in _PREFIX_RE.finditer(text)]
    spans += [m.span() for m in _PEM_BLOCK_RE.finditer(text)]
    for m in _TOKEN_RE.finditer(text):
        if _numeric_counter_key(m):
            continue
        spans.extend(_entropy_spans(m))
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
            if index == 0 and _benign_named_value(text[m.start(0):m.start(1)], value):
                continue
            if value and value != REDACTED and value.strip("\"'") != REDACTED:
                start, end = m.span(1)
                if len(value) >= 4 and value.startswith('\\"') and value.endswith('\\"'):
                    # The same inside a JSON string, where the quotes are
                    # escaped (live finding, rm-m0f M0-20).
                    start, end = start + 2, end - 2
                elif len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    # Mask inside the quotes so a JSON line stays JSON (live
                    # finding, rm-m0b: "session_id":[REDACTED] broke Claude's
                    # result line and its verdict and cost were lost).
                    start, end = start + 1, end - 1
                else:
                    # An unquoted value keeps a trailing backslash: it escapes
                    # the quote that ends the JSON string around it.
                    end -= len(value) - len(value.rstrip("\\"))
                if end > start:
                    spans.append((start, end))
    return _merge(spans)


# Usage counters such as "promptTokenCount": 12 or "max_tokens": 64 match the
# secret name rule ("token") but hold plain numbers, never credentials.
_COUNTER_NAME_RE = re.compile(r"(?i)(?:count|limit|tokens)[\"']?\s*[:=]\s*$")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?[}\]]*")  # "thinking_tokens":15327} ends an object
_STRUCTURAL_VALUES = frozenset({"[", "{", "[]", "{}"})
_JSON_LITERALS_RE = re.compile(r"(?:null|true|false)[}\]]*")


def _benign_named_value(prefix: str, value: str) -> bool:
    """True for values the name rule must leave visible (unquoted numbers of
    counter or limit fields, bare JSON structure tokens, and JSON null, true
    and false, as in "thinking_tokens":null)."""
    if value in _STRUCTURAL_VALUES or _JSON_LITERALS_RE.fullmatch(value):
        return True
    return bool(_NUMBER_RE.fullmatch(value) and _COUNTER_NAME_RE.search(prefix))


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


# JSON fields that carry provider generated opaque values (reasoning
# signatures, pagination cursors, response ids). A high entropy value inside
# one of these is not treated as a leaked credential during log inspection.
# Credential prefixes and registered secret values still count everywhere.
OPAQUE_FIELDS = ("thoughtSignature", "thought_signature", "nextPageToken", "next_page_token",
                 "responseId", "response_id")
_OPAQUE_VALUE_RE = re.compile(
    r"\"(?:%s)\"\s*:\s*\"([^\"]*)\"" % "|".join(re.escape(f) for f in OPAQUE_FIELDS))


def inspection_findings(texts: Iterable[object], secrets: Iterable[str] = ()) -> list[dict]:
    """Credential findings in runner output, for the certification log check.

    Each finding is ``{"text": index, "kind": ...}`` and never contains the
    value. Kinds: ``registered_secret`` (a literal secret such as the
    provider key), ``credential_pattern`` (known key prefixes, JWTs, private
    key blocks) and ``high_entropy`` (a long random looking token outside the
    opaque provider fields in :data:`OPAQUE_FIELDS`).
    """
    literal = [s for s in secrets if s]
    findings: list[dict] = []
    for index, text in enumerate(texts):
        if not isinstance(text, str) or not text:
            continue
        if any(value in text for value in literal):
            findings.append({"text": index, "kind": "registered_secret"})
        if _PREFIX_RE.search(text) or _PEM_BLOCK_RE.search(text):
            findings.append({"text": index, "kind": "credential_pattern"})
        opaque = [m.span(1) for m in _OPAQUE_VALUE_RE.finditer(text)]
        for m in _TOKEN_RE.finditer(text):
            if _numeric_counter_key(m):
                continue
            hits = [span for span in _entropy_spans(m)
                    if not any(start <= span[0] and span[1] <= end for start, end in opaque)]
            if hits:
                findings.append({"text": index, "kind": "high_entropy"})
                break
    return findings


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
