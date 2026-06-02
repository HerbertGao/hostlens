"""Tests for the Diagnostician tool schemas (tasks 1.2 / 1.3).

- `correlate_findings`: input aligns with `RootCauseHypothesis`,
  `supporting_findings` are labels, output is a bare ack that never carries a
  real `Finding.id`.
- `request_more_inspection`: input has no `target_name`; output exposes
  `status` plus findings with both id and ordinal label.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hostlens.reporting.models import (
    Finding,
    RootCauseHypothesis,
    compute_finding_id,
)
from hostlens.tools.schemas.correlate_findings import (
    CorrelateFindingsInput,
    CorrelateFindingsOutput,
)
from hostlens.tools.schemas.request_more_inspection import (
    LabeledFinding,
    RequestMoreInspectionInput,
    RequestMoreInspectionOutput,
)

# --- 1.2 correlate_findings -------------------------------------------------


def test_correlate_input_field_set_aligns_with_hypothesis() -> None:
    """Input mirrors RootCauseHypothesis (sans the id-vs-label difference)."""
    assert set(CorrelateFindingsInput.model_fields.keys()) == set(
        RootCauseHypothesis.model_fields.keys()
    )
    assert set(CorrelateFindingsInput.model_fields.keys()) == {
        "description",
        "confidence",
        "supporting_findings",
        "suggested_actions",
    }


def test_correlate_input_accepts_ordinal_labels() -> None:
    inp = CorrelateFindingsInput(
        description="load spike",
        confidence="high",
        supporting_findings=["F1", "F3"],
        suggested_actions=["scale up"],
    )
    # supporting_findings are short labels, NOT 16-hex ids.
    assert inp.supporting_findings == ["F1", "F3"]
    assert all(len(label) < 16 for label in inp.supporting_findings)


def test_correlate_input_confidence_is_closed_set() -> None:
    with pytest.raises(ValidationError):
        CorrelateFindingsInput(
            description="x",
            confidence="certain",  # type: ignore[arg-type]
        )


def test_correlate_input_extra_forbidden() -> None:
    with pytest.raises(ValidationError) as ei:
        CorrelateFindingsInput(
            description="x",
            confidence="low",
            finding_id="abc",  # type: ignore[call-arg]
        )
    assert "extra" in str(ei.value).lower()


def test_correlate_output_has_no_real_id_field() -> None:
    """Output is a bare ack: accepted + optional echoed labels. The field set
    must not include any field that could carry a real Finding.id."""
    assert set(CorrelateFindingsOutput.model_fields.keys()) == {
        "accepted",
        "echoed_labels",
    }


def test_correlate_output_json_carries_only_labels_not_real_id() -> None:
    real_id = compute_finding_id("linux.load", "1.0.0", "load high")
    out = CorrelateFindingsOutput(accepted=True, echoed_labels=["F1", "F2"])
    json_text = out.model_dump_json()
    assert real_id not in json_text
    assert "F1" in json_text


# --- 1.3 request_more_inspection --------------------------------------------


def test_request_input_has_no_target_name() -> None:
    """Target is closure-fixed by the handler, never accepted from the model."""
    assert "target_name" not in RequestMoreInspectionInput.model_fields
    assert set(RequestMoreInspectionInput.model_fields.keys()) == {
        "inspector_name",
        "parameters",
    }


def test_request_input_extra_forbidden_rejects_target_name() -> None:
    with pytest.raises(ValidationError) as ei:
        RequestMoreInspectionInput(
            inspector_name="linux.load",
            target_name="prod",  # type: ignore[call-arg]
        )
    assert "extra" in str(ei.value).lower()


def test_request_output_carries_status_and_id_and_label() -> None:
    real_id = compute_finding_id("linux.load", "1.0.0", "load high")
    finding = Finding(
        severity="warning",
        message="load high",
        id=real_id,
        inspector_name="linux.load",
        inspector_version="1.0.0",
    )
    out = RequestMoreInspectionOutput(
        status="ok",
        findings=[LabeledFinding(label="F4", finding=finding)],
    )
    # status field present and is the InspectorStatus closed set.
    assert out.status == "ok"
    # each finding exposes its label and its real id.
    assert out.findings[0].label == "F4"
    assert out.findings[0].finding.id == real_id


def test_request_output_status_accepts_non_ok_values() -> None:
    out = RequestMoreInspectionOutput(status="target_unreachable", findings=[])
    assert out.status == "target_unreachable"


def test_request_output_status_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        RequestMoreInspectionOutput(
            status="boom",  # type: ignore[arg-type]
            findings=[],
        )
