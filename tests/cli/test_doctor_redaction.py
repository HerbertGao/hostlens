"""Key-redaction tests for `hostlens doctor`.

Defends the security invariant from cli-foundation spec §"doctor 不泄露
密钥原值": existence-style checks must never surface the key value, mask,
prefix, suffix, or any derived form.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from hostlens.cli import app

_LEAK_SUBSTRINGS: tuple[str, ...] = (
    "secretkey",
    "sk-ant-secretkey",
    # also guard against common masking patterns that would still leak length:
    "sk-ant-",
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_doctor_json_never_contains_key_substring(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secretkey")
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    blob = result.stdout + result.stderr
    for needle in _LEAK_SUBSTRINGS:
        assert needle not in blob, f"doctor leaked {needle!r} into output: {blob!r}"


def test_doctor_anthropic_key_detail_is_strict_null(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secretkey")
    result = runner.invoke(app, ["doctor", "--json"])
    payload = json.loads(result.stdout)
    key_check = payload["checks"]["anthropic_key"]
    assert key_check["status"] == "present"
    # MUST be JSON null; anything else (empty string, mask, length, hash)
    # is a regression of the security checklist in doctor.py.
    assert key_check["detail"] is None


def test_doctor_anthropic_key_detail_null_when_missing(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = runner.invoke(app, ["doctor", "--json"])
    payload = json.loads(result.stdout)
    key_check = payload["checks"]["anthropic_key"]
    assert key_check["status"] == "missing"
    assert key_check["detail"] is None


def test_doctor_human_output_never_contains_key(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secretkey")
    result = runner.invoke(app, ["doctor"])
    blob = result.stdout + result.stderr
    assert "secretkey" not in blob


def test_doctor_debug_log_level_never_contains_key(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secretkey")
    monkeypatch.setenv("HOSTLENS_LOG_LEVEL", "DEBUG")
    result = runner.invoke(app, ["doctor"])
    # stderr should never carry the env value even at DEBUG verbosity.
    assert "secretkey" not in result.stderr
    assert "secretkey" not in result.stdout
