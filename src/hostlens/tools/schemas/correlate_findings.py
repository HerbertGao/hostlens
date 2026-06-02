"""Pydantic schemas for the `correlate_findings` ToolSpec.

`correlate_findings` is the Diagnostician's structured-output channel: each
call carries one root-cause hypothesis. `CorrelateFindingsInput` is the field
shape of a single `RootCauseHypothesis`, except `supporting_findings` carries
the **ordinal labels** (`F1` / `F2` …) shown in the first user message rather
than the 16-hex `Finding.id` (design D-9) — robust against transcription
error. The orchestration layer resolves labels → real ids at harvest time
(design D-2); the handler only does hit-checking, and the output is a bare ack
that **never** echoes a real `Finding.id` back to the model.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CorrelateFindingsInput",
    "CorrelateFindingsOutput",
]


class CorrelateFindingsInput(BaseModel):
    """Input schema for `correlate_findings` — one root-cause hypothesis.

    Field shape mirrors `reporting.models.RootCauseHypothesis` (description /
    confidence / supporting_findings / suggested_actions), with the single
    deliberate difference that `supporting_findings` holds **ordinal labels**
    (e.g. `["F1", "F3"]`) — not real `Finding.id` values. The model only ever
    sees and writes labels; real ids are resolved by the orchestration layer at
    harvest time (design D-2 / D-9).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str = Field(min_length=1)
    confidence: Literal["low", "medium", "high"]
    supporting_findings: list[str] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)


class CorrelateFindingsOutput(BaseModel):
    """Output schema for `correlate_findings` — a bare acknowledgement.

    `accepted` is True when every label in `supporting_findings` resolved
    against the per-run finding-store (no dangling reference). `echoed_labels`
    optionally echoes the accepted **labels** so the model can confirm what was
    recorded. It **must never** carry a real `Finding.id`: the model references
    findings by label, so echoing the id back is both unnecessary and widens the
    redaction surface (design D-2, spec §需求:`correlate_findings`).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted: bool
    echoed_labels: list[str] = Field(default_factory=list)
