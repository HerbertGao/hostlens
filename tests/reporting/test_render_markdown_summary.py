"""Tests for the `## Summary` block of `render_markdown.render`.

Covers spec §需求:`render_markdown.render` — Summary section shows
counts grouped by severity, or `_No findings._` fallback when empty.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from hostlens.inspectors.result import InspectorResult
from hostlens.reporting.models import Finding, Report
from hostlens.reporting.render_markdown import render


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


def _summary_block(rendered: str) -> str:
    start = rendered.index("## Summary")
    end_marker = "## Findings"
    end = rendered.index(end_marker, start)
    return rendered[start:end]


def test_summary_section_present() -> None:
    out = render(_make_report([]))
    assert "## Summary" in out


def test_summary_empty_findings_renders_no_findings_placeholder() -> None:
    out = render(_make_report([]))
    block = _summary_block(out)
    assert "_No findings._" in block


def test_summary_counts_grouped_by_severity() -> None:
    findings = [
        Finding(severity="info", message="i1"),
        Finding(severity="warning", message="w1"),
        Finding(severity="warning", message="w2"),
        Finding(severity="critical", message="c1"),
        Finding(severity="critical", message="c2"),
        Finding(severity="critical", message="c3"),
    ]
    out = render(_make_report(findings))
    block = _summary_block(out)
    assert "- critical: 3" in block
    assert "- warning: 2" in block
    assert "- info: 1" in block


def test_summary_zero_counts_still_listed() -> None:
    findings = [Finding(severity="info", message="only-info")]
    out = render(_make_report(findings))
    block = _summary_block(out)
    assert "- critical: 0" in block
    assert "- warning: 0" in block
    assert "- info: 1" in block
