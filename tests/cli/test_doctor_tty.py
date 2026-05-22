"""Non-TTY behaviour tests for the CLI.

Covers cli-foundation spec §"非交互环境的 CLI 行为可预测" and the
unknown-subcommand scenario from §"`hostlens` 命令必须作为全局 entrypoint
注册".
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from hostlens.cli import app


@pytest.fixture
def runner() -> CliRunner:
    # CliRunner does not present a TTY to the invoked command, mirroring
    # `hostlens doctor | cat` for output-format purposes. Click >=8.2
    # always separates stdout/stderr; `mix_stderr` is no longer accepted.
    return CliRunner()


def test_doctor_human_output_has_no_ansi_when_not_tty(runner: CliRunner) -> None:
    result = runner.invoke(app, ["doctor"])
    # No ANSI CSI sequences should reach stdout when the destination is
    # not a terminal. Rich detects this via Console(force_terminal=False)
    # by default.
    assert "\x1b[" not in result.stdout


def test_help_lists_doctor_subcommand(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "doctor" in result.stdout


def test_unknown_subcommand_exits_nonzero(runner: CliRunner) -> None:
    result = runner.invoke(app, ["nonexistent-subcommand"])
    assert result.exit_code != 0
    # Typer / Click route usage errors to stderr.
    combined = result.stdout + result.stderr
    assert combined, "expected usage/error output for unknown subcommand"


def test_doctor_help_subflag_exits_zero(runner: CliRunner) -> None:
    result = runner.invoke(app, ["doctor", "--help"])
    assert result.exit_code == 0
    assert "--json" in result.stdout


def test_doctor_does_not_hang_without_stdin(runner: CliRunner) -> None:
    # CliRunner invokes synchronously with no stdin attached; if doctor
    # ever started waiting for input, this test would block.
    result = runner.invoke(app, ["doctor", "--json"], input="")
    assert result.exit_code in (0, 1)
