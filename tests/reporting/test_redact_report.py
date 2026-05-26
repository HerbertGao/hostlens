"""Tests for `hostlens.reporting._redact.redact_report_for_render`.

Covers the six redaction scenarios in spec §需求:`render_markdown` /
`render_json` 必须在渲染边界对字符串字段过 `core/redact.py` (the
scenarios that exercise the helper directly, before the renderers
themselves are implemented).
"""

from __future__ import annotations

from datetime import datetime

from hostlens.inspectors.result import InspectorResult
from hostlens.reporting._redact import redact_report_for_render
from hostlens.reporting.models import Evidence, Finding, Report

API_KEY = "sk-abcdefghijklmnopqrstuvwxyz1234567890"
JWT_TOKEN = "eyJhbGciOiJIUzI1NiIs.eyJzdWIiOiIxMjM0NSJ9.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"


def _ir(findings: list[Finding], output: dict[str, object] | None = None) -> InspectorResult:
    return InspectorResult(
        name="x",
        version="1.0.0",
        status="ok",
        target_name="t",
        duration_seconds=0.1,
        output=output or {},
        findings=findings,
        error=None,
        missing=[],
    )


def _t() -> datetime:
    return datetime(2026, 5, 26, 12, 0, 0)


def _make_report(*, finding: Finding, output: dict[str, object] | None = None) -> Report:
    ir = _ir([finding], output=output)
    return Report.from_inspector_results(
        "t",
        [ir],
        started_at=_t(),
        finished_at=_t(),
    )


def test_redact_evidence_stderr_api_key() -> None:
    finding = Finding(
        severity="info",
        message="x",
        evidence=[
            Evidence(
                kind="command_output",
                command="x",
                stdout="",
                stderr=f"ERROR: invalid api_key={API_KEY}",
            )
        ],
    )
    report = _make_report(finding=finding)
    redacted = redact_report_for_render(report)
    redacted_stderr = redacted.inspector_results[0].findings[0].evidence[0].stderr
    assert redacted_stderr is not None
    assert API_KEY not in redacted_stderr


def test_redact_evidence_stdout_jwt() -> None:
    finding = Finding(
        severity="info",
        message="x",
        evidence=[
            Evidence(
                kind="command_output",
                command="x",
                stdout=f"Authorization: Bearer {JWT_TOKEN}",
            )
        ],
    )
    report = _make_report(finding=finding)
    redacted = redact_report_for_render(report)
    redacted_stdout = redacted.inspector_results[0].findings[0].evidence[0].stdout
    assert redacted_stdout is not None
    assert JWT_TOKEN not in redacted_stdout


def test_redact_does_not_mutate_source_report() -> None:
    finding = Finding(
        severity="info",
        message=f"raw secret api_key={API_KEY}",
    )
    report = _make_report(finding=finding)
    _ = redact_report_for_render(report)
    # The source report's finding still contains the unredacted secret.
    assert API_KEY in report.findings[0].message


def test_redact_evidence_data_nested_structure() -> None:
    finding = Finding(
        severity="info",
        message="x",
        evidence=[
            Evidence(
                kind="structured",
                data={"creds": {"password": "p@ssw0rd!supersecret"}, "level": "info"},
            )
        ],
    )
    report = _make_report(finding=finding)
    redacted = redact_report_for_render(report)
    data = redacted.inspector_results[0].findings[0].evidence[0].data
    assert data is not None
    assert "p@ssw0rd!supersecret" not in str(data)
    # Non-secret fields stay readable
    assert data["level"] == "info"


def test_redact_numeric_metric_value_preserved() -> None:
    finding = Finding(
        severity="info",
        message="x",
        evidence=[Evidence(kind="metric", metric_name="load_1min", metric_value=0.42)],
    )
    report = _make_report(finding=finding)
    redacted = redact_report_for_render(report)
    assert redacted.inspector_results[0].findings[0].evidence[0].metric_value == 0.42


def test_redact_metric_value_when_str() -> None:
    finding = Finding(
        severity="info",
        message="x",
        evidence=[
            Evidence(
                kind="metric",
                metric_name="api_key",
                metric_value=f"token={API_KEY}",
            )
        ],
    )
    report = _make_report(finding=finding)
    redacted = redact_report_for_render(report)
    value = redacted.inspector_results[0].findings[0].evidence[0].metric_value
    assert isinstance(value, str)
    assert API_KEY not in value


def test_redact_report_intent_and_metadata() -> None:
    ir = _ir([])
    report = Report.from_inspector_results(
        "t",
        [ir],
        started_at=_t(),
        finished_at=_t(),
        intent=f"investigate api_key={API_KEY}",
        metadata={"trace_id": f"token={API_KEY}"},
    )
    redacted = redact_report_for_render(report)
    assert redacted.intent is not None
    assert API_KEY not in redacted.intent
    assert API_KEY not in redacted.metadata["trace_id"]


def test_redact_finding_preserves_tags_verbatim() -> None:
    """Tags are constrained by Pydantic to `^[a-z][a-z0-9_-]*$` (see
    `reporting.models.Tag`); they cannot legitimately contain anything
    `redact_text` would match. The redactor skips them so the rebuilt
    `Finding` round-trips without risk that a future redact rule could
    mangle a tag into a string that violates the pattern (which would
    raise ValidationError at render time and break the renderer).
    """

    finding = Finding(
        severity="warning",
        message="cpu high",
        tags=["cpu", "perf"],
    )
    report = _make_report(finding=finding)
    redacted = redact_report_for_render(report)
    assert redacted.findings[0].tags == ["cpu", "perf"]
    assert redacted.inspector_results[0].findings[0].tags == ["cpu", "perf"]


def test_redact_inspector_result_output_recurses() -> None:
    finding = Finding(severity="info", message="x")
    report = _make_report(
        finding=finding,
        output={"nested": {"creds": f"password={API_KEY}"}},
    )
    redacted = redact_report_for_render(report)
    output_str = str(redacted.inspector_results[0].output)
    assert API_KEY not in output_str
