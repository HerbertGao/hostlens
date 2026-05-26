"""Tests for `Report` Pydantic model.

Covers spec §需求:`Report` Pydantic 模型必须严格 conform M1 字段集与
schema_version 锁定. Imports `hostlens.inspectors.result` first to
trigger the forward-ref resolution that lives at the bottom of that
module (per design.md §决策 1 末尾的循环导入处理).
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

# Import inspectors.result first → triggers Report.model_rebuild() so the
# `inspector_results: list[InspectorResult]` forward-ref resolves.
from hostlens.inspectors.result import InspectorResult
from hostlens.reporting.models import Evidence, Finding, Report


def _make_ir(name: str = "hello.echo", findings: list[Finding] | None = None) -> InspectorResult:
    return InspectorResult(
        name=name,
        version="1.0.0",
        status="ok",
        target_name="local-host",
        duration_seconds=0.5,
        output={},
        findings=findings or [],
        error=None,
        missing=[],
    )


def _now() -> datetime:
    return datetime(2026, 5, 26, 12, 0, 0)


def test_report_minimal_construction() -> None:
    r = Report(
        report_id=uuid4(),
        schema_version="1.0",
        target_name="local-host",
        inspector_results=[_make_ir()],
        started_at=_now(),
        finished_at=_now(),
    )
    assert r.intent is None
    assert r.metadata == {}
    assert r.findings == []


def test_report_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        Report(
            report_id=uuid4(),
            schema_version="1.0",
            target_name="x",
            inspector_results=[_make_ir()],
            started_at=_now(),
            finished_at=_now(),
            extra_field="y",  # type: ignore[call-arg]
        )


def test_report_schema_version_locked_to_1_0() -> None:
    with pytest.raises(ValidationError):
        Report(
            report_id=uuid4(),
            schema_version="2.0",  # type: ignore[arg-type]
            target_name="x",
            inspector_results=[_make_ir()],
            started_at=_now(),
            finished_at=_now(),
        )


def test_report_inspector_results_cannot_be_empty() -> None:
    with pytest.raises(ValidationError):
        Report(
            report_id=uuid4(),
            schema_version="1.0",
            target_name="x",
            inspector_results=[],
            started_at=_now(),
            finished_at=_now(),
        )


def test_report_target_name_min_length() -> None:
    with pytest.raises(ValidationError):
        Report(
            report_id=uuid4(),
            schema_version="1.0",
            target_name="",
            inspector_results=[_make_ir()],
            started_at=_now(),
            finished_at=_now(),
        )


def test_report_finished_before_started_rejected() -> None:
    with pytest.raises(ValidationError, match="finished_at must be >= started_at"):
        Report(
            report_id=uuid4(),
            schema_version="1.0",
            target_name="x",
            inspector_results=[_make_ir()],
            started_at=datetime(2026, 5, 26, 12, 0, 0),
            finished_at=datetime(2026, 5, 26, 11, 0, 0),
        )


def test_report_finished_equal_to_started_allowed() -> None:
    t = _now()
    r = Report(
        report_id=uuid4(),
        schema_version="1.0",
        target_name="x",
        inspector_results=[_make_ir()],
        started_at=t,
        finished_at=t,
    )
    assert r.started_at == r.finished_at


def test_report_intent_defaults_none() -> None:
    r = Report(
        report_id=uuid4(),
        schema_version="1.0",
        target_name="x",
        inspector_results=[_make_ir()],
        started_at=_now(),
        finished_at=_now(),
    )
    assert r.intent is None


def test_report_metadata_defaults_empty_dict() -> None:
    r = Report(
        report_id=uuid4(),
        schema_version="1.0",
        target_name="x",
        inspector_results=[_make_ir()],
        started_at=_now(),
        finished_at=_now(),
    )
    assert r.metadata == {}


def test_report_total_evidence_bytes_empty() -> None:
    """No evidence anywhere → 0 bytes."""
    r = Report(
        report_id=uuid4(),
        schema_version="1.0",
        target_name="x",
        inspector_results=[_make_ir()],
        started_at=_now(),
        finished_at=_now(),
    )
    assert r.total_evidence_bytes() == 0


def test_report_total_evidence_bytes_sums_string_fields() -> None:
    """Sums `command` + `stdout` + `stderr` (string-shaped attributes
    across every Evidence on every Finding on every InspectorResult).

    `exit_code` (int), `truncated` (bool), float-shaped `metric_value`
    and the `data` dict are intentionally excluded from the count.
    """

    ev1 = Evidence(
        kind="command_output",
        command="echo hi",  # 7 bytes
        stdout="hi\n",  # 3 bytes
        stderr="err",  # 3 bytes
        exit_code=0,
    )
    ev2 = Evidence(
        kind="file_excerpt",
        path="/etc/hosts",  # 10 bytes
        excerpt="localhost",  # 9 bytes
    )
    ev3 = Evidence(
        kind="metric",
        metric_name="load_1min",  # 9 bytes
        metric_value=0.42,  # float → not counted
    )
    ev4 = Evidence(
        kind="metric",
        metric_name="status",  # 6 bytes
        metric_value="up",  # 2 bytes (str-shaped value is counted)
    )
    finding = Finding(severity="info", message="x", evidence=[ev1, ev2, ev3, ev4])
    ir = _make_ir(findings=[finding])
    r = Report(
        report_id=uuid4(),
        schema_version="1.0",
        target_name="x",
        inspector_results=[ir],
        started_at=_now(),
        finished_at=_now(),
    )
    # 7 + 3 + 3 + 10 + 9 + 9 + 6 + 2 = 49
    assert r.total_evidence_bytes() == 49


def test_report_total_evidence_bytes_utf8_multibyte() -> None:
    """Multi-byte UTF-8 characters count by their byte length, not their
    grapheme / codepoint length. `中` (U+4E2D) encodes as 3 bytes; `é`
    (U+00E9) encodes as 2 bytes.
    """

    ev = Evidence(
        kind="command_output",
        command="中",  # 3 bytes UTF-8
        stdout="éé",  # 4 bytes UTF-8
    )
    finding = Finding(severity="info", message="x", evidence=[ev])
    ir = _make_ir(findings=[finding])
    r = Report(
        report_id=uuid4(),
        schema_version="1.0",
        target_name="x",
        inspector_results=[ir],
        started_at=_now(),
        finished_at=_now(),
    )
    assert r.total_evidence_bytes() == 7


def test_report_is_frozen() -> None:
    r = Report(
        report_id=uuid4(),
        schema_version="1.0",
        target_name="x",
        inspector_results=[_make_ir()],
        started_at=_now(),
        finished_at=_now(),
    )
    with pytest.raises((ValidationError, TypeError)):
        r.target_name = "other"  # type: ignore[misc]
