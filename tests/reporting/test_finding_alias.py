"""Type-alias identity: ``inspectors.result.Finding`` is the
``reporting.models.Finding`` SOT.

Spec: ``openspec/changes/add-report-data-model/specs/report-data-model/spec.md``
§需求:`Finding` 在 SOT 之外的所有引用必须是 type alias re-export.

The cross-module re-export removes the "double Finding class" smell
that existed before this proposal (one Pydantic model lived in
``inspectors.result`` and a near-duplicate lived in tool schemas). After
M1.6 every public import path must resolve to the **same class
object**; ``is`` equality is the load-bearing assertion.
"""

from __future__ import annotations


def test_finding_re_export_is_identity() -> None:
    """``inspectors.result.Finding is reporting.models.Finding``."""

    from hostlens.inspectors.result import Finding as _InspectorsFinding
    from hostlens.reporting.models import Finding as _ReportingFinding

    assert _InspectorsFinding is _ReportingFinding
