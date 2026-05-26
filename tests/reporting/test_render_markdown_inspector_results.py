"""Tests for the `## Inspector Results` appendix block.

Covers spec §需求:`render_markdown.render` — `**Status:**` /
`**Error:**` explicit lines when InspectorResult.status != "ok", and
JSON-fenced `output` block.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from hostlens.inspectors.result import InspectorResult
from hostlens.reporting.models import Report
from hostlens.reporting.render_markdown import render


def _make_report(ir: InspectorResult) -> Report:
    t = datetime(2026, 5, 26, 12, 0, 0)
    return Report(
        report_id=UUID("12345678-1234-5678-1234-567812345678"),
        schema_version="1.0",
        intent=None,
        target_name="t",
        inspector_results=[ir],
        findings=ir.findings,
        started_at=t,
        finished_at=t,
        metadata={},
    )


def test_section_header_and_inspector_name() -> None:
    ir = InspectorResult(
        name="hello.echo",
        version="1.0.0",
        status="ok",
        target_name="local-host",
        duration_seconds=0.05,
        output={"k": "v"},
        findings=[],
        error=None,
        missing=[],
    )
    out = render(_make_report(ir))
    assert "## Inspector Results" in out
    assert "### hello.echo" in out
    assert "- **Version:** 1.0.0" in out
    assert "- **Target:** local-host" in out
    assert "- **Duration (s):** 0.05" in out


def test_status_timeout_with_error_is_explicit() -> None:
    ir = InspectorResult(
        name="demo.sleep_timeout",
        version="1.0.0",
        status="timeout",
        target_name="t",
        duration_seconds=1.00,
        output={},
        findings=[],
        error="collect.command exceeded 60 seconds",
        missing=[],
    )
    out = render(_make_report(ir))
    assert "- **Status:** timeout" in out
    assert "- **Error:** collect.command exceeded 60 seconds" in out


def test_output_rendered_as_json_fenced_code_block() -> None:
    ir = InspectorResult(
        name="x",
        version="1.0.0",
        status="ok",
        target_name="t",
        duration_seconds=0.01,
        output={"a": 1, "b": ["x", "y"]},
        findings=[],
        error=None,
        missing=[],
    )
    out = render(_make_report(ir))
    assert "```json" in out
    # JSON should be deterministic (sorted keys).
    assert '"a": 1' in out
    assert '"b":' in out
    assert "```\n" in out


def test_missing_field_rendered_when_requires_unmet() -> None:
    ir = InspectorResult(
        name="x",
        version="1.0.0",
        status="requires_unmet",
        target_name="t",
        duration_seconds=0.0,
        output={},
        findings=[],
        error="missing binaries",
        missing=["jq", "curl"],
    )
    out = render(_make_report(ir))
    assert "- **Status:** requires_unmet" in out
    assert "- **Missing:** jq, curl" in out
