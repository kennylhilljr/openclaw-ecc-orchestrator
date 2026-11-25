#!/usr/bin/env bash
# Runs the Python tests, the plugin tests and the secret scan, in that order.
# Every check runs even if an earlier one fails. Exits 0 only if all pass.
# ECC_CHECK_PYTHON names the Python interpreter (default python3.11).
# ECC_CHECK_REQUIRE_GITLEAKS=1 turns a missing gitleaks into a failure; CI sets it.
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

PYTHONPATH=src "${ECC_CHECK_PYTHON:-python3.11}" -m unittest discover -s tests -t .
report python $?

(cd integrations/openclaw-plugin && npm test)
report node $?

if command -v gitleaks >/dev/null 2>&1; then
  gitleaks detect --source . --config .gitleaks.toml --redact
  report secrets $?
elif [ "${ECC_CHECK_REQUIRE_GITLEAKS:-}" = "1" ]; then
  echo "FAIL: gitleaks not installed and ECC_CHECK_REQUIRE_GITLEAKS=1, so the secret scan cannot be skipped"
  report secrets 1
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
