"""End-to-end exit-code matrix for ``hostlens inspect`` (Group 8.1).

Spec: ``openspec/changes/add-report-data-model/specs/inspect-cli-command/spec.md``
§需求:`hostlens inspect` 退出码必须语义化 4 值.

The exit-code ladder is a closed 4-value set with priority ``3 > 2 > 1 > 0``:

  - ``0`` healthy: ``InspectorResult.status == "ok"`` AND no critical finding
  - ``1`` business: ``status == "ok"`` AND >=1 ``severity == "critical"``
  - ``2`` runner failure: ``status != "ok"``
         (``timeout`` / ``target_unreachable`` / ``requires_unmet`` / ``exception``);
         runner failure **dominates** critical findings (2 wins over 1)
  - ``3`` usage error: target / inspector unknown, ``--parameters`` parse failure,
         ``--output`` write failure, Typer usage error, ``--timeout`` out of range

Group 6 already covers:
  - ``_compute_exit_code`` as a unit (status->code mapping, no CLI plumbing) — 9
    parametrized cases in ``test_inspect.py::test_inspect_compute_exit_code``
  - exit-3 surfaces: missing target / inspector / --format / --timeout boundary /
    --parameters bad / --output write failure / clock-skew → exit 2

This module fills the gap Group 6 leaves: **end-to-end** exit-code assertions
that drive the full ``main()`` pipeline (parameter parse → runner dispatch →
``_compute_exit_code`` → ``sys.exit``) for each ``InspectorStatus`` value, plus
the runner-failure-dominates-critical scenario. We monkeypatch ``_dispatch`` to
hand back a synthetic ``InspectorResult`` so the CLI gets to exercise the real
Report build + render + exit code computation without spinning up a Local
target / shell sub-process for every status.

Why we don't use ``CliRunner.invoke(app, ...)``: it would call the raw Typer
``app`` directly and observe Click's default ``SystemExit(2)`` for usage
errors. The project remaps usage errors to exit 3 only inside
``hostlens.cli.main()``, so the only honest E2E driver is the ``_run_main``
helper (patches ``sys.argv``, calls ``main``, captures via ``capsys``) used
across ``tests/cli/test_inspect.py``. We mirror that here.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from hostlens.cli import inspect as inspect_module
from hostlens.cli import main
from hostlens.inspectors.result import InspectorResult
from hostlens.reporting.models import Finding

# --------------------------------------------------------------------------- #
# Fixtures (mirror Group 6's setup so the registry / target wiring works)
# --------------------------------------------------------------------------- #


@pytest.fixture
def targets_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``Settings.targets_config_path`` at a tmp targets.yaml with ``local-host``."""

    path = tmp_path / "targets.yaml"
    path.write_text(
        yaml.safe_dump(
            {"version": "1", "targets": [{"name": "local-host", "type": "local"}]},
            sort_keys=False,
        )
    )
    monkeypatch.setenv("HOSTLENS_TARGETS_CONFIG_PATH", str(path))
    return path


