"""Pydantic input schema for the `propose_target_import` ToolSpec.

`ProposeTargetImportInput` is the host-agnostic input contract for the MCP
`propose_target_import` tool. The tool's **output_schema is `ImportPlan`
directly** (not wrapped) — the adapter only does `isinstance(result,
ImportPlan)` + `model_dump()` on the output and never projects it into an
`inputSchema`, so there is no discriminated-union `anyOf` projection problem
to solve here; only this input model is projected via `model_json_schema()`.

Constraints (design D6):

- `source` is `Literal["ssh_config", "yaml"] | None`. An out-of-set value
  fails `model_validate` in the adapter's input-validation step (→ `TypeError`
  → MCP `isError`), never reaching the handler.
- `concurrency` is `Field(ge=1, le=100)` (optional; `None` → the handler
  derives the default 10). The `le=100` upper bound mirrors `TargetProbe`'s
  internal clamp (concurrent SSH fan-out is already capped at ≤100): the
  schema bound turns that silent clamp into an honest contract — an
  over-limit value is rejected rather than silently clamped. (This is NOT a
  handler-side `min()` clamp like `MAX_STATUS_LIMIT`; the rejection is the
  point.)
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ProposeTargetImportInput"]


class ProposeTargetImportInput(BaseModel):
    """Input schema for `propose_target_import`.

    `ref` is the inventory reference (an ssh_config path / yaml path) fed to
    `build_import_plan`. `source` optionally pins the source parser; `None`
    lets the registry content-sniff. `concurrency` optionally overrides the
    probe fan-out within `[1, 100]`; `None` defers to the handler default.
    """

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1)
    source: Literal["ssh_config", "yaml"] | None = None
    concurrency: int | None = Field(default=None, ge=1, le=100)
