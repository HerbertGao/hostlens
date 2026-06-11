"""Pydantic schemas for the `propose_remediation` ToolSpec.

`propose_remediation` is the Remediation Planner's structured-output channel:
each call carries one complete remediation plan for one finding. It mirrors the
Diagnostician's `correlate_findings` (one structured artefact per call) — the
difference is the artefact is a `RemediationPlan` body, not a hypothesis.

`steps` **reuses the frozen P1a `RemediationStep`** (design D-2): the model
emits a `list[RemediationStep]`, and because emit goes through
`ToolsAdapter.dispatch` (`model_validate`), every P1a invariant
(`high_requires_precheck` / `rollback_none_requires_high` / non-blank command)
is enforced at the emit boundary — a step violating any invariant is rejected
and fed back for self-correction, at zero extra code (design Decision 2).

`finding_label` carries the **ordinal label** (`F1` / `F2` …) shown in the
first user message rather than the 16-hex `Finding.id` (mirrors `correlate_
findings`' `supporting_findings`, design D-9 / D-3) — robust against
transcription error. The handler only hit-checks the label against the per-run
`FindingStore`; the orchestration layer resolves label → real id and stamps
`target_name` at harvest time (design D-2). The output is a bare ack that
**never** echoes a real `Finding.id` back to the model.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from hostlens.remediation.models import RemediationStep

__all__ = [
    "ProposeRemediationInput",
    "ProposeRemediationOutput",
]


class ProposeRemediationInput(BaseModel):
    """Input schema for `propose_remediation` — one remediation plan.

    Field shape mirrors the model-facing subset of `remediation.models.
    RemediationPlan` (rationale / steps / estimated_duration_seconds), with the
    `finding_id` / `target_name` bindings replaced by a single `finding_label`
    **ordinal label** (e.g. `"F1"`) — not a real `Finding.id`. The model only
    ever sees and writes the label; the real id and `target_name` are stamped by
    the orchestration layer at harvest time (design D-2 / D-3 / D-9).

    `steps` reuses the frozen P1a `RemediationStep` so the emit-time
    `model_validate` enforces every P1a cross-field invariant (design Decision
    2) — do not weaken this by substituting a mirror model.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    finding_label: str = Field(min_length=1)
    rationale: str
    estimated_duration_seconds: StrictInt = Field(ge=0)
    steps: list[RemediationStep] = Field(min_length=1)


class ProposeRemediationOutput(BaseModel):
    """Output schema for `propose_remediation` — a bare acknowledgement.

    `accepted` is True when `finding_label` resolved against the per-run
    finding-store (no dangling reference). `echoed_label` optionally echoes the
    accepted **label** so the model can confirm what was recorded. It **must
    never** carry a real `Finding.id`: the model references the finding by
    label, so echoing the id back is both unnecessary and widens the redaction
    surface (mirrors `CorrelateFindingsOutput`, design D-2).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted: bool
    echoed_label: str | None = None
