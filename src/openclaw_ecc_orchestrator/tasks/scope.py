"""Repository relative path safety and scope matching.

Scope entries are repository relative POSIX paths or globs. ``**`` matches any
number of directory segments; ``*`` and ``?`` never cross a ``/``.
"""

from __future__ import annotations

import re
import unicodedata
import urllib.parse

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def unsafe_path_reason(path: object) -> str | None:
    """Return why ``path`` is not a safe repository relative path, or None."""
    if not isinstance(path, str):
        return "path must be a string"
    if not path or not path.strip():
        return "path is empty"
    if len(path) > 1024:
        return "path is too long"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
        return "path contains control characters"
    candidates = {path, unicodedata.normalize("NFKC", path)}
    decoded = path
    for _ in range(3):  # catch single and nested percent encoding
        decoded = urllib.parse.unquote(decoded)
        candidates.add(decoded)
        candidates.add(unicodedata.normalize("NFKC", decoded))
    for cand in candidates:
        norm = cand.replace("\\", "/")
        if norm.startswith("/") or _DRIVE_RE.match(norm):
            return "absolute paths are not allowed"
        if norm.startswith("~"):
            return "home relative paths are not allowed"
        if any(seg == ".." for seg in norm.split("/")):
            return "path traversal is not allowed"
        if "\x00" in cand:
            return "path contains control characters"
    return None


def _glob_to_regex(pattern: str) -> re.Pattern:
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("".join(out) + r"\Z")


def normalize(path: str) -> str:
    norm = path.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return re.sub(r"/+", "/", norm)


def glob_match(path: str, pattern: str) -> bool:
    return bool(_glob_to_regex(normalize(pattern)).match(normalize(path)))


def path_in_scope(path: str, scope_files: list[str]) -> bool:
    """True when a safe ``path`` equals or matches any scope entry."""
    if unsafe_path_reason(path) is not None:
        return False
    return any(glob_match(path, entry) for entry in scope_files)


def is_glob(entry: str) -> bool:
    return any(ch in entry for ch in "*?")


def scope_contains(path: str, scope_files: list[str]) -> bool:
    """True when ``path`` is covered by the declared scope.

    An empty scope means the whole repository. A literal entry also covers
    everything beneath it when it names a directory (``src`` or ``src/``);
    globs use :func:`glob_match` semantics. Unsafe paths are never covered.
    """
    if unsafe_path_reason(path) is not None:
        return False
    if not scope_files:
        return True
    norm = normalize(path)
    for entry in scope_files:
        if glob_match(norm, entry):
            return True
        if not is_glob(entry):
            base = normalize(entry).rstrip("/")
            if base and norm.startswith(base + "/"):
                return True
    return False


def out_of_scope(paths: list[str], scope_files: list[str]) -> list[str]:
    """Paths not covered by ``scope_files`` (see :func:`scope_contains`)."""
    return [p for p in paths if not scope_contains(p, scope_files)]
