"""Remediation plan data model SOT — `RemediationStep` / `RemediationPlan`.

Both are Pydantic v2 frozen models with `extra="forbid"`. They are the
frozen contract consumed downstream by:

- M9 P1b Remediation Planner (produces a `RemediationPlan` as structured output)
- M9 P2 Remediation Executor (loads an approved plan from disk and runs it)
- M9 P3 Lark approval card (renders the plan for a human approver)

This module is **pure data + construction-time validation**: no execution
logic, no reference to `ExecutionTarget`, no LLM, no IO beyond the explicit
`RemediationPlan.load_json` parse entrypoint. Per the M9 architecture
invariants it is **not** projected to any surface (no ToolSpec / MCP tool /
CLI command) — Remediation is its own subsystem, not an Agent capability.

Fail-closed by design: every ambiguous or under-specified input is rejected
at construction rather than silently coerced, so a half-valid plan can never
reach the executor.
"""

from __future__ import annotations

import json
import unicodedata
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

__all__ = ["RemediationPlan", "RemediationStep", "RiskLevel"]


RiskLevel = Literal["low", "medium", "high"]
"""Closed three-value risk ladder. Extra values (`critical`, case variants)
are rejected by the `Literal` type — extension must be add-only via a
follow-up OpenSpec proposal."""


_NonEmptyCmd = Annotated[str, StringConstraints(min_length=1)]
"""A command string that is at minimum non-zero-length. The `min_length=1`
constraint rejects `""`; the per-field `_reject_blank_command` validator
additionally rejects any value with no visible content (pure whitespace or
pure invisible characters) WITHOUT mutating it, so legitimate leading/trailing
whitespace around a real command is preserved."""


def _is_blank_equivalent(value: str) -> bool:
    """True if `value` carries no visible content — empty, pure whitespace,
    or composed solely of invisible characters (Unicode categories
    `Cf` format / `Zs` space / `Cc` control, e.g. ZERO WIDTH SPACE `\\u200b`,
    BOM `\\ufeff`). `str.strip()` alone does not remove `Cf` characters, so a
    value of `"\\u200b"` would otherwise pass `min_length=1`. Used for both
    command and binding fields: a string with zero visible glyphs is garbage in
    either role, and rejecting it never touches a value that has real content."""
    return (
        "".join(c for c in value if unicodedata.category(c) not in ("Cf", "Zs", "Cc")).strip() == ""
    )


class RemediationStep(BaseModel):
    """One atomic `precheck → forward → verify` unit (with optional
    `rollback`) of a controlled remediation. Pure data — no execution method,
    no `ExecutionTarget` reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str
    precheck_cmd: _NonEmptyCmd | None
    forward_cmd: _NonEmptyCmd
    rollback_cmd: _NonEmptyCmd | None
    verify_cmd: _NonEmptyCmd
    risk_level: RiskLevel

    @field_validator("forward_cmd", "verify_cmd", "precheck_cmd", "rollback_cmd")
    @classmethod
    def _reject_blank_command(cls, value: str | None) -> str | None:
        if value is not None and _is_blank_equivalent(value):
            raise ValueError("command_must_not_be_blank")
        return value

    @model_validator(mode="after")
    def _validate_risk_invariants(self) -> Self:
        if self.risk_level == "high" and self.precheck_cmd is None:
            raise ValueError(
                "high_requires_precheck: risk_level='high' requires a non-None precheck_cmd"
            )
        if self.rollback_cmd is None and self.risk_level != "high":
            raise ValueError(
                "rollback_none_requires_high: rollback_cmd=None requires risk_level='high'"
            )
        return self


class RemediationPlan(BaseModel):
    """A finding-bound, ordered list of `RemediationStep`s plus metadata.
    `steps` order is execution order, interpreted by the P2 Executor; this
    model attaches no further semantics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    finding_id: _NonEmptyCmd
    target_name: _NonEmptyCmd
    rationale: str
    steps: list[RemediationStep] = Field(min_length=1)
    estimated_duration_seconds: StrictInt = Field(ge=0)

    @field_validator("finding_id", "target_name")
    @classmethod
    def _reject_blank_binding(cls, value: str) -> str:
        if _is_blank_equivalent(value):
            raise ValueError("binding_field_must_not_be_blank")
        return value

    @classmethod
    def load_json(cls, data: str | bytes) -> RemediationPlan:
        """Parse a plan from JSON, **rejecting duplicate object keys**.

        `model_validate_json` relies on pydantic-core's parser, which silently
        keeps the last value on duplicate keys — a tamper/corruption vector for
        plans loaded off disk. This entrypoint parses with a duplicate-key
        rejecting hook first, then validates. P2 must load approved plans
        through here, not through `model_validate_json`."""
        obj = json.loads(data, object_pairs_hook=_reject_duplicate_keys)
        return cls.model_validate(obj)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise ValueError(f"duplicate_json_key: {key}")
        seen.add(key)
    return dict(pairs)
