# `hostlens inspect` Operations Guide

`hostlens inspect <target> --inspector <name>` is the M1 user-facing
entry point for running a single Inspector against a registered target
and rendering a `Report` to stdout (or a file via `--output`). This
document covers the demo path, exit-code semantics, the redaction
boundary, and the small set of accepted operational risks shipped with
this command.

## Command synopsis

```
hostlens inspect <target> --inspector <name>
                          [--output FILE]
                          [--format md|json]
                          [--parameters JSON|@FILE]
                          [--allow-privileged]
                          [--timeout SECONDS]
```

- `target` — positional, must already be registered (see
  `hostlens target list`).
- `--inspector / -i` — required, must be loaded into the
  `InspectorRegistry` (see `hostlens inspectors list`).
- `--output / -o` — write the rendered Report to a file. stdout is
  silent in that case. See "Known accepted risks" for the overwrite
  semantics.
- `--format / -f` — `md` (default) or `json`. Format and `--output`
  file suffix are deliberately not cross-validated.
- `--parameters / -p` — either inline JSON (must start with `{`) or
  `@/path/to/file.json` (must start with `@`). Anything else is
  rejected with exit 3.
- `--allow-privileged` — opt-in for inspectors whose manifest declares
  `privilege != "none"`.
- `--timeout` — override `collect.timeout_seconds` for this run.
  Integer in `[1, 300]`. Values outside the range exit 3; the CLI
  reconstructs `CollectSpec(**{...})` so the manifest-level Pydantic
  validation still triggers as defence in depth.

## Demo Path (10 steps)

The steps below are copy-pasteable on a clean macOS / Linux machine
with no SSH access and no paid API keys. They mirror the proposal
demo path and the contents of `examples/m1-report/README.md` once
that example lands.

```bash
# 1. Environment prep (~30s)
git clone <repo-url> hostlens && cd hostlens
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Loader sanity check (~10s)
hostlens doctor --json | jq '.inspectors'
# expect: {"status": "ok", "loaded": 2, "errors": []}

# 3. Register a local execution target (~10s)
hostlens target add local-host --type local

# 4. Run inspect, default markdown to stdout (~10s)
hostlens inspect local-host --inspector hello.echo
# expect: '# Hostlens Inspection Report' on stdout, exit 0

# 5. Run inspect, JSON to file (~10s)
hostlens inspect local-host --inspector hello.echo \
  --format json --output /tmp/hostlens-demo.json
jq '.schema_version, .findings[0].severity' /tmp/hostlens-demo.json
# expect: "1.0" then "info", exit 0

# 6. Failure path: inspector missing (~10s)
hostlens inspect local-host --inspector nonexistent.foo; echo "exit=$?"
# expect: stderr "inspector not found: nonexistent.foo; run
#         'hostlens inspectors list' to see available inspectors"
# expect: exit=3

# 7. Failure path: target missing (~10s)
hostlens inspect ghost-host --inspector hello.echo; echo "exit=$?"
# expect: stderr "target not found: ghost-host; run
#         'hostlens target list' to see registered targets"
# expect: exit=3

# 8. Failure path: runner internal failure -> exit 2 (~60s)
HOSTLENS_INSPECTORS_SEARCH_PATHS=./examples/m1-report/inspectors \
  hostlens inspect local-host \
    --inspector demo.sleep_timeout \
    --parameters '{"sleep_seconds": 30}' \
    --timeout 1; echo "exit=$?"
# expect: rendered Report on stdout with status: timeout, exit=2

# 9. Allow EUID==0 (read-only command) (~10s)
sudo hostlens inspect local-host --inspector hello.echo
# expect: exit 0; the command does not refuse root

# 10. CI replay verification (~60s)
pytest tests/reporting/ tests/cli/test_inspect.py -v
# expect: all green, including the syrupy snapshot test
```

## Exit-code semantics

`hostlens inspect` returns exactly one of four exit codes. The
priority is `3 > 2 > 1 > 0` — usage errors mask runner failures, which
mask business critical findings, which mask healthy runs.

| exit | meaning | user-visible state |
|---|---|---|
| `0` | `inspector_result.status == "ok"` AND every finding has `severity <= "warning"` | healthy run |
| `1` | `inspector_result.status == "ok"` AND at least one finding has `severity == "critical"` | business problem detected |
| `2` | `inspector_result.status != "ok"` (`timeout` / `target_unreachable` / `requires_unmet` / `exception`) OR `Report` model validation failure (e.g. system clock skew making `finished_at < started_at`) | runner / runtime failure |
| `3` | usage error: target / inspector not found, `--parameters` JSON or file failure, `--output` write failure, `--timeout` out of `[1, 300]`, Typer usage errors (`Missing argument`, `Invalid value for ...`) | CLI / config error |

stdout always carries the rendered Report (or is empty when
`--output` is used or when exit code is 3). All errors and warnings go
to stderr. Python tracebacks are never surfaced; CLI-boundary
exceptions are wrapped as `internal: <kind>: <brief>` one-liners.

### CI scriptlet

