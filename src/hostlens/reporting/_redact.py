"""Internal helper: produce a redacted deep-copy of a `Report` for
rendering.

`redact_report_for_render(report)` walks the report and applies
`hostlens.core.redact.redact_text` to every string field listed in the
`report-data-model` capability spec (§需求:`render_markdown` /
`render_json` 必须在渲染边界对字符串字段过 `core/redact.py`). The
source `Report` is never mutated — a fresh `Report` instance is
returned, suitable for `render_markdown.render` / `model_dump_json`.

Fields that are NOT redacted (per spec):

- `Report.report_id` (UUID)
- `Report.schema_version` (Literal)
- `Report.started_at` / `Report.finished_at` (datetime)
- `Evidence.exit_code` (int)
- `InspectorResult.duration_seconds` (float)
- `Evidence.metric_value` when stored as float (str values are redacted)
"""

from __future__ import annotations

from typing import Any

from hostlens.core.redact import is_sensitive_key, redact_text
from hostlens.reporting.models import Evidence, Finding, Report

__all__ = ["redact_report_for_render"]


def _mask_subtree(value: Any) -> Any:
    """Aggressively replace every string inside `value` (and its nested
    children) with a fully masked placeholder. Used when an adjacent
    dict key name flagged the entire subtree as secret-bearing.
    """
    if isinstance(value, str):
        return "****"
    if isinstance(value, dict):
        return {k: _mask_subtree(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_subtree(item) for item in value]
    return value


def _redact_structured(value: Any) -> Any:
    """Recursively redact strings inside arbitrary JSON-like structures
    (dict / list / str / primitive). Used for `Evidence.data` and for
    `InspectorResult.output`.

    When walking a dict, keys matching `is_sensitive_key` mask the
    associated value subtree wholesale (handles JSON-like
    ``{"password": "<the-value>"}`` shapes that bare-keyword regex
    matching cannot reach).
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        result: dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and is_sensitive_key(k):
                result[k] = _mask_subtree(v)
            else:
                result[k] = _redact_structured(v)
        return result
    if isinstance(value, list):
        return [_redact_structured(item) for item in value]
    return value


def _redact_evidence(evidence: Evidence) -> Evidence:
    """Return a new `Evidence` with all string fields redacted.

    `kind`, `exit_code`, `truncated`, and float-typed `metric_value` are
    passed through unchanged. `metric_value` is only redacted when it is
    stored as a `str` (the `float | str | None` union).
    """
    metric_value: float | str | None = evidence.metric_value
    if isinstance(metric_value, str):
        metric_value = redact_text(metric_value)

    return Evidence(
        kind=evidence.kind,
        command=redact_text(evidence.command) if evidence.command is not None else None,
        stdout=redact_text(evidence.stdout) if evidence.stdout is not None else None,
        stderr=redact_text(evidence.stderr) if evidence.stderr is not None else None,
        exit_code=evidence.exit_code,
        path=redact_text(evidence.path) if evidence.path is not None else None,
        excerpt=redact_text(evidence.excerpt) if evidence.excerpt is not None else None,
        metric_name=(
            redact_text(evidence.metric_name) if evidence.metric_name is not None else None
        ),
        metric_value=metric_value,
        data=_redact_structured(evidence.data) if evidence.data is not None else None,
        truncated=evidence.truncated,
    )


def _redact_finding(finding: Finding) -> Finding:
    # `tags` are not passed through `redact_text`: every tag is already
    # constrained to the safe character set `^[a-z][a-z0-9_-]*$` by the
    # `Tag` Pydantic annotation (see `reporting.models.Tag`), so there
    # is nothing for the keyword-assignment / JWT / `sk-...` regexes to
    # match. Running redact_text on them is not just wasted work — if a
    # future redact rule mangled a tag string the redacted value would
    # violate the pattern constraint and `Finding(tags=...)` here would
    # raise ValidationError, breaking the renderer.
    return Finding(
        severity=finding.severity,
        message=redact_text(finding.message),
        evidence=[_redact_evidence(e) for e in finding.evidence],
        tags=list(finding.tags),
    )


def _redact_inspector_result(ir: Any) -> Any:
    """Return a new `InspectorResult` with string fields redacted.

    Typed as `Any` to keep the module free of an `inspectors.result`
    import at module load (would re-introduce the circular import the
    package design avoids). The function is called with a real
    `InspectorResult`; we reconstruct via its class to preserve type.
    """
    cls = type(ir)
    return cls(
        name=redact_text(ir.name),
        version=redact_text(ir.version),
        status=ir.status,
        target_name=redact_text(ir.target_name),
        duration_seconds=ir.duration_seconds,
        output=_redact_structured(ir.output),
        findings=[_redact_finding(f) for f in ir.findings],
        error=redact_text(ir.error) if ir.error is not None else None,
        missing=[redact_text(m) for m in ir.missing],
    )


def redact_report_for_render(report: Report) -> Report:
    """Return a redacted deep-copy of `report` suitable for rendering.

    The source `report` is not modified. The returned `Report` has the
    same `report_id` / `schema_version` / timestamps as the source, and
    redacted strings on every other path enumerated in the spec.
    """
    return Report(
        report_id=report.report_id,
        schema_version=report.schema_version,
        intent=redact_text(report.intent) if report.intent is not None else None,
        target_name=redact_text(report.target_name),
        inspector_results=[_redact_inspector_result(ir) for ir in report.inspector_results],
        findings=[_redact_finding(f) for f in report.findings],
        started_at=report.started_at,
        finished_at=report.finished_at,
        metadata={k: redact_text(v) for k, v in report.metadata.items()},
    )
