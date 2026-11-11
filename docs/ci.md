# Continuous integration

`scripts/check.sh` is the one check to run before opening a pull request. It
runs the Python `unittest` suite, the plugin's Node unit tests and a gitleaks
secret scan, in that order. Every step runs even when an earlier one fails, and
the script exits 0 only if all three pass. When gitleaks is not installed
locally, the script prints a SKIP line for the scan and does not count it as a
failure.

The workflow below runs the same script on GitHub Actions for every pull
request and for every push to `main`. The job checks out the repository, sets
up Python 3.11 and Node 22, downloads the gitleaks 8.30.1 release binary into
`/usr/local/bin`, and then calls `scripts/check.sh`. The workflow token only
has read access to the repository contents.

The worker bots cannot write under `.github/`, so the workflow is not active in
this repository yet. Kenny adds it by hand as `.github/workflows/ci.yml`. A
copy lives in `fixtures/ci/workflow.yml`, and `tests/test_ci_docs.py` checks
that the block below matches that file exactly.

```yaml
name: CI

on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - uses: actions/setup-node@v4
        with:
          node-version: "22"
      - name: Install gitleaks
        run: curl -fsSL -o "$RUNNER_TEMP/gitleaks.tar.gz" https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz && sudo tar -xzf "$RUNNER_TEMP/gitleaks.tar.gz" -C /usr/local/bin gitleaks
      - name: Check
        run: scripts/check.sh
```

The plugin's contract suite (`npm run test:contract`) and integration suite
are not part of the workflow. Run the contract suite yourself when a change
touches the plugin or the approval broker.
