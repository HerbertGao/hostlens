"""Type-alias identity: ``run_inspector.FindingSummary`` is
``reporting.models.Finding``.

Spec: ``openspec/changes/add-report-data-model/specs/report-data-model/spec.md``
§需求:`Finding` 在 SOT 之外的所有引用必须是 type alias re-export. The
companion test ``tests/reporting/test_finding_alias.py`` pins the same
invariant for ``hostlens.inspectors.result.Finding``.
"""

from __future__ import annotations


def test_finding_summary_is_finding_identity() -> None:
    """``FindingSummary is Finding`` — single class object, no duplication."""

    from hostlens.reporting.models import Finding
    from hostlens.tools.schemas.run_inspector import FindingSummary

    assert FindingSummary is Finding
