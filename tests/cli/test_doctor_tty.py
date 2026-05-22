"""Non-TTY behaviour tests for the CLI.

Covers cli-foundation spec §"非交互环境的 CLI 行为可预测" and the
unknown-subcommand scenario from §"`hostlens` 命令必须作为全局 entrypoint
注册".
"""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from hostlens.cli import app

# Typer >=0.12 uses Rich for `--help`. In CI (GitHub Actions sets CI=true)
# Rich force-enables colors AND box-drawing layout, which inserts ANSI
# escapes and may wrap flag names like `--json` across lines, breaking
# naive substring assertions. Normalise help output before matching:
#   1. strip ANSI CSI sequences,
#   2. collapse whitespace (incl. newlines and box-drawing wraps).
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_BOX_RE = re.compile(r"[│─╭╮╰╯┃━┏┓┗┛]+")


def _normalise(text: str) -> str:
    no_ansi = _ANSI_RE.sub("", text)
    no_box = _BOX_RE.sub(" ", no_ansi)
    return re.sub(r"\s+", " ", no_box)


@pytest.fixture
def runner() -> CliRunner:
    # CliRunner does not present a TTY to the invoked command, mirroring
    # `hostlens doctor | cat` for output-format purposes. Click >=8.2
    # always separates stdout/stderr; `mix_stderr` is no longer accepted.
    return CliRunner()


def test_doctor_human_output_has_no_ansi_when_not_tty(runner: CliRunner) -> None:
    result = runner.invoke(app, ["doctor"])
    # No ANSI CSI sequences should reach stdout when the destination is
    # not a terminal. doctor.py uses Console(force_terminal=False) for
    # the human path; this guards against accidental colour leakage.
    assert "\x1b[" not in result.stdout


def test_help_lists_doctor_subcommand(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.stdout + result.stderr
    # Typer/Rich help may wrap or colour the command name; normalise first.
    assert "doctor" in _normalise(result.stdout)


def test_unknown_subcommand_exits_nonzero(runner: CliRunner) -> None:
    result = runner.invoke(app, ["nonexistent-subcommand"])
    assert result.exit_code != 0
    # Typer / Click route usage errors to stderr.
    combined = result.stdout + result.stderr
    assert combined, "expected usage/error output for unknown subcommand"


def test_doctor_help_subflag_exits_zero(runner: CliRunner) -> None:
    result = runner.invoke(app, ["doctor", "--help"])
    assert result.exit_code == 0
    # Typer/Rich help renders inside a box that wraps `--json` across lines
    # (esp. under CI=true where Rich force-enables layout). Normalise before
    # matching so the assertion remains stable across local TTY / piped /
    # CI environments.
    assert "--json" in _normalise(result.stdout)


def test_doctor_does_not_hang_without_stdin(runner: CliRunner) -> None:
    # CliRunner invokes synchronously with no stdin attached; if doctor
    # ever started waiting for input, this test would block.
    result = runner.invoke(app, ["doctor", "--json"], input="")
    assert result.exit_code in (0, 1)
