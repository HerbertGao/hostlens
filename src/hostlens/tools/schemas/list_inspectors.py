"""Pydantic schemas for the `list_inspectors` ToolSpec."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "InspectorSummary",
    "ListInspectorsInput",
    "ListInspectorsOutput",
]


class ListInspectorsInput(BaseModel):
    """Input schema for `list_inspectors`.

    Both filters are optional; when both `None` the full inspector set is
    returned.
    """

    model_config = ConfigDict(extra="forbid")

    tag: str | None = None
    target_kind: str | None = None


class InspectorSummary(BaseModel):
    """Per-inspector summary returned by `list_inspectors`."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    description: str
    tags: list[str] = Field(default_factory=list)
    compatible_target_kinds: list[str] = Field(default_factory=list)


class ListInspectorsOutput(BaseModel):
    """Output schema for `list_inspectors`."""

    model_config = ConfigDict(extra="forbid")

    inspectors: list[InspectorSummary]
