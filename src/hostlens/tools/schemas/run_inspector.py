"""Pydantic schemas for the `run_inspector` ToolSpec.

`FindingSummary` is a deliberately minimal placeholder for M2 — M3's
report data model will redefine the canonical finding type. Until then,
three fields (severity / message / evidence) keep the surface stable
enough for the agent loop demo path without forcing M2 to ship report
persistence.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "FindingSummary",
    "RunInspectorInput",
    "RunInspectorOutput",
]


class RunInspectorInput(BaseModel):
    """Input schema for `run_inspector`."""

    model_config = ConfigDict(extra="forbid")

    target_name: str
    inspector_name: str
    parameters: dict[str, str] = Field(default_factory=dict)


class FindingSummary(BaseModel):
    """Minimal M2 placeholder for an inspector finding.

    M3 (`add-report-data-model`) will replace this with the full finding
    identity model. For M2 the agent loop only needs severity + a human
    message + opaque evidence dict.
    """

    model_config = ConfigDict(extra="forbid")

    severity: Literal["info", "warning", "critical"]
    message: str
    evidence: dict[str, str] = Field(default_factory=dict)


class RunInspectorOutput(BaseModel):
    """Output schema for `run_inspector`."""

    model_config = ConfigDict(extra="forbid")

    target_name: str
    inspector_name: str
    findings: list[FindingSummary]
