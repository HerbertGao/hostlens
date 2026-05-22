"""`hostlens doctor` implementation.

Three checkers report on local environment health:
- `check_python_version()`: interpreter is >= 3.11 (project floor).
- `check_anthropic_key()` : `ANTHROPIC_API_KEY` env var is present.
- `check_config_dir()`    : `~/.config/hostlens/` exists and is readable.

`run_doctor(json_output)` builds a `DoctorReport`, prints it (human Rich
table or strict JSON), and returns the process exit code.

================================================================
SECURITY REVIEW CHECKLIST — `check_anthropic_key()` (M0 task 7.4)
================================================================
Do NOT regress these invariants without an explicit spec update:

- [ ] Function body MUST NOT read `os.environ["ANTHROPIC_API_KEY"]`
      or `os.environ.get("ANTHROPIC_API_KEY")`. Use membership test
      (`"ANTHROPIC_API_KEY" in os.environ`) only — existence-style
      checks have no need for the value.
- [ ] Returned `CheckResult.detail` MUST be the literal `None`. No
      conditional assignment (no length, hash, prefix, suffix, mask,
      or any other value-derived string).
- [ ] No `print()`, no `logger.info()`, no exception messages that
      could capture the env value (even indirectly via f-strings).
- [ ] Any future "validate the key actually works" probe MUST live in
      a separate checker with its own spec entry; do not extend this
      function with side effects.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from hostlens.cli._doctor_schema import CheckResult, DoctorReport
from hostlens.core.config import load_settings
from hostlens.core.logging import configure_logging

__all__ = [
    "check_anthropic_key",
    "check_config_dir",
    "check_python_version",
    "run_doctor",
]


_CONFIG_DIR_DEFAULT = Path("~/.config/hostlens")


def check_python_version() -> CheckResult:
    """Report on the running interpreter version vs the >=3.11 floor."""

    info = sys.version_info
    detail = f"{info.major}.{info.minor}.{info.micro}"
    if (info.major, info.minor) < (3, 11):
        return CheckResult(status="error", detail=detail)
    return CheckResult(status="ok", detail=detail)


def check_anthropic_key() -> CheckResult:
    """Report existence (not value) of `ANTHROPIC_API_KEY`.

    SECURITY: This function intentionally performs ONLY a membership
    test against `os.environ`. It must never read, mask, hash, or
    surface any portion of the key value. See module-level checklist.
    """

    if "ANTHROPIC_API_KEY" in os.environ:
        return CheckResult(status="present", detail=None)
    return CheckResult(status="missing", detail=None)


def check_config_dir() -> CheckResult:
    """Report on `~/.config/hostlens/` existence and readability."""

    path = _CONFIG_DIR_DEFAULT.expanduser()
    path_str = str(path)
    if not path.exists():
        return CheckResult(status="missing", detail=None, path=path_str)
    if not path.is_dir():
        return CheckResult(
            status="error",
            detail="path exists but is not a directory",
            path=path_str,
        )
    if not os.access(path, os.R_OK):
        return CheckResult(status="unreadable", detail=None, path=path_str)
    return CheckResult(status="ok", detail=None, path=path_str)


# Readiness semantics per spec cli-foundation (M0):
# - `python_version`: must be `ok` (interpreter floor is hard).
# - `anthropic_key` : `present` or `missing` both pass (spec: "缺失
#   ANTHROPIC_API_KEY 不阻塞"); only `error` fails.
# - `config_dir`    : `ok` or `missing` both pass (M0 only probes; a
#   non-existent dir is fine because `hostlens` writes nothing there
#   yet). `unreadable` / `error` fail (spec explicitly requires exit 1
#   for the unreadable case).


def _is_ready(checks: dict[str, CheckResult]) -> bool:
    py = checks["python_version"].status
    cfg = checks["config_dir"].status
    key = checks["anthropic_key"].status
    return py == "ok" and cfg in {"ok", "missing"} and key in {"present", "missing"}


def _build_report() -> DoctorReport:
    checks: dict[str, CheckResult] = {
        "python_version": check_python_version(),
        "anthropic_key": check_anthropic_key(),
        "config_dir": check_config_dir(),
    }
    return DoctorReport(
        version="0.1.0",
        timestamp=datetime.now(UTC),
        checks=checks,
        ready=_is_ready(checks),
    )


def _render_human(report: DoctorReport, console: Console) -> None:
    table = Table(title="hostlens doctor")
    table.add_column("check", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("detail")
    for name, result in report.checks.items():
        detail_parts: list[str] = []
        if result.detail is not None:
            detail_parts.append(result.detail)
        if result.path is not None:
            detail_parts.append(f"path={result.path}")
        table.add_row(name, result.status, " ".join(detail_parts))
    console.print(table)
    console.print(f"ready: {report.ready}")


def _emit_remediation(report: DoctorReport, stderr: Console) -> None:
    """Print fix hints to stderr for actionable failures."""

    cfg = report.checks["config_dir"]
    if cfg.status == "unreadable":
        path = cfg.path or str(_CONFIG_DIR_DEFAULT)
        stderr.print(
            f"hint: config directory is not readable; try `chmod 755 {path}`",
        )
    elif cfg.status == "error":
        path = cfg.path or str(_CONFIG_DIR_DEFAULT)
        stderr.print(
            f"hint: {path} exists but is not a directory; remove or replace it",
        )

    py = report.checks["python_version"]
    if py.status == "error":
        stderr.print(
            "hint: hostlens requires Python >=3.11; upgrade your interpreter",
        )


def run_doctor(json_output: bool) -> int:
    """Run all checks, emit output, return process exit code.

    Wires core/config + core/logging into the CLI entrypoint so that
    `HOSTLENS_LOG_MODE` / `HOSTLENS_LOG_LEVEL` take effect for any
    structlog calls made during checks (and from M1+ checkers that may
    emit diagnostics). `load_settings()` raises `ConfigError` on invalid
    user config; we let that propagate so the user sees the validated
    error with sensitive-field redaction (see core/config.py).
    """

    settings = load_settings()
    configure_logging(settings.log_mode)

    report = _build_report()
    stdout = Console(highlight=False, soft_wrap=True)
    stderr = Console(stderr=True, highlight=False, soft_wrap=True)

    if json_output:
        # Strict JSON to stdout only; nothing else may interleave.
        sys.stdout.write(report.model_dump_json(indent=2))
        sys.stdout.write("\n")
        sys.stdout.flush()
    else:
        _render_human(report, stdout)

    _emit_remediation(report, stderr)
    return 0 if report.ready else 1
