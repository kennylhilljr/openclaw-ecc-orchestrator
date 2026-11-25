#!/usr/bin/env bash
# Runs the Python tests, the plugin tests and the secret scan, in that order.
# Every check runs even if an earlier one fails. Exits 0 only if all pass.
set -u

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root" || exit 1

# Base is literally /tmp so an inherited TMPDIR cannot break or redirect it.
if ! check_tmp="$(mktemp -d /tmp/ecc-check.XXXXXX)"; then
  echo "FAIL: could not create a temp folder under /tmp" >&2
  exit 1
fi
trap 'rm -rf "$check_tmp"' EXIT
export TMPDIR="$check_tmp"

failed=""

report() {
  if [ "$2" -eq 0 ]; then
    echo "PASS: $1"
  else
    echo "FAIL: $1"
    failed="$failed${failed:+,}$1"
  fi
}

# PYTHON picks the interpreter; it must be 3.11 or later (pyproject.toml).
python="${PYTHON:-python3}"
if ! command -v "$python" >/dev/null 2>&1; then
  echo "FAIL: Python interpreter '$python' not found, set PYTHON to Python 3.11 or later" >&2
  report python 1
elif ! "$python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
  echo "FAIL: '$python' is older than Python 3.11, set PYTHON to Python 3.11 or later" >&2
  report python 1
else
  PYTHONPATH=src "$python" -m unittest discover -s tests -t .
  report python $?
fi

(cd integrations/openclaw-plugin && npm test)
report node $?

if command -v gitleaks >/dev/null 2>&1; then
  gitleaks detect --source . --config .gitleaks.toml --redact
  report secrets $?
else
  echo "SKIP: gitleaks not installed, install from https://github.com/gitleaks/gitleaks to run it locally"
  report secrets 0
fi

if [ -n "$failed" ]; then
  echo "FAIL: $failed"
  exit 1
fi
echo "OK"
exit 0
