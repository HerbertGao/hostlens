"""Pydantic schemas for the `run_inspector` ToolSpec.

`FindingSummary` is a type alias of `hostlens.reporting.models.Finding`
(the unified report-data-model SOT introduced by the
`add-report-data-model` proposal). Keeping the alias name preserves the
existing `from hostlens.tools.schemas.run_inspector import FindingSummary`
import path while letting the underlying schema track the canonical
`Finding` definition exactly.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from hostlens.reporting.models import Finding

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


# Type alias — `FindingSummary` is exactly the canonical `Finding` model
# from `hostlens.reporting.models`. JSON schema for the ToolSpec surface
# is produced by `Finding.model_json_schema()` at adapter projection
# time, so this alias automatically tracks any field-set evolution of
# `Finding` without requiring schema duplication.
FindingSummary = Finding


class RunInspectorOutput(BaseModel):
    """Output schema for `run_inspector`."""

    model_config = ConfigDict(extra="forbid")

    target_name: str
    inspector_name: str
    findings: list[FindingSummary]