Wrap `hostlens inspect` in CI so the four exit codes map to four
distinct alerting actions. The script below treats exit 2 as a
retry-able runner problem, exit 1 as a real alert, and exit 3 as a
broken pipeline configuration.

```bash
#!/usr/bin/env bash
set -uo pipefail

REPORT=/tmp/hostlens-prod-web.json

if hostlens inspect prod-web \
    --inspector linux.cpu.load \
    --format json \
    --output "$REPORT"; then
  exit_code=$?
else
  exit_code=$?
fi

case "$exit_code" in
  0)
    echo "healthy: $(jq -r '.report_id' "$REPORT")"
    ;;
  1)
    /usr/local/bin/page-oncall \
      "critical finding in linux.cpu.load on prod-web (see $REPORT)"
    ;;
  2)
    /usr/local/bin/retry-or-alert \
      "hostlens runner failure on prod-web (see $REPORT)"
    ;;
  3)
    echo "usage error, fix CI config" >&2
    exit 1
    ;;
  *)
    echo "unexpected exit code $exit_code" >&2
    exit 1
    ;;
esac
```

Notes for CI authors:

- Do not assume stdout is always populated — `--output` writes the
  Report to a file and leaves stdout silent.
- `set -e` is intentionally omitted; `hostlens inspect` returning 1 / 2
  is a normal control-flow signal, not a shell failure.
- exit 3 should fail the pipeline loudly (broken config). exit 1 / 2
  should route to your alerting / retry layer, not abort the job.

## Redaction boundary

`hostlens inspect` applies the `hostlens.core.redact.redact_text`
patterns from `docs/OPERABILITY.md` §7.2 at the rendering boundary
(`render_markdown.render` and `render_json.render`) before any byte
reaches stdout, `--output` files, or downstream sinks. The in-memory
`Report` / `InspectorResult` objects held by the runner are **not**
redacted — reasoning code (Agent loop, M3 diagnostician) still sees
raw strings.

Default regex coverage from OPERABILITY §7.2:

- `(password|secret|token|api[_-]?key|bearer)\s*[:=]\s*\S+`
- JWT-shaped `eyJ...`.`...`.`...`
- Anthropic / OpenAI keys `sk-[a-zA-Z0-9-]{20,}`

Redaction preserves the first 4 and last 4 characters of the matched
secret (e.g. `sk-abcd...7890`) so operators can still correlate alerts
without leaking the full secret.

**The default rules in OPERABILITY §7.2 are enforced at render time,
but they are not exhaustive.** Custom token formats (proprietary
HMAC schemes, vendor-specific API keys outside the `sk-` / JWT
families, internal session cookies) are **not** matched by the M1
ruleset. Adding such rules requires extending the redaction
configuration — that configuration surface is intentionally deferred
to the M5 Notifier proposal, which will design a single config entry
point shared between `hostlens inspect` stdout, `--output` files, and
all Notifier payloads. Until then, treat the OPERABILITY §7.2
defaults as a floor, not a ceiling.

Additional rendering-side hardening that ships with this command:

- `Evidence.command` is rendered as the manifest template string;
  shell-style `$VAR` / `${VAR}` references are **not** expanded by the
  renderer. Concrete env-var values stay out of the report.
- C0 control characters (`\x00-\x1f`) and DEL (`\x7f`) in `stdout` /
  `stderr` / `excerpt` / `command` / `InspectorResult.error` are
  escaped to literal `\xXX` sequences (newline and tab are preserved).
  ANSI escapes cannot pollute terminal state or confuse downstream
  markdown renderers. C1 control characters (`\x80-\x9f`) are not
  escaped in M1 — extending the range would need a follow-up proposal.

## Known accepted risks

These are deliberate M1 trade-offs. They are tracked here so reviewers
and operators do not mistake them for bugs.

- **`--output` silently overwrites an existing file.** The CLI does
  not implement a `--force` opt-in or a "file exists" prompt. The
  rationale is POSIX consistency with `cp -f`, `tar -f`, and `>`
  shell redirection. Wrap the call in a guard
  (`[[ -e "$OUT" ]] && exit 1`) if your CI cannot tolerate silent
  overwrites.
- **Large reports warn but do not fail.** When
  `Report.total_evidence_bytes() > 8 MiB`, the CLI emits a warning to
  stderr but still writes the full Report. There is no truncation
  and no `--max-bytes` flag in M1; the upstream runner already caps
  individual stdout / stderr captures at 1 MiB so this only triggers
  in pathological multi-evidence cases.
- **Redaction covers OPERABILITY §7.2 defaults only.** As noted in
  the "Redaction boundary" section above, custom token shapes need
  the M5 Notifier-driven redaction config surface. The M1 ruleset is
  a floor, not a ceiling — do not rely on it to catch arbitrary
  secret formats.
- **`--output` files inherit the process umask.** The CLI does not
  force `0o600`. Use `umask 077` (or a more restrictive value) before
  invoking `hostlens inspect` if the output path could be world-
  readable. This matches the behaviour of `targets.yaml` writes from
  `hostlens target add`.
- **`--format` and `--output` suffix are not cross-validated.**
  `--format json --output report.md` is accepted. The CLI does not
  second-guess file naming.
