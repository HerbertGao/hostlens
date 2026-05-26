# M1 Report — Demo Path

## Why this demo

This walkthrough exercises the M1 end-to-end path: load Inspectors, run one against a `local` target, render the `Report` to markdown and json, and verify the four CLI exit codes (`0` healthy / `1` critical finding / `2` runner failure / `3` usage error). It runs entirely on the local machine — no SSH, no remote APIs, no paid services.

Estimated runtime: ~3 minutes on a fresh checkout.

## Prerequisites

- Python 3.11+
- `git`, `jq` available on `PATH`
- A POSIX shell (`bash` / `zsh`). All commands below are written for `bash`-compatible shells.

---

## Step 1 — Environment

Create a clean virtualenv and install Hostlens with the `[dev]` extras.

```bash
git clone https://github.com/HerbertGao/hostlens.git
cd hostlens
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

**Expected:** `pip install` finishes without errors. `which hostlens` resolves to a binary inside `.venv/bin/`.

**Troubleshooting:** if `hostlens` is not on `PATH`, confirm the venv is activated (`echo "$VIRTUAL_ENV"`).

---

## Step 2 — Verify Inspectors loaded

```bash
hostlens doctor --json | jq '.inspectors'
```

**Expected:**

```json
{
  "status": "ok",
  "loaded": 2,
  "errors": [],
  "missing_secrets": []
}
```

`loaded: 2` corresponds to the two builtin Inspectors (`hello.echo`, `system.uptime`). The number rises to `3` once Step 8's `sleep_timeout.yaml` is on the search path.

**Troubleshooting:** any non-empty `errors` list means a manifest under the search path failed to load — fix or remove the offending file before continuing.

---

## Step 3 — Register a local target

```bash
hostlens target add local-host --type local
```

**Expected:** stderr/stdout reports success; `hostlens target list` now shows a row for `local-host`.

**Troubleshooting:** if the target already exists from an earlier run (`target with name 'local-host' already exists`), either reuse it or run `hostlens target remove local-host` first.

---

## Step 4 — Run an Inspector (markdown to stdout)

```bash
hostlens inspect local-host --inspector hello.echo
echo "exit=$?"
```

**Expected:**

- stdout: a markdown report starting with `# Hostlens Inspection Report`, a meta table, `## Summary` showing `info: 1`, a `## Findings` block listing the `[INFO] hello received: hello` finding, and an `## Inspector Results` appendix with `Status: ok`.
- `exit=0`.

On the happy path stderr is empty. The CLI raises the structlog filter to WARNING for the inspect command's scope, so info-level events (`inspector_started` / `inspector_finished`) do not fire; only `warning` / `error` log records reach stderr.

**Troubleshooting:** exit `2` indicates the runner failed (commonly because `local-host` is missing or `echo` is unavailable on `PATH`). Re-run Step 3 or check the warning-level log lines on stderr.

---

## Step 5 — Run an Inspector (json to a file)

```bash
hostlens inspect local-host --inspector hello.echo --format json --output /tmp/hostlens-demo.json
echo "exit=$?"
jq '.schema_version, .findings[0].severity' /tmp/hostlens-demo.json
```

**Expected:**

- `exit=0`.
- The file `/tmp/hostlens-demo.json` exists.
- `jq` prints:

  ```text
  "1.0"
  "info"
  ```

**Troubleshooting:** if `--output` write fails (`failed to write output:`), confirm `/tmp` is writable. Exit `3` on a write error is by design.

---

## Step 6 — Failure path: unknown Inspector (exit 3)

```bash
hostlens inspect local-host --inspector nonexistent.foo; echo "exit=$?"
```

**Expected:**

- stderr line: `inspector not found: nonexistent.foo; run 'hostlens inspectors list' to see available inspectors`.
- `exit=3`.

**Troubleshooting:** any exit code other than `3` is a regression — the CLI must distinguish usage errors (`3`) from runner failures (`2`).

---

## Step 7 — Failure path: unknown target (exit 3)

```bash
hostlens inspect ghost-host --inspector hello.echo; echo "exit=$?"
```

**Expected:**

- stderr line: `target not found: ghost-host; run 'hostlens target list' to see registered targets`.
- `exit=3`.

**Troubleshooting:** if exit is `0`, confirm `ghost-host` is not actually registered (`hostlens target list`).

---

## Step 8 — Failure path: runner timeout (exit 2)

This demo uses `examples/m1-report/inspectors/sleep_timeout.yaml`, which runs `sleep N` on the local target. The CLI's `--timeout 1` forces a 1-second budget so the 30-second sleep cleanly trips the runner's timeout path.

```bash
HOSTLENS_INSPECTORS_SEARCH_PATHS=./examples/m1-report/inspectors \
  hostlens inspect local-host \
    --inspector demo.sleep_timeout \
    --parameters '{"sleep_seconds": 30}' \
    --timeout 1
echo "exit=$?"
```

**Expected:**

- `exit=2`.
- The rendered Report contains `Status: timeout` under `## Inspector Results`.
- Duration is approximately `1.0s` (the timeout budget), not 30s.

**Troubleshooting:**

- If exit is `0` or `1`, `--timeout 1` did not propagate — verify the CLI version (`pip show hostlens`).
- If you see `inspector not found: demo.sleep_timeout`, the `HOSTLENS_INSPECTORS_SEARCH_PATHS` env var did not reach the process. The env-var form above (single shell statement with the assignment in front of the command) is the only reliable shape; do not `export` then run on a separate line in a subshell.
- `hostlens doctor --json | jq '.inspectors.errors'` should report `[]` when the search path is set — any entries indicate the manifest failed to load.

---

## Step 9 — Root is allowed (read-only command)

`hostlens inspect` is a read-only command and is permitted to run as root, matching the posture of `hostlens inspectors list/show` and `hostlens target list`.

If you have passwordless `sudo` available:

```bash
sudo hostlens inspect local-host --inspector hello.echo
echo "exit=$?"
```

**Expected:** `exit=0` (root is not refused).

**Equivalent verification without `sudo`:** the CLI deliberately does not check `os.geteuid()`. Confirm with:

```bash
grep -n "geteuid\|EUID\|euid" src/hostlens/cli/inspect.py
```

The only match should be a docstring comment stating the command tolerates `EUID==0`; there must be no runtime refuse branch.

---

## Step 10 — CI replay

```bash
pytest tests/reporting/ tests/cli/test_inspect.py -v
```

**Expected:** all tests pass. The suite includes a `syrupy` snapshot test that asserts byte-level markdown output against a golden file; regenerate with `pytest --snapshot-update` only when an intentional rendering change is shipped.

**Troubleshooting:** a snapshot failure means either (a) the renderer changed and the snapshot must be re-recorded as part of the same change, or (b) the renderer regressed.
