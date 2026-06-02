"""Pydantic schemas for the `request_more_inspection` ToolSpec.

`request_more_inspection` lets the Diagnostician re-run one inspector when the
evidence is insufficient. Unlike `run_inspector` (whose wire shape is frozen
for cassette stability and deliberately strips `id` / inspector identity), this
is a **new** tool, so its `output_schema` is free to expose:

1. the inspector `status` (so the model can tell "ran, found nothing" from
   "failed and was swallowed"),
2. each finding's stable `id`, and
3. each finding's ordinal **label** (so a later turn's `correlate_findings`
   can reference the newly-collected findings).

The input carries **no** `target_name`: the target is fixed by the handler
closure to the CLI's `<target>` argument (the Diagnostician is constrained to
the same target the Planner ran against — §7 minimal capability / design D-6).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from hostlens.inspectors.result import InspectorStatus
from hostlens.reporting.models import Finding

__all__ = [
    "LabeledFinding",
    "RequestMoreInspectionInput",
    "RequestMoreInspectionOutput",
]


class RequestMoreInspectionInput(BaseModel):
    """Input schema for `request_more_inspection`.

    `target_name` is intentionally absent — the handler closure fixes it to the
    CLI `<target>` (design D-6). `parameters` mirrors `run_inspector`'s optional
    inspector parameters and is transparently passed to `InspectorRunner.run`.
    """

    model_config = ConfigDict(extra="forbid")

    inspector_name: str
    parameters: dict[str, str] = Field(default_factory=dict)


class LabeledFinding(BaseModel):
    """A collected finding paired with its newly-assigned ordinal label.

    `finding` is the stamped `Finding` (carries a real `id` filled by
    `compute_finding_id`). `label` is the per-run finding-store label (`F4`,
    `F5`, …) the model can reference from a **later** turn's `correlate_findings`
    (same-turn forward references dangle and self-correct — design D-8).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    finding: Finding


class RequestMoreInspectionOutput(BaseModel):
    """Output schema for `request_more_inspection`.

    `status` is the inspector's five-value `InspectorStatus` — surfaced so the
    model distinguishes a genuine non-ok run (`timeout` / `target_unreachable`
    / `requires_unmet` / `exception`, with empty `findings`) from a clean
    "ran, found nothing". `findings` carries each new finding with its stable
    `id` and freshly-assigned ordinal label.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: InspectorStatus
    findings: list[LabeledFinding] = Field(default_factory=list)
