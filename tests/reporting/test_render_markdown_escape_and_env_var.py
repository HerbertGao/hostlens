"""Tests for control-character escaping and env-var literal pass-through.

Covers spec §需求:`render_markdown.render` — control bytes (ANSI escape
`\\x1b`, etc.) are escaped to `\\xXX` literals while keeping `\\n` /
`\\t` raw; `$VAR` / `${VAR}` in `Evidence.command` is NOT expanded.
"""

from __future__ import annotations

import os
from datetime import datetime
from uuid import UUID

from hostlens.inspectors.result import InspectorResult
from hostlens.reporting.models import Evidence, Finding, Report
from hostlens.reporting.render_markdown import _escape_control_chars, render


def _make_report(findings: list[Finding]) -> Report:
    ir = InspectorResult(
        name="x",
        version="1.0.0",
        status="ok",
        target_name="t",
        duration_seconds=0.05,
        output={},
        findings=findings,
        error=None,
        missing=[],
    )
    t = datetime(2026, 5, 26, 12, 0, 0)
    return Report(
        report_id=UUID("12345678-1234-5678-1234-567812345678"),
        schema_version="1.0",
        intent=None,
        target_name="t",
        inspector_results=[ir],
        findings=findings,
        started_at=t,
        finished_at=t,
        metadata={},
    )


# ---- _escape_control_chars unit ----------------------------------------------


def test_escape_preserves_newline_and_tab() -> None:
    assert _escape_control_chars("a\nb\tc") == "a\nb\tc"


def test_escape_converts_ansi_escape_to_literal() -> None:
    raw = "\x1b[31mred\x1b[0m"
    escaped = _escape_control_chars(raw)
    assert "\x1b" not in escaped
    assert escaped == "\\x1b[31mred\\x1b[0m"


def test_escape_handles_del_character() -> None:
    # DEL = 0x7f
    assert _escape_control_chars("a\x7fb") == "a\\x7fb"


def test_escape_handles_null_byte() -> None:
    assert _escape_control_chars("a\x00b") == "a\\x00b"


def test_escape_leaves_normal_unicode_alone() -> None:
    assert _escape_control_chars("héllo — 中文") == "héllo — 中文"


# ---- end-to-end render escaping ----------------------------------------------


def test_render_escapes_ansi_in_stdout() -> None:
    ev = Evidence(
        kind="command_output",
        command="cat colored.log",
        stdout="ok\n\x1b[31mred\x1b[0m\n",
    )
    f = Finding(severity="warning", message="ansi", evidence=[ev])
    out = render(_make_report([f]))
    assert "\x1b" not in out
    assert "\\x1b[31mred\\x1b[0m" in out


def test_render_escapes_ansi_in_stderr() -> None:
    ev = Evidence(
        kind="command_output",
        command="bad",
        stdout="",
        stderr="\x1b[1mboom\x1b[0m",
    )
    f = Finding(severity="critical", message="stderr-ansi", evidence=[ev])
    out = render(_make_report([f]))
    assert "\x1b" not in out
    assert "\\x1b[1mboom\\x1b[0m" in out


def test_render_escapes_ansi_in_excerpt() -> None:
    ev = Evidence(
        kind="file_excerpt",
        path="/var/log/x",
        excerpt="line1\n\x1b[31mERR\x1b[0m\n",
    )
    f = Finding(severity="warning", message="excerpt-ansi", evidence=[ev])
    out = render(_make_report([f]))
    assert "\x1b" not in out
    assert "\\x1b[31mERR\\x1b[0m" in out


def test_render_escapes_control_chars_in_inspector_error() -> None:
    ir = InspectorResult(
        name="x",
        version="1.0.0",
        status="exception",
        target_name="t",
        duration_seconds=0.0,
        output={},
        findings=[],
        error="boom \x1b[31m!!!\x1b[0m",
        missing=[],
    )
    t = datetime(2026, 5, 26, 12, 0, 0)
    r = Report(
        report_id=UUID("12345678-1234-5678-1234-567812345678"),
        schema_version="1.0",
        intent=None,
        target_name="t",
        inspector_results=[ir],
        findings=[],
        started_at=t,
        finished_at=t,
        metadata={},
    )
    out = render(r)
    assert "\x1b" not in out
    assert "\\x1b[31m!!!\\x1b[0m" in out


# ---- env-var non-expansion ---------------------------------------------------


def test_env_var_in_command_is_not_expanded(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("PGHOST", "db.prod.internal")
    monkeypatch.setenv("PGUSER", "admin")
    ev = Evidence(
        kind="command_output",
        command="psql -h $PGHOST -U $PGUSER",
        stdout="",
    )
    f = Finding(severity="info", message="env", evidence=[ev])
    out = render(_make_report([f]))
    assert "psql -h $PGHOST -U $PGUSER" in out
    assert "db.prod.internal" not in out
    assert "admin" not in out
    # Spot-check the rendered command did not call os.path.expandvars.
    assert os.path.expandvars("$PGHOST") == "db.prod.internal"


def test_curly_env_var_in_command_is_not_expanded(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("HOST", "secret-host")
    ev = Evidence(
        kind="command_output",
        command="ssh ${HOST}",
        stdout="",
    )
    f = Finding(severity="info", message="env2", evidence=[ev])
    out = render(_make_report([f]))
    assert "ssh ${HOST}" in out
    assert "secret-host" not in out
