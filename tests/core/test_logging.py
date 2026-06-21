from __future__ import annotations

import json
import os
from typing import Any

import pytest
import structlog

from hostlens.core.logging import configure_logging, redact_sensitive


@pytest.fixture(autouse=True)
def _reset_structlog() -> None:
    """Each test reconfigures structlog explicitly; reset state afterwards.

    Without this, configuration from one test could leak into the next
    because `structlog.configure` mutates module-global state.
    """

    yield
    structlog.reset_defaults()


def _capture_log(
    capsys: pytest.CaptureFixture[str],
    mode: str,
    *,
    event: str,
    **kwargs: Any,
) -> str:
    """Configure logging in the given mode, emit one event, return captured output."""

    configure_logging(mode)  # type: ignore[arg-type]
    logger = structlog.get_logger()
    logger.info(event, **kwargs)
    captured = capsys.readouterr()
    # PrintLoggerFactory writes to stdout by default.
    return captured.out


# ---------------------------------------------------------------------------
# (a) prod mode -> JSON-parseable
# ---------------------------------------------------------------------------


def test_prod_mode_emits_json_line(capsys: pytest.CaptureFixture[str]) -> None:
    out = _capture_log(capsys, "prod", event="hello", k="v")
    line = out.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["event"] == "hello"
    assert payload["k"] == "v"
    assert payload["level"] == "info"
    assert "timestamp" in payload


# ---------------------------------------------------------------------------
# (b) dev mode under TTY mock -> ANSI colour codes present
# ---------------------------------------------------------------------------


def test_dev_mode_under_tty_emits_ansi_colours(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ConsoleRenderer decides colour at configure time via sys.stderr.isatty().
    import sys

    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    out = _capture_log(capsys, "dev", event="hello", k="v")
    assert "\x1b[" in out, f"expected ANSI escape in dev-mode output, got: {out!r}"


# ---------------------------------------------------------------------------
# (c) top-level redaction + non-sensitive fields preserved
# ---------------------------------------------------------------------------


def test_top_level_sensitive_field_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    out = _capture_log(
        capsys,
        "prod",
        event="test",
        anthropic_api_key="sk-ant-realkey",
        username="alice",
    )
    assert "sk-ant-realkey" not in out
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["anthropic_api_key"] == "***"
    assert payload["username"] == "alice"


# ---------------------------------------------------------------------------
# (d) nested dict -> recursive redaction
# ---------------------------------------------------------------------------


def test_nested_dict_recursive_redaction(capsys: pytest.CaptureFixture[str]) -> None:
    out = _capture_log(
        capsys,
        "prod",
        event="env",
        env={"ANTHROPIC_API_KEY": "sk-x", "HOME": "/u/a"},
    )
    assert "sk-x" not in out
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["env"]["ANTHROPIC_API_KEY"] == "***"
    assert payload["env"]["HOME"] == "/u/a"


# ---------------------------------------------------------------------------
# (e) nested list of dicts -> recursive redaction
# ---------------------------------------------------------------------------


def test_nested_list_of_dicts_recursive_redaction(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _capture_log(
        capsys,
        "prod",
        event="targets",
        targets=[{"name": "prod-01", "ssh_key": "BEGIN PRIVATE KEY..."}],
    )
    assert "BEGIN PRIVATE KEY" not in out
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["targets"][0]["ssh_key"] == "***"
    assert payload["targets"][0]["name"] == "prod-01"


# ---------------------------------------------------------------------------
# (f) caller's original data is never mutated
# ---------------------------------------------------------------------------


def test_original_data_is_not_mutated(capsys: pytest.CaptureFixture[str]) -> None:
    data = {"api_key": "sk-x", "nested": {"token": "tk-y"}, "list": [{"secret": "s"}]}
    out = _capture_log(capsys, "prod", event="d", d=data)
    # Logged output is redacted.
    assert "sk-x" not in out
    assert "tk-y" not in out
    # Original dict structure is untouched.
    assert data == {
        "api_key": "sk-x",
        "nested": {"token": "tk-y"},
        "list": [{"secret": "s"}],
    }


# ---------------------------------------------------------------------------
# (g) 9-level nesting must not raise RecursionError (depth cap = 8)
# ---------------------------------------------------------------------------


def test_deep_nesting_does_not_raise(capsys: pytest.CaptureFixture[str]) -> None:
    nested: Any = "leaf"
    for _ in range(9):
        nested = {"nested": nested}
    # Must not raise RecursionError or any exception during processing.
    out = _capture_log(capsys, "prod", event="deep", data=nested)
    # JSON line must still be parseable.
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["event"] == "deep"


# ---------------------------------------------------------------------------
# (h) os.environ (an os._Environ, not a dict) is treated as Mapping
# ---------------------------------------------------------------------------


def test_os_environ_mapping_is_redacted(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leakkey")
    monkeypatch.setenv("HOME", "/tmp/home-fixture")
    out = _capture_log(capsys, "prod", event="dump", env=os.environ)
    assert "sk-ant-leakkey" not in out
    payload = json.loads(out.strip().splitlines()[-1])
    # os._Environ keys round-trip through dict during redaction.
    assert payload["env"]["ANTHROPIC_API_KEY"] == "***"
    assert payload["env"]["HOME"] == "/tmp/home-fixture"


# ---------------------------------------------------------------------------
# Direct processor-level tests (independent of structlog config)
# ---------------------------------------------------------------------------


def test_redact_sensitive_is_pure_function() -> None:
    """`redact_sensitive` must not mutate the input event_dict."""

    original: dict[str, Any] = {
        "api_key": "sk-x",
        "name": "alice",
        "nested": {"token": "tk-y"},
    }
    snapshot = {"api_key": "sk-x", "name": "alice", "nested": {"token": "tk-y"}}
    result = redact_sensitive(None, "info", original)  # type: ignore[arg-type]

    assert original == snapshot, "input event_dict was mutated"
    assert result["api_key"] == "***"
    assert result["nested"]["token"] == "***"
    assert result["name"] == "alice"
    # The nested mapping in the result must be a fresh dict, not the same object.
    assert result["nested"] is not original["nested"]


# ---------------------------------------------------------------------------
# (e) `stream=` selects the sink (stdio-MCP requirement: serve passes stderr)
# ---------------------------------------------------------------------------


def test_stream_param_routes_logs_off_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """`configure_logging(stream=...)` routes ALL output to the given sink,
    keeping stdout byte-clean. `hostlens mcp serve` relies on this (stdout is the
    JSON-RPC protocol stream); a debug emitted after configuring a non-stdout
    sink must not leak onto stdout."""

    import io

    buf = io.StringIO()
    configure_logging("prod", stream=buf)
    structlog.get_logger().debug("routed_event", k="v")

    captured = capsys.readouterr()
    assert captured.out == "", "log leaked onto stdout despite stream= sink"
    payload = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert payload["event"] == "routed_event"
    assert payload["k"] == "v"
