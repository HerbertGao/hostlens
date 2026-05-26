"""Verify the M1.6 BREAKING change: ``Finding.evidence`` is a
``list[Evidence]``, never ``dict``.

Spec: ``openspec/changes/add-report-data-model/specs/inspector-plugin-system/spec.md``
§需求:`Finding.evidence` 必须是 `list[Evidence]` ("旧 dict 形式 evidence
不再接受").

Before this proposal a duplicate ``Finding`` class living in
``inspectors.result`` accepted ``evidence: dict[str, str]``. The unified
SOT in ``reporting.models`` rejects that shape — every construction
attempt must raise ``pydantic.ValidationError``. The test guards against
silent regressions where someone reintroduces a dict-shaped evidence
field for backwards compatibility.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hostlens.reporting.models import Evidence, Finding


def test_finding_rejects_dict_evidence() -> None:
    """The legacy ``evidence={...}`` shape MUST raise ``ValidationError``."""

    with pytest.raises(ValidationError):
        Finding(severity="info", message="x", evidence={"a": "b"})  # type: ignore[arg-type]


def test_finding_accepts_list_of_evidence() -> None:
    """The new list-of-`Evidence` shape continues to work."""

    finding = Finding(
        severity="info",
        message="x",
        evidence=[Evidence(kind="command_output", command="echo hi", stdout="hi\n")],
    )
    assert len(finding.evidence) == 1
    assert finding.evidence[0].kind == "command_output"


def test_finding_default_evidence_is_empty_list() -> None:
    """Omitting ``evidence`` yields ``[]``, not ``{}``."""

    finding = Finding(severity="info", message="x")
    assert finding.evidence == []
    assert isinstance(finding.evidence, list)
