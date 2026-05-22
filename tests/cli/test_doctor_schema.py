"""JSON schema contract tests for `hostlens doctor --json`.

Locks down the stable contract from cli-foundation spec §"`hostlens doctor
--json` 输出稳定 schema" and design.md D-9.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest
from typer.testing import CliRunner

from hostlens.cli import app

_VALID_STATUSES: frozenset[str] = frozenset(["ok", "present", "missing", "unreadable", "error"])
_REQUIRED_CHECK_KEYS: frozenset[str] = frozenset(["python_version", "anthropic_key", "config_dir"])


@pytest.fixture
def runner() -> CliRunner:
    # Click >=8.2 always separates stdout/stderr; `mix_stderr` is gone.
    return CliRunner()


def _invoke_json(runner: CliRunner) -> dict[str, Any]:
    result = runner.invoke(app, ["doctor", "--json"])
    # exit code is allowed to be 0 or 1 depending on local env health; schema
    # contract must hold regardless. We only assert stdout parses.
    assert result.exit_code in (0, 1), result.stdout + result.stderr
    return json.loads(result.stdout)


def test_doctor_json_exits_zero_when_ready(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate spec §"全部检查通过": key present + (config_dir handled by
    # readiness rule that allows missing). Python version is whatever the
    # test interpreter is (>=3.11 per pyproject).
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-placeholder")
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ready"] is True


def test_doctor_json_has_required_top_level_fields(runner: CliRunner) -> None:
    payload = _invoke_json(runner)
    for field in ("version", "timestamp", "checks", "ready"):
        assert field in payload, f"missing top-level field: {field}"
    assert isinstance(payload["version"], str) and payload["version"]
    assert isinstance(payload["ready"], bool)
    # timestamp must parse as ISO 8601
    datetime.fromisoformat(payload["timestamp"])
    assert isinstance(payload["checks"], dict)


def test_doctor_json_version_matches_contract(runner: CliRunner) -> None:
    payload = _invoke_json(runner)
    # Bumping this requires a breaking spec change (design.md D-9).
    assert payload["version"] == "0.1.0"


def test_doctor_json_has_three_required_checks(runner: CliRunner) -> None:
    payload = _invoke_json(runner)
    actual = set(payload["checks"].keys())
    missing = _REQUIRED_CHECK_KEYS - actual
    assert not missing, f"missing required check keys: {sorted(missing)}"


def test_doctor_json_every_check_status_in_enum(runner: CliRunner) -> None:
    payload = _invoke_json(runner)
    for name, check in payload["checks"].items():
        assert "status" in check, f"check {name!r} missing status"
        assert check["status"] in _VALID_STATUSES, (
            f"check {name!r} has invalid status {check['status']!r}"
        )
