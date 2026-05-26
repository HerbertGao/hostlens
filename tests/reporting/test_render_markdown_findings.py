"""Tests for the `## Findings` block of `render_markdown.render`.

Covers spec §需求:`render_markdown.render` — severity descending order
(critical → warning → info), `<details>` collapse block for non-empty
evidence, and no `<details>` when evidence list is empty.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from hostlens.inspectors.result import InspectorResult
from hostlens.reporting.models import Evidence, Finding, Report
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


def test_findings_section_present() -> None:
    out = render(_make_report([Finding(severity="info", message="x")]))
    assert "## Findings" in out


def test_findings_sorted_by_severity_descending() -> None:
    findings = [
        Finding(severity="info", message="i"),
        Finding(severity="critical", message="c"),
        Finding(severity="warning", message="w"),
    ]
    out = render(_make_report(findings))
    idx_c = out.index("### [CRITICAL] c")
    idx_w = out.index("### [WARNING] w")
    idx_i = out.index("### [INFO] i")
    assert idx_c < idx_w < idx_i


def test_finding_title_uppercase_severity_bracketed() -> None:
    out = render(_make_report([Finding(severity="warning", message="hello")]))
    assert "### [WARNING] hello" in out


def test_finding_no_evidence_does_not_render_details() -> None:
    out = render(_make_report([Finding(severity="info", message="bare")]))
    assert "<details>" not in out
    assert "<summary>" not in out


def test_finding_with_evidence_renders_details_block_and_count() -> None:
    ev1 = Evidence(kind="command_output", command="echo a", stdout="a\n")
    ev2 = Evidence(kind="file_excerpt", path="/etc/hosts", excerpt="127.0.0.1\n")
    f = Finding(severity="critical", message="db down", evidence=[ev1, ev2])
    out = render(_make_report([f]))
    assert "<details><summary>Evidence (2 items)</summary>" in out
    # Both evidences must appear inside the details.
    assert "echo a" in out
    assert "/etc/hosts" in out
    assert "</details>" in out


def test_finding_tags_rendered_when_present() -> None:
    f = Finding(severity="info", message="m", tags=["cpu", "perf"])
    out = render(_make_report([f]))
    assert "_tags: cpu, perf_" in out
