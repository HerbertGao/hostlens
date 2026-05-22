"""Sensitive-field redaction tests for `load_settings()` / ConfigError.

These tests dynamically subclass `Settings` to add fields with sensitive
names, then trigger validation failures and assert the formatted error
message (a) names the field, (b) contains the redaction marker `***`,
(c) contains **no** substring (>=4 chars) of the original input value.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from hostlens.core.config import (
    _SENSITIVE_FIELD_PATTERN,
    Settings,
    _format_validation_error,
)
from hostlens.core.exceptions import ConfigError

SECRET_VALUE = "sk-ant-realleak123"
"""Synthetic leaked-looking value used across redaction tests."""


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in list(os.environ):
        if key.startswith("HOSTLENS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


def _windowed_substrings(value: str, window: int = 4) -> list[str]:
    """All length-`window` contiguous substrings of `value`.

    Used to enforce strict redaction: even truncated leakage of >=4 chars
    of the secret must fail the test (defense against accidental prefix
    leaks like `sk-a...`).
    """

    return [value[i : i + window] for i in range(len(value) - window + 1)]


def _assert_strictly_redacted(message: str, secret: str) -> None:
    """Assert no >=4-char window of `secret` appears in `message`."""

    for window in _windowed_substrings(secret, window=4):
        assert window not in message, (
            f"redaction leak: substring {window!r} of secret {secret!r} "
            f"appeared in error message: {message!r}"
        )


# ---------------------------------------------------------------------------
# (a) + (b) + (c): redaction for `anthropic_api_key` via load_settings() path
# ---------------------------------------------------------------------------


def _build_validation_error(field_name: str, bad_value: Any) -> ValidationError:
    """Force a `ValidationError` whose single error.loc == (field_name,).

    Dynamically subclasses `Settings` so the field uses the project's real
    formatter pipeline (`_format_validation_error`). Field is typed as
    `int` so passing a string yields an `int_parsing` validation error
    with the raw input preserved on the error dict.
    """

    sub_cls = type(
        "SensitiveSettings",
        (Settings,),
        {"__annotations__": {field_name: int}},
    )
    with pytest.raises(ValidationError) as excinfo:
        sub_cls(**{field_name: bad_value})
    return excinfo.value


def test_anthropic_api_key_value_is_redacted_in_error_message() -> None:
    ve = _build_validation_error("anthropic_api_key", SECRET_VALUE)
    message = _format_validation_error(ve)

    assert "anthropic_api_key" in message, "field name must surface in error"
    assert "***" in message, "redaction marker must appear"
    _assert_strictly_redacted(message, SECRET_VALUE)


def test_load_settings_redacts_sensitive_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: load_settings() -> ConfigError with redacted message."""

    # Dynamically extend Settings with a sensitive field bound to env var.
    sub_cls = type(
        "EnvSensitiveSettings",
        (Settings,),
        {"__annotations__": {"anthropic_api_key": int}},
    )

    monkeypatch.setenv("HOSTLENS_ANTHROPIC_API_KEY", SECRET_VALUE)

    # Re-implement the load_settings flow against the dynamic subclass so we
    # exercise the same redaction code path the public factory uses.
    try:
        sub_cls()
    except ValidationError as ve:
        message = _format_validation_error(ve)
        raised = ConfigError(message, original=ve)
    else:  # pragma: no cover - validation must fail
        pytest.fail("expected ValidationError from sensitive-int field")

    msg = str(raised)
    assert "anthropic_api_key" in msg
    assert "***" in msg
    _assert_strictly_redacted(msg, SECRET_VALUE)


# ---------------------------------------------------------------------------
# (d) field-name pattern coverage — five distinct sensitive name shapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field_name",
    ["api_key", "auth_token", "client_secret", "db_password", "aws_credential"],
)
def test_each_sensitive_pattern_triggers_redaction(field_name: str) -> None:
    ve = _build_validation_error(field_name, SECRET_VALUE)
    message = _format_validation_error(ve)

    assert field_name in message
    assert "***" in message
    _assert_strictly_redacted(message, SECRET_VALUE)


@pytest.mark.parametrize(
    "field_name",
    ["api_key", "auth_token", "client_secret", "db_password", "aws_credential"],
)
def test_sensitive_pattern_matches_directly(field_name: str) -> None:
    """Module-level regex sanity: each name truly matches the pattern."""

    assert _SENSITIVE_FIELD_PATTERN.search(field_name) is not None


def test_non_sensitive_field_name_does_not_match_pattern() -> None:
    for safe in ("log_level", "log_mode", "config_dir", "hostname", "port"):
        assert _SENSITIVE_FIELD_PATTERN.search(safe) is None


def test_non_sensitive_field_preserves_actual_value_in_message() -> None:
    """Counter-test to (d): a non-sensitive field must keep its raw value
    so users can debug. We use the existing `log_level` Literal field.
    """

    from hostlens.core.config import load_settings

    # monkeypatch.setenv isn't usable here without a fixture, so set/unset
    # manually to keep this test self-contained.
    os.environ["HOSTLENS_LOG_LEVEL"] = "NotALevel"
    try:
        with pytest.raises(ConfigError) as excinfo:
            load_settings()
    finally:
        del os.environ["HOSTLENS_LOG_LEVEL"]

    msg = str(excinfo.value)
    assert "log_level" in msg
    assert "NotALevel" in msg, "non-sensitive value must be preserved for debugging"
    assert "***" not in msg, "non-sensitive value must not be redacted"
