"""Tests for `hostlens.reporting.render_json.render`.

Covers the five scenarios in spec §需求:`render_json.render` 必须先脱敏再走
Pydantic model_dump_json:

1. Output is valid JSON (`json.loads` succeeds, returns dict).
2. Pydantic round-trip compatibility on a clean Report (no sensitive
   strings) — `Report.model_validate(json.loads(...))` succeeds.
3. Pydantic round-trip on a Report with sensitive content — still
   validates as a `Report` (schema-level), but the redacted value path
   is irreversibly masked (no byte-equal round-trip guarantee).
4. `exclude_none=False`: `None` fields are present as `null` in JSON.
5. `indent=2`: 2-space indentation observable in the output.

Also covers tasks §7.8 — the redaction boundary itself: rendering a
Report that contains an API key string in `evidence.stderr` must not
emit the raw key in the JSON output.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from hostlens.inspectors.result import InspectorResult
from hostlens.reporting.models import Evidence, Finding, Report
from hostlens.reporting.render_json import render

API_KEY = "sk-abcdefghijklmnopqrstuvwxyz1234567890"


def _t() -> datetime:
    return datetime(2026, 5, 26, 12, 0, 0)


def _ir(
    findings: list[Finding] | None = None,
    *,
    output: dict[str, Any] | None = None,
) -> InspectorResult:
    return InspectorResult(
        name="demo.echo",
        version="1.0.0",
        status="ok",
        target_name="local-host",
        duration_seconds=0.12,
        output=output or {},
        findings=findings or [],
        error=None,
        missing=[],
    )


def _clean_report() -> Report:
    """Report whose strings contain nothing matched by redact_text's
    default rules. Used for byte-equal round-trip assertions.
    """
    finding = Finding(
        severity="info",
        message="all good",
        evidence=[
            Evidence(
                kind="command_output",
                command="echo hi",
                stdout="hi\n",
                stderr=None,
                exit_code=0,
            )
        ],
        tags=["demo"],
    )
    return Report.from_inspector_results(
        "local-host",
        [_ir([finding])],
        started_at=_t(),
        finished_at=_t(),
        metadata={"trace_id": "abc-123"},
    )


def _report_with_secret() -> Report:
    finding = Finding(
        severity="warning",
        message="suspicious assignment",
        evidence=[
            Evidence(
                kind="command_output",
                command="x",
                stdout="",
                stderr=f"ERROR: invalid api_key={API_KEY}",
            )
        ],
    )
    return Report.from_inspector_results(
        "local-host",
        [_ir([finding])],
        started_at=_t(),
        finished_at=_t(),
    )


# Scenario 1 — output is valid JSON
def test_output_is_valid_json_dict() -> None:
    report = _clean_report()
    raw = render(report)
    decoded = json.loads(raw)
    assert isinstance(decoded, dict)


# Scenario 2 — Pydantic round-trip on a clean Report
def test_pydantic_round_trip_clean_report() -> None:
    report = _clean_report()
    raw = render(report)
    data = json.loads(raw)
    rebuilt = Report.model_validate(data)
    # Spec carve-outs: Pydantic v2 auto-parses UUID + datetime from strings.
    assert rebuilt.report_id == report.report_id
    assert rebuilt.schema_version == report.schema_version
    assert rebuilt.target_name == report.target_name
    assert rebuilt.started_at == report.started_at
    assert rebuilt.finished_at == report.finished_at
    assert rebuilt.findings[0].message == report.findings[0].message
    assert rebuilt.findings[0].evidence[0].kind == "command_output"


# Scenario 3 — round-trip on a Report with sensitive content
def test_round_trip_schema_compatible_when_redaction_active() -> None:
    report = _report_with_secret()
    raw = render(report)

    # Output must not contain the raw secret.
    assert API_KEY not in raw

    # The JSON must still validate as a Report (schema-level).
    data = json.loads(raw)
    rebuilt = Report.model_validate(data)
    # The stderr field on the rebuilt report is the redacted form,
    # not the original — byte-equality is explicitly not required.
    rebuilt_stderr = rebuilt.inspector_results[0].findings[0].evidence[0].stderr
    assert rebuilt_stderr is not None
    assert API_KEY not in rebuilt_stderr


# Scenario 4 — exclude_none=False preserves null fields
def test_intent_none_serialised_as_null() -> None:
    report = _clean_report()
    assert report.intent is None
    raw = render(report)
    data = json.loads(raw)
    assert "intent" in data
    assert data["intent"] is None


# Scenario 5 — indent=2
def test_indent_two_spaces_visible() -> None:
    report = _clean_report()
    raw = render(report)
    # `model_dump_json(indent=2)` emits `\n  "<field>": ...` for every
    # top-level field after the opening brace.
    assert '\n  "' in raw


# Tasks §7.8 — redaction at render boundary (render_json path)
def test_redaction_strips_api_key_from_output() -> None:
    report = _report_with_secret()
    raw = render(report)
    assert API_KEY not in raw


# Defence-in-depth — render must not mutate the source Report
def test_render_does_not_mutate_source_report() -> None:
    report = _report_with_secret()
    _ = render(report)
    # The in-memory report still carries the unredacted secret.
    original_stderr = report.inspector_results[0].findings[0].evidence[0].stderr
    assert original_stderr is not None
    assert API_KEY in original_stderr
