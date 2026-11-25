# Continuous integration

`scripts/check.sh` is the one check to run before opening a pull request. It
runs the Python `unittest` suite, the plugin's Node unit tests and a gitleaks
secret scan, in that order. Every step runs even when an earlier one fails, and
the script exits 0 only if all three pass. When gitleaks is not installed
locally, the script prints a SKIP line for the scan and does not count it as a
failure, unless `ECC_CHECK_REQUIRE_GITLEAKS=1` is set: then a missing gitleaks
fails the secrets check. `ECC_CHECK_PYTHON` names the Python interpreter the
script uses (default `python3.11`).

`.github/workflows/ci.yml` runs the same script on GitHub Actions for every pull
request and for every push to `main`. The job runs once with Python 3.11 and
once with Python 3.12, both with Node 22 (the plugin's `engines` field). It
checks out the full history so gitleaks scans every commit, downloads the
gitleaks 8.30.1 release binary, checks its SHA256 against the release checksum
and installs it into `/usr/local/bin`, then calls `scripts/check.sh` with
`ECC_CHECK_REQUIRE_GITLEAKS=1` so the scan cannot be skipped. Actions are
pinned to full commit SHAs, the checkout keeps no credentials, and the
workflow token only has read access to the repository contents.

A copy of the workflow lives in `fixtures/ci/workflow.yml`.
`tests/test_ci_docs.py` checks that the block below, that copy and
`.github/workflows/ci.yml` are identical.

```yaml
name: CI

on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  check:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    strategy:
      fail-fast: false
      matrix:
        python: ["3.11", "3.12"]
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0
        with:
          fetch-depth: 0
          persist-credentials: false
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5.6.0
        with:
          python-version: ${{ matrix.python }}
      - uses: actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020 # v4.4.0
        with:
          node-version: "22"
      - name: Install gitleaks
        env:
          GITLEAKS_VERSION: "8.30.1"
          GITLEAKS_SHA256: "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
        run: |
          set -euo pipefail
          archive="$RUNNER_TEMP/gitleaks.tar.gz"
          curl -fsSL -o "$archive" "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz"
          echo "${GITLEAKS_SHA256}  $archive" | sha256sum -c -
          sudo tar -xzf "$archive" -C /usr/local/bin gitleaks
          gitleaks version
      - name: Check
        env:
          ECC_CHECK_PYTHON: python${{ matrix.python }}
          ECC_CHECK_REQUIRE_GITLEAKS: "1"
        run: scripts/check.sh
```

The plugin's contract suite (`npm run test:contract`) and integration suite
are not part of the workflow. Run the contract suite yourself when a change
touches the plugin or the approval broker.