@pytest.fixture
def user_inspectors_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point inspectors search paths at an empty user dir; builtin ``hello.echo`` still resolves."""

    user_path = tmp_path / "inspectors"
    user_path.mkdir()
    monkeypatch.setenv("HOSTLENS_INSPECTORS_SEARCH_PATHS", str(user_path))
    return user_path


def _run_main(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[int, str, str]:
    """Invoke ``hostlens.cli.main`` end-to-end and capture (exit, stdout, stderr).

    Same shape as ``tests/cli/test_inspect.py::_run_main``; duplicated here to
    keep this module independently runnable without cross-file imports.
    """

    monkeypatch.setattr(sys, "argv", ["hostlens", *argv])
    try:
        main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    else:
        code = 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _make_result(
    *,
    status: str,
    findings: list[Finding] | None = None,
    error: str | None = None,
    missing: list[str] | None = None,
) -> InspectorResult:
    """Construct an ``InspectorResult`` via the real Pydantic model.

    Going through the model (rather than ``MagicMock``) means cross-field
    invariants are enforced — e.g. ``status='ok'`` rejects a non-empty
    ``missing`` list, ``status='requires_unmet'`` requires ``missing`` to be
    non-empty. That keeps test data realistic.
    """

    return InspectorResult(
        name="hello.echo",
        version="1.0.0",
        status=status,  # type: ignore[arg-type]
        target_name="local-host",
        duration_seconds=0.01,
        output={"raw": "hello"} if status == "ok" else {},
        findings=findings or [],
        error=error,
        missing=missing or [],
    )


def _install_dispatch_returning(monkeypatch: pytest.MonkeyPatch, result: InspectorResult) -> None:
    """Replace ``hostlens.cli.inspect._dispatch`` with an async stub.

    The CLI's runner-invocation layer is bypassed; the test focuses on what
    the CLI does with a given ``InspectorResult`` — exit-code computation,
    Report build, render, output emission. Spec-required statuses
    (``timeout`` / ``target_unreachable`` / ``requires_unmet`` / ``exception``)
    are produced by the real runner only under conditions that are hard /
    expensive to reproduce in unit tests (network failures, sleeping
    longer than the configured timeout, etc); a direct stub gives reliable
    coverage of every branch in the exit-code ladder.
    """

    async def _fake_dispatch(*_args: Any, **_kwargs: Any) -> InspectorResult:
        return result

    monkeypatch.setattr(inspect_module, "_dispatch", _fake_dispatch)


# --------------------------------------------------------------------------- #
# Exit code 0 — healthy
# --------------------------------------------------------------------------- #


def test_inspect_exit_0_status_ok_info_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """Spec §场景:healthy 退出 0 — status=ok + only info findings -> exit 0."""

    _install_dispatch_returning(
        monkeypatch,
        _make_result(
            status="ok",
            findings=[Finding(severity="info", message="hello received: hello")],
        ),
    )

    exit_code, stdout, _stderr = _run_main(
        ["inspect", "local-host", "--inspector", "hello.echo"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 0
    assert "# Hostlens Inspection Report" in stdout


def test_inspect_exit_0_status_ok_warning_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """Spec §场景:warning finding 仍退出 0 — warning does NOT flip to 1."""

    _install_dispatch_returning(
        monkeypatch,
        _make_result(
            status="ok",
            findings=[Finding(severity="warning", message="threshold approaching")],
        ),
    )

    exit_code, _stdout, _stderr = _run_main(
        ["inspect", "local-host", "--inspector", "hello.echo"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 0


# --------------------------------------------------------------------------- #
# Exit code 1 — critical finding under status=ok
# --------------------------------------------------------------------------- #


def test_inspect_exit_1_status_ok_critical_finding(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """Spec §场景:critical finding 退出 1.

    status='ok' + >=1 ``severity == 'critical'`` flips exit code from 0
    to 1; the Report still renders to stdout (no error path).
    """

    _install_dispatch_returning(
        monkeypatch,
        _make_result(
            status="ok",
            findings=[
                Finding(severity="critical", message="disk usage > 95%"),
                Finding(severity="info", message="filesystem mounted"),
            ],
        ),
    )

    exit_code, stdout, _stderr = _run_main(
        ["inspect", "local-host", "--inspector", "hello.echo"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 1
    # Report still emitted on stdout (runner finished cleanly, just unhappy
    # business outcome).
    assert "# Hostlens Inspection Report" in stdout
    assert "critical" in stdout.lower()


# --------------------------------------------------------------------------- #
# Exit code 2 — runner failure (4 status values)
# --------------------------------------------------------------------------- #


def test_inspect_exit_2_status_timeout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """Spec §场景:status=timeout 退出 2 + Report still rendered on stdout."""

    _install_dispatch_returning(
        monkeypatch,
        _make_result(status="timeout", error="collect.command exceeded 60 seconds"),
    )

    exit_code, stdout, _stderr = _run_main(
        ["inspect", "local-host", "--inspector", "hello.echo"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 2
    # Spec: "stdout 仍输出完整 Report (含 inspector_result 的 timeout 状态)".
    assert "# Hostlens Inspection Report" in stdout
    assert "timeout" in stdout.lower()


def test_inspect_exit_2_status_target_unreachable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """Spec §场景:status=target_unreachable 退出 2."""

    _install_dispatch_returning(
        monkeypatch,
        _make_result(status="target_unreachable", error="ssh_connection_lost"),
    )

    exit_code, _stdout, _stderr = _run_main(
        ["inspect", "local-host", "--inspector", "hello.echo"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 2


def test_inspect_exit_2_status_requires_unmet(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """Spec §场景:status=requires_unmet 退出 2 — non-empty missing list required."""

    _install_dispatch_returning(
        monkeypatch,
        _make_result(status="requires_unmet", missing=["nginx"]),
    )

    exit_code, _stdout, _stderr = _run_main(
        ["inspect", "local-host", "--inspector", "hello.echo"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 2


def test_inspect_exit_2_status_exception(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """Spec §场景:status=exception 退出 2."""

    _install_dispatch_returning(
        monkeypatch,
        _make_result(status="exception", error="parse_failed: invalid JSON"),
    )

    exit_code, _stdout, _stderr = _run_main(
        ["inspect", "local-host", "--inspector", "hello.echo"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 2


# --------------------------------------------------------------------------- #
# Exit code 2 dominates exit code 1 (runner failure beats critical finding)
# --------------------------------------------------------------------------- #


def test_inspect_runner_failure_dominates_critical_finding(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """Spec §场景:runner 失败优先于 critical finding.

    Theoretical scenario where the runner returns ``status='timeout'`` AND
    a critical finding (a partial result emitted before the timeout hit).
    Priority ladder ``2 > 1`` says we exit 2, not 1.
    """

    _install_dispatch_returning(
        monkeypatch,
        _make_result(
            status="timeout",
            error="collect.command exceeded 60 seconds",
            findings=[Finding(severity="critical", message="partial result before timeout")],
        ),
    )

    exit_code, _stdout, _stderr = _run_main(
        ["inspect", "local-host", "--inspector", "hello.echo"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 2


# --------------------------------------------------------------------------- #
# Exit code 3 — usage / configuration error
# --------------------------------------------------------------------------- #


def test_inspect_exit_3_target_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """Spec §场景:target 未找到退出 3 + stderr hint string locked.

    End-to-end exit-3 for an unknown target name — the resolution layer
    runs **before** ``_dispatch``, so no monkeypatch is needed.
    """

    exit_code, stdout, stderr = _run_main(
        ["inspect", "ghost-host", "--inspector", "hello.echo"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 3
    # stdout must stay clean on the error path.
    assert stdout == ""
    assert "target not found: ghost-host" in stderr
    assert "run 'hostlens target list' to see registered targets" in stderr
